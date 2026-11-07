"""Locating, probing and streaming the thing the user asked to play. No Qt here."""
import base64
import http.client
import io
import os
import re
import socket
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
from isoplay.disc_image import (MEDIA_EXT, disc_kind, has_udf, iso_signature, project_iso9660, project_media,
                                 project_remote, project_zip)

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

    def __init__(self, url, timeout=30, share=None):
        if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
            raise OSError("only http and https disc images can be streamed")
        self.url, self.timeout = url, timeout
        # Files on one share reuse each other's connections: one per thread per file ran to hundreds.
        self.local, self.lock, self.connections = ((share.local, share.lock, share.connections) if share
                                                   else (threading.local(), threading.Lock(), []))
        self.throttles, self.closed = 0, False

    def _connection(self):
        url = self.url
        parts = urllib.parse.urlsplit(url)
        held = getattr(self.local, "held", None)
        if held and held[0] == parts[:2]:
            return held[1], url
        self._drop()
        factory = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        connection = factory(parts.hostname, parts.port, timeout=self.timeout)  # netloc may carry user:pass
        self.local.held = (parts[:2], connection)
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
            if self.closed:  # read-ahead still queued when playback stopped; don't keep retrying it
                raise OSError("the stream was closed")
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
            except Exception as e:
                if self.closed:  # close() pulled the socket from under this read; http.client trips over it
                    raise OSError("the stream was closed") from None
                if not isinstance(e, (http.client.HTTPException, OSError)):
                    raise
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
            if response.status == 429 and attempt < 2:  # a CDN throttling a burst; it clears in seconds
                trace(f"upstream {start}-{end} throttled (429), retrying")
                self.throttles += 1
                wait = response.getheader("Retry-After", "")
                time.sleep(min(int(wait), 10) if wait.isdigit() else 2 ** attempt)
                continue
            if response.status >= 400:  # urlopen raised these; http.client hands back the error page
                raise OSError(f"the server answered {response.status} {response.reason}")
            return _Body(response.status, response.headers, data)
        raise OSError("the server kept redirecting the disc image")

    def close(self):
        self.closed = True
        with self.lock:
            connections, self.connections = self.connections, []
        for connection in connections:
            if connection.sock:  # close() alone doesn't wake a read blocked on it; quitting waited 30 s
                try:
                    connection.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
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


class Memory:
    """Blocks held in RAM, least recently used dropped once past `limit` bytes. A share's files
    share one, so its budget holds whichever stream is playing rather than every file it touched."""

    def __init__(self, limit):
        self.limit, self.size, self.items, self.lock = limit, 0, OrderedDict(), threading.Lock()

    def __contains__(self, key):
        return key in self.items

    def __len__(self):
        return len(self.items)

    def get(self, key):
        with self.lock:
            data = self.items.get(key)
            if data is not None:
                self.items.move_to_end(key)
            return data

    def put(self, key, data):
        with self.lock:
            old = self.items.pop(key, None)
            self.size += len(data) - (len(old) if old is not None else 0)
            self.items[key] = data
            while self.size > self.limit and len(self.items) > 1:
                self.size -= len(self.items.popitem(last=False)[1])


KEEP = 256 << 20  # RAM for one link's blocks, or for all of a share's together: the window and behind it
RUN = 4 << 20  # read-ahead is fetched this much per request; each request costs ~1 s before any data
# How far read-ahead runs past the player: ~20 s of UHD. libVLC's own buffer stays short so a
# start or seek is quick; this one fills in the background while it plays.
AHEAD = 160 << 20


