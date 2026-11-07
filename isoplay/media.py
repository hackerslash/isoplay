"""Locating, probing and streaming the thing the user asked to play. No Qt here."""
import base64
import http.client
import io
import os
import re
import sys
import threading
import time
import urllib.parse
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from isoplay import runtime  # noqa: F401 - sets SSL_CERT_FILE before any request
from isoplay import webdav
from isoplay.disc_image import (MEDIA_EXT, disc_kind, has_udf, iso_signature,  # noqa: F401 - re-exported
                                 project_iso9660, project_media, project_remote, project_zip)

UA = "Mozilla/5.0 isoplay"
DEBUG = bool(os.environ.get("ISOPLAY_DEBUG"))


def trace(message):
    if DEBUG:
        print(f"[isoplay] {message}", file=sys.stderr, flush=True)


def credentials(url):
    """Basic auth header for a https://user:pass@host link, which WebDAV shares tend to need.

    Derived from the live URL, so a redirect to another host drops them rather than leaking them.
    """
    parts = urllib.parse.urlsplit(url)
    if not parts.username:
        return {}
    raw = f"{urllib.parse.unquote(parts.username)}:{urllib.parse.unquote(parts.password or '')}"
    return {"Authorization": "Basic " + base64.b64encode(raw.encode()).decode()}


def bare(url):
    """The link without its user:pass, which goes in a header instead of the host name."""
    parts = urllib.parse.urlsplit(url)
    if not parts.username:
        return url
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    return urllib.parse.urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def is_url(src):
    return src.split("://", 1)[0].lower() in ("http", "https", "ftp")


BLOCK = 512 << 10
SERVE_CAP = 16 << 20  # most to answer one range with; players ask for the whole rest
SNIFF_DISC = 4 << 20  # how far in to look for BDMV/VIDEO_TS, independent of the block size
REDIRECTS = {301, 302, 303, 307, 308}


class _Body(io.BytesIO):
    def __init__(self, status, headers, data):
        super().__init__(data)
        self.status, self.headers = status, headers


class Upstream:
    """Keep-alive connections to a remote disc image, one per reader thread.

    A disc generates thousands of range requests, and a fresh TLS handshake for
    each one costs more than the transfer.
    """

    def __init__(self, url, timeout=30):
        if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
            raise OSError("only http and https disc images can be streamed")
        self.url, self.timeout = url, timeout
        self.local = threading.local()
        self.lock = threading.Lock()
        self.connections = []

    def _connection(self):
        url = self.url
        held = getattr(self.local, "held", None)
        if held and held[0] == url:
            return held[1], url
        self._drop()
        parts = urllib.parse.urlsplit(url)
        factory = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        connection = factory(parts.hostname, parts.port, timeout=self.timeout)  # netloc may carry user:pass
        self.local.held = (url, connection)
        with self.lock:
            self.connections.append(connection)
        return connection, url

    def _drop(self):
        held = getattr(self.local, "held", None)
        if not held:
            return
        self.local.held = None
        with self.lock:
            if held[1] in self.connections:
                self.connections.remove(held[1])
        held[1].close()

    def get(self, start, end):
        """One byte range, read in full so the connection is ready for the next block."""
        for attempt in range(3):
            connection, url = self._connection()
            parts = urllib.parse.urlsplit(url)
            try:
                connection.request("GET", urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, "")),
                                   headers={"Range": f"bytes={start}-{end}", "User-Agent": UA,
                                            "Accept-Encoding": "identity", **credentials(url)})
                response = connection.getresponse()
                # Bounded: a server that ignores Range answers with the whole file, and some
                # of those files are tens of gigabytes.
                data = response.read(end - start + 1)
            except (http.client.HTTPException, OSError) as e:
                trace(f"upstream {start}-{end} attempt {attempt + 1} failed: {e!r}")
                self._drop()
                if attempt == 2:
                    raise
                continue
            if response.status == 200:
                self._drop()  # the rest of the body is still pending, so the connection is spent
                if start:  # these bytes are the head of the file, not the range we asked for
                    raise OSError("That server ignores Range requests, so it can't be streamed")
            if response.status in REDIRECTS and response.getheader("Location"):
                self.url = urllib.parse.urljoin(url, response.getheader("Location"))
                self._drop()
                continue
            if response.will_close:
                self._drop()
            if response.status >= 400:  # urlopen raised these; http.client hands back the error page
                raise OSError(f"the server answered {response.status} {response.reason}")
            return _Body(response.status, response.headers, data)
        raise OSError("the server kept redirecting the disc image")

    def close(self):
        with self.lock:
            connections, self.connections = self.connections, []
        for connection in connections:
            connection.close()


