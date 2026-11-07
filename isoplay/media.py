"""Locating, probing and streaming the thing the user asked to play. No Qt here."""
import http.client
import io
import re
import sys
import threading
import time
import urllib.parse
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

from isoplay import runtime  # noqa: F401 - sets SSL_CERT_FILE before any request
from isoplay.disc_image import project_iso9660

UA = "Mozilla/5.0 isoplay"


def is_url(src):
    return src.split("://", 1)[0].lower() in ("http", "https", "ftp")


BLOCK = 4 << 20
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
        connection = factory(parts.netloc, timeout=self.timeout)
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
                                            "Accept-Encoding": "identity"})
                response = connection.getresponse()
                data = response.read()
            except (http.client.HTTPException, OSError):
                self._drop()
                if attempt == 2:
                    raise
                continue
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


class RangeCache:
    """Local HTTP endpoint for a remote disc image, backed by a block cache with parallel read-ahead.

    libbluray/dvdnav seek constantly and each upstream request costs ~1 s, so menus need the cache;
    one upstream connection (~1.5 MB/s on worker hosts) is too slow for Blu-ray, so playback needs
    several blocks in flight at once.
    """
    # ponytail: in-memory LRU (keep*4 MiB); switch to a disk cache if RAM gets tight

    def __init__(self, url, keep=64, ahead=6):
        self.url, self.keep, self.ahead = url, keep, ahead
        self.upstream = Upstream(url)
        self.blocks, self.pending, self.lock = OrderedDict(), {}, threading.Lock()
        self.pool = ThreadPoolExecutor(ahead + 2)
        self.size = self._size()
        self.view = None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        self.server.handle_error = lambda *a: None  # players drop connections on every seek
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.mrl = f"http://127.0.0.1:{self.server.server_port}/disc.iso"

    def _get(self, start, end):
        return self.upstream.get(start, end)

    def _size(self):
        with self._get(0, 0) as r:
            if r.status != 206:
                raise OSError("server ignores Range requests, so a disc cannot be streamed")
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
                raise OSError(f"short read at block {i}")
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
        # Queue the requested block before its neighbours.
        for i in range(first, min(first + self.ahead + 1, (self.size + BLOCK - 1) // BLOCK)):
            self.block(i)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        if self.view:
            self.view.close()
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.upstream.close()

    def _handler(self):
        cache = self
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
                if start >= size or end < start:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
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
                except Exception:
                    self.close_connection = True

        return H


VIDEO_EXT = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm", ".ts", ".m2ts", ".mts", ".wmv", ".flv",
             ".mpg", ".mpeg", ".vob", ".m3u8", ".mpd", ".mp3", ".flac", ".ogg", ".opus", ".wav", ".m4a"}
DISC_EXT = {".iso", ".img", ".bin", ".udf"}


def suffix(src):
    path = urllib.parse.urlparse(src).path if is_url(src) else src
    return Path(urllib.parse.unquote(path)).suffix.lower()


def iso_signature(data):
    """ISO 9660 (CD001) or UDF (BEA01) volume descriptor at sector 16: DVD5/9, BD25/50/66/100."""
    return data[32769:32774] in (b"CD001", b"BEA01")


def disc_kind(data):
    if b"BDMV" in data:
        return "bluray"
    if b"VIDEO_TS" in data:
        return "dvd"
    return None


def sniff(url):
    try:
        req = Request(url, headers={"Range": "bytes=32768-32775", "User-Agent": UA})
        with urlopen(req, timeout=20) as r:
            return r.status == 206 and iso_signature(b"\0" * 32768 + r.read(8))
    except OSError:
        return False


def prepare(src, disc_root=None):
    """How libVLC should open src: (mrl, "bluray" | "dvd" | None, RangeCache | None). Blocks on I/O."""
    ext = suffix(src)
    if not is_url(src):
        p = Path(src)
        if p.is_dir():
            for sub, kind in (("BDMV", "bluray"), ("VIDEO_TS", "dvd")):
                if (p / sub).is_dir() or (p.name.upper() == sub):
                    root = p if (p / sub).is_dir() else p.parent
                    return ("bluray" if kind == "bluray" else "dvd") + root.as_uri()[4:], kind, None
            return src, None, None
        if ext in VIDEO_EXT:
            return src, None, None
        with open(src, "rb") as f:
            data = f.read(BLOCK)
        return src, (disc_kind(data) if iso_signature(data) else None), None
    if ext in VIDEO_EXT or (ext not in DISC_EXT and not sniff(src)):
        return src, None, None
    proxy = RangeCache(src)
    try:
        data = proxy.read(0, BLOCK)
        if not iso_signature(data):
            proxy.close()
            return src, None, None
        kind = disc_kind(data)
        udf = any(data[s * 2048 + 1:s * 2048 + 6] in (b"BEA01", b"NSR02", b"NSR03")
                  for s in range(16, 32))
        if kind == "bluray" and not udf:
            proxy.view = project_iso9660(proxy, root=disc_root)
        return proxy.mrl, kind, proxy
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