class Blocks:
    """A block cache with parallel read-ahead over one remote file.

    libbluray/dvdnav seek constantly and each upstream request costs ~1 s, so menus need the cache;
    one upstream connection (~1.5 MB/s on worker hosts) is too slow for Blu-ray, so playback needs
    several requests in flight at once.

    Block size is a latency/throughput trade. Opening an encrypted DVD makes libdvdcss probe
    sectors scattered over the whole image, and each probe waits for a whole block: on a host
    that gives ~85 KB/s per connection a 4 MiB block is ~50 s of stall, a 512 KiB one ~7 s.
    So a block a reader waits on is fetched alone, while read-ahead goes out in RUN-sized
    requests: CDNs refuse a dozen connections at once (429, then connection refused), and a few
    large requests carry the same throughput as many small ones.
    """
    def __init__(self, url, keep=KEEP, ahead=None, size=None, pool=None, bs=None, upstream=None, background=None,
                 streams=10, memory=None):
        self.url, self.bs, self.streams = url, bs or BLOCK, streams
        self.memory = memory or Memory(keep)
        # Read-ahead requests in flight, found as TCP finds a window: one more per round while there
        # is more to fetch than requests to fetch it, halved when the host throttles or fails.
        # Hosts differ wildly: one CDN refused a dozen connections, a share happily took 24.
        self.limit, self.starved = float(min(3, streams)), False
        self.ahead = AHEAD // self.bs if ahead is None else ahead  # in blocks
        self.run = max(1, RUN // self.bs)
        self.upstream = upstream or Upstream(url)
        self.pending, self.lock, self.requests = {}, threading.Lock(), 0
        self.streak = [-2, -2]  # first and last block of the reader's current in-order run
        self.warming = {}  # block -> (job, first, futures) still queued on a background pool
        self.background = background  # where read-ahead queues, if not behind readers in `pool`
        self.pool, self.own_pool = (pool, False) if pool else (ThreadPoolExecutor(streams + 2), True)
        self.size = self._size() if size is None else size  # a share already knows it

    def _streams(self):
        return int(self.limit)

    def _adapt(self, ok):
        with self.lock:
            if not ok:
                self.limit = max(1.0, self.limit / 2)
            elif self.starved:
                self.limit, self.starved = min(float(self.streams), self.limit + 1 / self.limit), False

    def _get(self, start, end):
        return self.upstream.get(start, end)

    def _size(self):
        with self._get(0, 0) as r:
            if r.status != 206:
                raise OSError("That server ignores Range requests, so it can't be streamed. "
                              "Download the file first")
            return int(r.headers["Content-Range"].rsplit("/", 1)[1])

    def _fetch(self, first, futures):
        start = first * self.bs
        end = min((first + len(futures)) * self.bs, self.size) - 1
        throttles, ok = self.upstream.throttles, True
        try:
            for attempt in range(3):
                try:
                    with self._get(start, end) as r:
                        data = r.read()
                    if len(data) == end - start + 1:
                        break
                except OSError:
                    ok = False
                    if attempt == 2:
                        raise
            else:
                raise OSError(f"short read at block {first}")
            self._adapt(ok and self.upstream.throttles == throttles)
            parts = [data[k * self.bs:(k + 1) * self.bs] for k in range(len(futures))]
            for k, part in enumerate(parts):
                self.memory.put((self, first + k), part)
            for future, part in zip(futures, parts):
                future.set_result(part)
        except BaseException as e:
            self._adapt(False)
            for future in futures:
                if not future.done():
                    future.set_exception(e)
        finally:
            with self.lock:
                for k in range(len(futures)):
                    self.pending.pop(first + k, None)
                    self.warming.pop(first + k, None)
                self.requests -= 1

    def _queue(self, first, last, pool=None):
        """Fetch blocks [first, last) in one request. Lock held; none of them cached or pending."""
        futures = [Future() for _ in range(first, last)]
        for k, future in enumerate(futures):
            self.pending[first + k] = future
        self.requests += 1
        job = (pool or self.pool).submit(self._fetch, first, futures)
        if pool:
            for k in range(len(futures)):
                self.warming[first + k] = (job, first, futures)

    def _promote(self, i):
        """A reader needs block i, still queued behind background fetches: fetch it now instead."""
        entry = self.warming.get(i)
        if entry and entry[0].cancel():
            _, first, futures = entry
            for k in range(len(futures)):
                self.warming.pop(first + k, None)
            self.pool.submit(self._fetch, first, futures)

    def _queue_span(self, first, last, limit=None, pool=None):
        """Queue the missing blocks of [first, last) in runs, stopping at `limit` requests in flight."""
        with self.lock:
            i = first
            while i < last:
                if (self, i) in self.memory or i in self.pending:
                    i += 1
                    continue
                if limit is not None and self.requests >= limit:
                    self.starved = True
                    return
                j = i + 1
                while j < min(last, i + self.run) and (self, j) not in self.memory and j not in self.pending:
                    j += 1
                self._queue(i, j, pool)
                i = j

    def block(self, i):
        with self.lock:
            data = self.memory.get((self, i))
            if data is not None:
                f = Future()
                f.set_result(data)
                return f
            if i not in self.pending:
                self._queue(i, i + 1)
            else:
                self._promote(i)
            return self.pending[i]

    def peek(self, pos, n):
        """A few bytes straight from upstream, without fetching or caching the block around them."""
        with self._get(pos, pos + n - 1) as r:
            return r.read()

    def want(self, pos, n, pool=None):
        """Queue the blocks under a span without waiting for them, on `pool` if given."""
        self._queue_span(pos // self.bs, (min(pos + n, self.size) + self.bs - 1) // self.bs, pool=pool)

    def read(self, pos, n):
        self.want(pos, n)  # queue the whole span before waiting on any of it
        out = bytearray()
        while n > 0 and pos < self.size:
            i, off = divmod(pos, self.bs)
            b = self.block(i).result()[off:off + n]
            out += b
            pos += len(b)
            n -= len(b)
        return bytes(out)

    def prefetch(self, pos):
        # The block a reader needs goes first and alone; read-ahead behind it never delays it.
        first = pos // self.bs
        self.block(first)
        # Read-ahead grows with how far the reader has gone on in order, so a probe or a menu clip
        # glanced at costs one run, not a window per file; playback reaches the full window in seconds.
        with self.lock:
            if not self.streak[0] <= first <= self.streak[1] + 1:
                self.streak = [first, first]
            self.streak[1] = max(self.streak[1], first)
            ahead = min(self.ahead, self.run + 2 * (first - self.streak[0]))
        last = min(first + 1 + ahead, (self.size + self.bs - 1) // self.bs)
        self._queue_span(first + 1, last, limit=self._streams() + 1, pool=self.background)

    def close(self):
        if self.own_pool:
            self.pool.shutdown(wait=False, cancel_futures=True)
        with self.lock:
            for future in self.pending.values():  # a cancelled fetch would leave its readers waiting
                if not future.done():
                    future.set_exception(OSError("closed"))
        self.upstream.close()


class RangeCache(Blocks, Served):
    """A remote disc image, cached in blocks and served to libVLC at a loopback URL."""

    def __init__(self, url, keep=KEEP, ahead=None, size=None):  # keep holds the whole read-ahead window
        super().__init__(url, keep, ahead, size)
        self._serve()

    def close(self):
        Served.close(self)
        Blocks.close(self)


def _endpoint(cache):
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
            if partial:  # a 200 must carry the whole entity, so only a range can be cut short
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
                        cache.prefetch(pos)
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


# A share request waits ~0.5-1.3 s before its first byte, then runs at ~5 MB/s, so small
# blocks spend their connection waiting: 512 KiB gave 0.7 MB/s each, 2 MiB 1.4 and 8 MiB 3.
SHARE_BLOCK = 4 << 20
SHARE_AHEAD = 8  # read-ahead requests in flight per file, one per read-ahead connection
# 24 connections is the most a share gave before it throttled. They are split three ways so a
# reader never queues behind speculation: blocks a reader waits on, read-ahead, and the disc's
# small files fetched in advance. A queued speculative fetch a reader then needs is promoted.
SHARE_POOL = 8


class Share(Served):
    """A disc assembled from the separate files of a remote share.

    One pool of connections is shared by every file: a Blu-ray has a few hundred of them
    and a pool each would mean thousands of threads.
    """

    def __init__(self, files):
        self.pool, self.ahead, self.warm = (ThreadPoolExecutor(SHARE_POOL) for _ in range(3))
        hub, memory = Upstream(next(iter(files.values()))[0]), Memory(KEEP)
        self.streams = {path: RemoteFile(link, size, self.pool, Upstream(link, share=hub), self.ahead, memory)
                        for path, (link, size) in files.items()}
        self._serve()

    def close(self):
        super().close()
        for stream in self.streams.values():
            stream.close()
        for pool in (self.pool, self.ahead, self.warm):
            pool.shutdown(wait=False, cancel_futures=True)


class RemoteFile(io.RawIOBase):
    """One file on a share, read through its own block cache off the share's connection pool."""

    def __init__(self, url, size, pool=None, upstream=None, background=None, memory=None):
        self.cache = Blocks(url, size=size, pool=pool, bs=SHARE_BLOCK, upstream=upstream, background=background,
                            streams=SHARE_AHEAD, memory=memory)
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

    def want(self, pos, size, pool=None):
        self.cache.want(pos, size, pool)

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


class WebPage(OSError):
    """The link answered with a page, not media; a share's index can look like this."""


def _not_a_page(headers, head=b""):
    if (headers.get_content_type() in ("text/html", "application/xhtml+xml")
            or head.lstrip()[:5].lower() in (b"<!doc", b"<html")):
        raise WebPage("That's a web page, not a video file")


def probe(url):
    """One ranged read: ("disc" | "zip" | None, size), sniffed because download links rarely have an
    extension. size is the total a ranged answer reports, so streaming needn't ask again; None if not."""
    req = Request(bare(url), headers={"Range": f"bytes=0-{SNIFF - 1}", "User-Agent": UA,
                                      **credentials(url)})
    try:
        with urlopen(req, timeout=20) as r:
            if r.status not in (200, 206):
                return None, None
            head = r.read(SNIFF)
            _not_a_page(r.headers, head)
            total = (r.headers.get("Content-Range") or "").rpartition("/")[2]
            size = int(total) if r.status == 206 and total.isdigit() else None
    except HTTPError as e:
        if e.code in DEAD or e.code >= 500:
            raise OSError("That link is no longer available") from None
        _not_a_page(e.headers)  # a 416 on a web page still means the link is not a video
        return None, None  # the server dislikes the probe, not the link; let the player try
    except URLError:
        raise OSError("Couldn't reach that server") from None
    except TimeoutError:
        raise OSError("That server didn't respond") from None
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return "zip", size
    for magic, name in ((b"Rar!", ".rar"), (b"7z\xbc\xaf", ".7z"), (b"\xfd7zXZ", ".xz")):
        if head.startswith(magic):
            raise OSError(f"isoplay can't open {name} archives")
    return ("disc" if iso_signature(head) else None), size


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
    # A BD-J menu reads its few hundred small files one round trip at a time; have them on hand.
    # Only those in the disc's folders: loose files beside them are a release's readme.
    root = image.view.disc_root
    prefix = root + "/" if root else ""
    for path, fp in image.streams.items() if root is not None else ():
        if fp.size <= SHARE_BLOCK and path.startswith(prefix) and "/" in path[len(prefix):]:
            fp.want(0, fp.size, image.warm)
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
    """How libVLC should open src: (mrl, kind, Served | None, disc roots, chosen root). Blocks on I/O."""
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
        found, size = probe(src)
    except WebPage as page:
        try:  # a share's own index page looks like one, so give it a listing before giving up
            return _share(src, disc_root)
        except NeedsCredentials:
            raise
        except OSError:
            raise page from None
    if not found:
        return (src, *PLAIN)
    proxy = RangeCache(src, size=size)
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