class Served:
    """Whatever `view` projects, offered to libVLC at a loopback URL."""

    size = 0  # only the raw-image path reads this; a view carries its own length

    def _serve(self):
        self.view = None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _endpoint(self))
        self.server.daemon_threads = True
        self.server.handle_error = lambda *a: None  # players drop connections on every seek
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.mrl = f"http://127.0.0.1:{self.server.server_port}/disc.iso"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        if self.view:
            self.view.close()


class Blocks:
    """A block cache with parallel read-ahead over one remote file.

    libbluray/dvdnav seek constantly and each upstream request costs ~1 s, so menus need the cache;
    one upstream connection (~1.5 MB/s on worker hosts) is too slow for Blu-ray, so playback needs
    several blocks in flight at once.

    Block size is a latency/throughput trade. Opening an encrypted DVD makes libdvdcss probe
    sectors scattered over the whole image, and each probe waits for a whole block: on a host
    that gives ~85 KB/s per connection a 4 MiB block is ~50 s of stall, a 512 KiB one ~7 s.
    Sequential throughput is kept up by holding more blocks in flight instead.
    """
    # ponytail: in-memory LRU (keep*BLOCK); switch to a disk cache if RAM gets tight

    def __init__(self, url, keep=512, ahead=14, size=None, pool=None):
        self.url, self.keep, self.ahead = url, keep, ahead
        self.upstream = Upstream(url)
        self.blocks, self.pending, self.lock = OrderedDict(), {}, threading.Lock()
        self.pool, self.own_pool = (pool, False) if pool else (ThreadPoolExecutor(ahead + 2), True)
        self.size = self._size() if size is None else size  # a share already knows it

    def _get(self, start, end):
        return self.upstream.get(start, end)

    def _size(self):
        with self._get(0, 0) as r:
            if r.status != 206:
                raise OSError("That server ignores Range requests, so it can't be streamed. "
                              "Download the file first")
            return int(r.headers["Content-Range"].rsplit("/", 1)[1])

    def _load(self, i):
        start = i * BLOCK
        end = min(start + BLOCK, self.size) - 1
        try:
            for attempt in range(3):
                try:
                    with self._get(start, end) as r:
                        data = r.read()
                    if len(data) == end - start + 1:
                        break
                except OSError:
                    if attempt == 2:
                        raise
            else:
                raise OSError(f"short read at block {i}")  # noqa: TRY003
            with self.lock:
                self.blocks[i] = data
                while len(self.blocks) > self.keep:
                    self.blocks.popitem(last=False)
            return data
        finally:
            with self.lock:
                self.pending.pop(i, None)

    def block(self, i):
        with self.lock:
            if i in self.blocks:
                self.blocks.move_to_end(i)
                f = Future()
                f.set_result(self.blocks[i])
                return f
            if i not in self.pending:
                self.pending[i] = self.pool.submit(self._load, i)
            return self.pending[i]

    def read(self, pos, n):
        end = min(pos + n, self.size)
        for i in range(pos // BLOCK, (end + BLOCK - 1) // BLOCK):
            self.block(i)  # queue the whole span before waiting on any of it
        out = bytearray()
        while n > 0 and pos < self.size:
            i, off = divmod(pos, BLOCK)
            b = self.block(i).result()[off:off + n]
            out += b
            pos += len(b)
            n -= len(b)
        return bytes(out)

    def prefetch(self, pos):
        first = pos // BLOCK
        # Queue the requested block before its neighbours, and stop once enough is in flight:
        # speculative blocks must never delay one a reader is already waiting on.
        for i in range(first, min(first + self.ahead + 1, (self.size + BLOCK - 1) // BLOCK)):
            if i > first and len(self.pending) >= self.ahead:
                return
            self.block(i)

    def close(self):
        if self.own_pool:
            self.pool.shutdown(wait=False, cancel_futures=True)
        self.upstream.close()


class RangeCache(Blocks, Served):
    """A remote disc image, cached in blocks and served to libVLC at a loopback URL."""

    def __init__(self, url, keep=512, ahead=14):
        super().__init__(url, keep, ahead)
        self._serve()

    def close(self):
        Served.close(self)
        Blocks.close(self)

    def _handler(self):
        return _endpoint(self)


def _endpoint(cache):
    last = (cache.size - 1) // BLOCK

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _head(self):
            size = cache.view.size if cache.view else cache.size
            start, end, partial = 0, size - 1, False
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
            if m and (m[1] or m[2]):
                partial = True
                if m[1]:
                    start = int(m[1])
                    end = min(int(m[2]), end) if m[2] else end
                else:
                    start = max(0, size - int(m[2]))
            # A player asks for the rest of the disc, reads a little and seeks away. Answering in
            # full leaves a thread streaming gigabytes nobody reads, whose read-ahead then crowds
            # out the seek that follows -- which the player reports as a seek failure.
            end = min(end, start + SERVE_CAP - 1)
            if start >= size or end < start:
                trace(f"serve {self.headers.get('Range', '')} -> 416 (size {size})")
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            trace(f"serve {self.headers.get('Range', 'whole file')} -> {206 if partial else 200} "
                  f"{start}-{end}/{size}")
            self.send_response(206 if partial else 200)
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            return start, end

        def do_HEAD(self):
            self._head()

        def do_GET(self):
            r = self._head()
            if not r:
                return
            pos, end = r
            first = pos // BLOCK
            try:
                while pos <= end:
                    if cache.view:
                        chunk = cache.view.read(pos, min(BLOCK, end - pos + 1))
                        self.wfile.write(chunk)
                        pos += len(chunk)
                        continue
                    i, off = divmod(pos, BLOCK)
                    if i > first:  # reader is streaming, not probing: keep blocks in flight
                        for j in range(i + 1, min(i + cache.ahead, last) + 1):
                            cache.block(j)
                    chunk = cache.block(i).result()[off:off + end - pos + 1]
                    self.wfile.write(chunk)
                    pos += len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                trace(f"serving {self.headers.get('Range', 'whole file')} failed at {pos}: {e!r}")
                self.close_connection = True

    return H


class LocalImage(Served):
    """An image on disk, projected and served when libVLC can't open it directly."""

    def __init__(self, path):
        self.fp = open(path, "rb")
        self.size = self.fp.seek(0, 2)
        self.lock = threading.Lock()
        self._serve()

    def read(self, pos, size):
        with self.lock:
            self.fp.seek(pos)
            return self.fp.read(size)

    def close(self):
        super().close()
        self.fp.close()


SHARE_AHEAD = 22  # 24 connections: measured as the most a share gives before it throttles
SHARE_KEEP = 128  # blocks per file, so ~64 MiB on whichever stream is playing


class Share(Served):
    """A disc assembled from the separate files of a remote share.

    One pool of connections is shared by every file: a Blu-ray has a few hundred of them
    and a pool each would mean thousands of threads.
    """

    def __init__(self, files):
        self.pool = ThreadPoolExecutor(SHARE_AHEAD + 2)
        self.streams = {path: RemoteFile(link, size, self.pool) for path, (link, size) in files.items()}
        self._serve()

    def close(self):
        super().close()
        for stream in self.streams.values():
            stream.close()
        self.pool.shutdown(wait=False, cancel_futures=True)


class RemoteFile(io.RawIOBase):
    """One file on a share, read through its own block cache off the share's connection pool."""

    def __init__(self, url, size, pool=None):
        self.cache = Blocks(url, keep=SHARE_KEEP, ahead=SHARE_AHEAD, size=size, pool=pool)
        self.size, self.pos = size, 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=0):
        self.pos = (0 if whence == 0 else self.pos if whence == 1 else self.size) + offset
        return self.pos

    def want(self, pos, size):
        """Queue the blocks under a span without waiting for them."""
        for i in range(pos // BLOCK, (min(pos + size, self.size) + BLOCK - 1) // BLOCK):
            self.cache.block(i)

    def pread(self, pos, size):
        size = min(size, self.size - pos)
        if size <= 0:
            return b""
        # Reads arrive a block at a time, so the next ones have to be in flight already.
        self.cache.prefetch(pos)
        return self.cache.read(pos, size)

    def read(self, size=-1):
        data = self.pread(self.pos, self.size - self.pos if size < 0 else size)
        self.pos += len(data)
        return data

    def close(self):
        self.cache.close()
        super().close()


class NeedsCredentials(OSError):
    """The share refused the listing until the link carries a user name and password."""


def propfind(url):
    """One level of a WebDAV collection, as a Multi-Status body."""
    req = Request(bare(url), method="PROPFIND", data=webdav.PROPS,
                  headers={"Depth": "1", "Content-Type": "text/xml; charset=utf-8",
                           "User-Agent": UA, **credentials(url)})
    try:
        with urlopen(req, timeout=30) as r:
            return r.read()
    except HTTPError as e:
        if e.code in (401, 403):
            if urllib.parse.urlsplit(url).username:
                raise NeedsCredentials("That share rejected the user name and password in the link") from None
            raise NeedsCredentials("That share needs a user name and password in the link") from None
        if e.code in (405, 501):
            raise OSError("That link isn't a WebDAV share") from None
        raise OSError(f"That share answered {e.code}") from None
    except URLError:
        raise OSError("Couldn't reach that share") from None


VIDEO_EXT = MEDIA_EXT | {".m3u8", ".mpd"}
DISC_EXT = {".iso", ".img", ".bin", ".udf"}
ARCHIVE_EXT = {".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".r00", ".001", ".part", ".torrent"}
KNOWN_EXT = VIDEO_EXT | DISC_EXT | ARCHIVE_EXT | {".zip"}
BARE_HOST = re.compile(r"[\w-]+(\.[\w-]+)+(/.*)?")
DEAD = {401, 402, 403, 404, 410, 451}  # the link is wrong; other 4xx may just be an unloved Range
SNIFF = 32776  # enough to reach the volume descriptor at sector 16
PLAIN = (None, None, (), None)  # kind, proxy, disc roots, chosen root: hand it straight to the player


def suffix(src):
    path = urllib.parse.urlparse(src).path if is_url(src) else src
    return Path(urllib.parse.unquote(path)).suffix.lower()


def normalize(src):
    """A bare host like google.com is a link with the scheme left off, not a missing file."""
    src = src.strip()
    if is_url(src) or Path(src).exists():
        return src
    if BARE_HOST.fullmatch(src) and ("/" in src or suffix(src) not in KNOWN_EXT):
        return "https://" + src
    return src


def _not_a_page(headers, head=b""):
    if (headers.get_content_type() in ("text/html", "application/xhtml+xml")
            or head.lstrip()[:5].lower() in (b"<!doc", b"<html")):
        raise OSError("That's a web page, not a video file")


def probe(url):
    """One ranged read: "disc", "zip" or None, sniffed because download links rarely have an extension."""
    req = Request(bare(url), headers={"Range": f"bytes=0-{SNIFF - 1}", "User-Agent": UA,
                                      **credentials(url)})
    try:
        with urlopen(req, timeout=20) as r:
            if r.status not in (200, 206):
                return None
            head = r.read(SNIFF)
            _not_a_page(r.headers, head)
    except HTTPError as e:
        if e.code in DEAD or e.code >= 500:
            raise OSError("That link is no longer available") from None
        _not_a_page(e.headers)  # a 416 on a web page still means the link is not a video
        return None  # the server dislikes the probe, not the link; let the player try
    except URLError:
        raise OSError("Couldn't reach that server") from None
    except TimeoutError:
        raise OSError("That server didn't respond") from None
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return "zip"
    for magic, name in ((b"Rar!", ".rar"), (b"7z\xbc\xaf", ".7z"), (b"\xfd7zXZ", ".xz")):
        if head.startswith(magic):
            raise OSError(f"isoplay can't open {name} archives")
    return "disc" if iso_signature(head) else None


DEPTH = 2  # folders to descend looking for discs: a rip of several discs is rarely deeper


def disc_folders(path):
    """Every DVD or Blu-ray folder at or under path: {folder to hand libVLC: kind}."""
    found, stack = {}, [(path, 0)]
    while stack:
        here, level = stack.pop()
        if (here / "BDMV").is_dir():
            found[here] = "bluray"
        elif (here / "VIDEO_TS").is_dir():
            found[here] = "dvd"
        elif level < DEPTH:
            stack += [(child, level + 1) for child in sorted(here.iterdir()) if child.is_dir()]
    return found


def _folder(path, disc_root):
    if path.name.upper() in ("BDMV", "VIDEO_TS"):
        path = path.parent  # the disc is the folder holding it
    discs = disc_folders(path)
    if not discs:
        videos = [f for f in path.iterdir() if f.is_file() and suffix(f.name) in VIDEO_EXT]
        if len(videos) == 1:
            return (str(videos[0]), *PLAIN)
        raise OSError("No DVD or Blu-ray in that folder")
    roots = tuple(sorted(str(d) for d in discs))
    root = disc_root if disc_root in roots else roots[0]
    kind = discs[Path(root)]
    return kind + Path(root).as_uri()[4:], kind, None, roots, root


def _streamed(proxy, kind):
    view = proxy.view
    if view is not None and getattr(view, "name", None):
        # libVLC picks its demuxer from the URL, so a lifted title keeps its own name
        proxy.mrl = proxy.mrl.rpartition("/")[0] + "/" + urllib.parse.quote(view.name)
    return proxy.mrl, kind, proxy, view.discs if view else (), view.disc_root if view else None


def _share(url, disc_root):
    """A WebDAV collection: list it, project the disc on it, serve that."""
    files = webdav.walk(url, propfind)
    if not files:
        raise OSError("That share is empty")
    image = Share(files)
    try:
        image.view = project_remote(image.streams, root=disc_root)
    except Exception:
        image.close()
        raise
    return _streamed(image, image.view.kind)


def _project(image, project):
    """Serve image through project, or close it and give up so the caller can fall back."""
    try:
        image.view = project(image)
    except Exception:
        image.close()
        raise
    if image.view is None:
        image.close()
        return None
    return _streamed(image, image.view.kind)


def prepare(src, disc_root=None):
    """How libVLC should open src: (mrl, kind, RangeCache | None, disc roots, chosen root). Blocks on I/O."""
    ext = suffix(src)
    if ext in ARCHIVE_EXT:
        raise OSError(f"isoplay can't open {ext} archives")
    if not is_url(src):
        p = Path(src)
        if not p.exists():
            raise OSError("That file isn't there any more")
        if p.is_dir():
            return _folder(p, disc_root)
        if ext in VIDEO_EXT:
            return (src, *PLAIN)
        with open(p, "rb") as f:
            data = f.read(SNIFF_DISC)
        if data[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
            return _project(LocalImage(p), lambda image: project_zip(image, root=disc_root))
        if not iso_signature(data):
            return (src, *PLAIN)
        kind = disc_kind(data)
        # A DVD, or a Blu-ray already in UDF, opens best straight off disk; the rest need projecting
        project = None
        if kind == "bluray" and not has_udf(data):
            project = lambda image: project_iso9660(image, root=disc_root)  # noqa: E731
        elif kind is None:  # HD DVD, VCD, DVD-Audio, a rip of loose files
            project = project_media
        if project:
            lifted = _project(LocalImage(p), project)
            if lifted:
                return lifted
        return src, kind, None, (), None
    if src.endswith("/"):  # a collection, not a file: only a share walk can make sense of it
        return _share(src, disc_root)
    try:
        found = probe(src)
    except OSError as page:
        if "web page" not in str(page):
            raise
        try:  # a share's own index page looks like one, so give it a listing before giving up
            return _share(src, disc_root)
        except NeedsCredentials:
            raise
        except OSError:
            raise page from None
    if not found:
        return (src, *PLAIN)
    proxy = RangeCache(src)
    try:
        if found == "zip":
            proxy.view = project_zip(proxy, root=disc_root)
            return _streamed(proxy, proxy.view.kind)
        data = proxy.read(0, SNIFF_DISC)
        kind = disc_kind(data)
        if kind == "bluray" and not has_udf(data):
            proxy.view = project_iso9660(proxy, root=disc_root)
        elif kind is None:
            proxy.view = project_media(proxy)
        return _streamed(proxy, kind)
    except Exception:
        proxy.close()
        raise


def fmt(ms):
    s = max(ms, 0) // 1000
    return f"{s // 3600}:{s // 60 % 60:02}:{s % 60:02}" if s >= 3600 else f"{s // 60}:{s % 60:02}"


def ago(ts, now=None):
    m = int(((now or time.time()) - ts) // 60)
    if m < 1:
        return "Just now"
    if m < 60:
        return f"{m} min ago"
    if m < 24 * 60:
        return f"{m // 60} h ago"
    if m < 48 * 60:
        return "Yesterday"
    return time.strftime("%-d %b" if sys.platform != "win32" else "%d %b", time.localtime(ts))


def display_name(src):
    return urllib.parse.unquote(src.rstrip("/").rsplit("/", 1)[-1].split("?")[0]) or src


def origin(src):
    return (urllib.parse.urlparse(src).hostname or src) if is_url(src) else str(Path(src).parent)
