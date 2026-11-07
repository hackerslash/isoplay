import base64
import os
import re
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

from isoplay import media
from isoplay.media import RangeCache, Upstream, disc_kind, fmt, iso_signature, prepare, suffix

assert os.path.isfile(os.environ["SSL_CERT_FILE"])  # https links fail without a trust store
assert disc_kind(b"..\x08BDMV..") == "bluray"
assert disc_kind(b"..VIDEO_TS..") == "dvd"
assert disc_kind(b"nothing") is None
assert suffix("https://h/x/Enthiran%202010%20DVD9.ISO") == ".iso"
assert suffix("https://h/movie.mkv?x=.iso") == ".mkv" and suffix("https://h/dl/abc123") == ""


def image(fs, marker):
    d = bytearray(65536)
    d[32769:32774] = fs
    d[50000:50000 + len(marker)] = marker
    return bytes(d)


assert iso_signature(image(b"BEA01", b"")) and iso_signature(image(b"CD001", b""))
assert not iso_signature(b"\x1aE\xdf\xa3" + bytes(70000))  # mkv

import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

tmp = Path(tempfile.mkdtemp())
(tmp / "bd.img").write_bytes(image(b"BEA01", b"BDMV"))  # UDF-only BD, odd extension
(tmp / "dvd.iso").write_bytes(image(b"CD001", b"VIDEO_TS"))
(tmp / "film.mkv").write_bytes(b"x")
(tmp / "Disc/BDMV").mkdir(parents=True)
assert prepare(str(tmp / "bd.img"))[1] == "bluray"
assert prepare(str(tmp / "dvd.iso"))[1] == "dvd"
assert prepare(str(tmp / "film.mkv")) == (str(tmp / "film.mkv"), None, None, (), None)
mrl, kind, *_ = prepare(str(tmp / "Disc"))
assert kind == "bluray" and mrl.startswith("bluray:///"), mrl
assert prepare(str(tmp / "Disc/BDMV"))[0] == mrl
assert fmt(3_725_000) == "1:02:05" and fmt(65_000) == "1:05"

DATA = image(b"BEA01", b"VIDEO_TS")


RANGES = []


class Up(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        RANGES.append(self.headers.get("Range"))
        if self.path == "/missing":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        a, b = map(int, re.fullmatch(r"bytes=(\d+)-(\d+)", self.headers["Range"]).groups())
        body = DATA[a:b + 1]
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {a}-{a + len(body) - 1}/{len(DATA)}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


up = ThreadingHTTPServer(("127.0.0.1", 0), Up)
threading.Thread(target=up.serve_forever, daemon=True).start()
media.BLOCK = 1000
c = RangeCache(f"http://127.0.0.1:{up.server_port}/x.iso", keep=3 * 1000, ahead=2)
assert c.size == len(DATA)
assert c.read(990, 30) == DATA[990:1020]
assert c.read(len(DATA) - 40, 500) == DATA[-40:]


def get(rng):
    with urlopen(Request(c.mrl, headers={"Range": rng})) as r:
        return r.status, r.headers["Content-Range"], r.read()


assert get("bytes=1500-") == (206, f"bytes 1500-{len(DATA) - 1}/{len(DATA)}", DATA[1500:])
assert get("bytes=5-2005")[2] == DATA[5:2006]
assert get("bytes=-40")[2] == DATA[-40:]
try:
    get("bytes=99999-")
    raise AssertionError("expected 416")
except Exception as e:
    assert getattr(e, "code", None) == 416, e
assert c.memory.size <= 3 * 1000  # keep is a byte budget
c.close()



class KeepAlive(Up):
    protocol_version = "HTTP/1.1"


accepted = []


class Counting(ThreadingHTTPServer):
    def process_request(self, request, address):
        accepted.append(address)
        super().process_request(request, address)


alive = Counting(("127.0.0.1", 0), KeepAlive)
threading.Thread(target=alive.serve_forever, daemon=True).start()
c = RangeCache(f"http://127.0.0.1:{alive.server_port}/x.iso", keep=64 * 1000, ahead=0)
for i in range(12):  # one size probe plus twelve block fetches
    c.read(i * 1000, 10)
assert len(accepted) <= 4, f"13 range requests opened {len(accepted)} connections"
c.close()

try:
    Upstream(f"http://127.0.0.1:{alive.server_port}/missing").get(0, 0)
    raise AssertionError("expected the 404 to surface")
except OSError as e:
    assert "404" in str(e), e
try:
    RangeCache("ftp://example.invalid/x.iso")
    raise AssertionError("expected ftp to be rejected")
except OSError as e:
    assert "http" in str(e), e
alive.shutdown()

media.BLOCK = 1 << 20
url = f"http://127.0.0.1:{up.server_port}/download?id=7"  # no extension: probed, then proxied
RANGES.clear()
mrl, kind, proxy, *_ = prepare(url)
assert kind == "dvd" and mrl.startswith("http://127.0.0.1:") and proxy
assert proxy.size == len(DATA) and "bytes=0-0" not in RANGES, RANGES  # the probe's size is reused
proxy.close()
print("ok")


class RangeIgnoredTests(unittest.TestCase):
    """Some hosts advertise Accept-Ranges and then send the whole file anyway."""

    def serve(self, size):
        sent = []

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)  # the Range header is ignored outright
                self.send_header("Content-Length", str(size))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                try:
                    while sum(sent) < size:  # slow enough that draining it would take hours
                        self.wfile.write(b"\0" * (64 << 10))
                        sent.append(64 << 10)
                        time.sleep(0.05)
                except Exception:
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        server.daemon_threads = True
        server.handle_error = lambda *a: None  # the client hangs up mid-body by design
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}/huge.iso", sent

    def test_refused_promptly_instead_of_buffering_the_whole_file(self):
        url, sent = self.serve(64 << 30)
        started = time.monotonic()
        with self.assertRaises(OSError) as caught:
            RangeCache(url)
        self.assertIn("Range", str(caught.exception))
        self.assertLess(time.monotonic() - started, 10)
        self.assertLess(sum(sent), 64 << 20)  # nowhere near the 64 GiB it offered

    def test_a_range_answered_from_the_head_of_the_file_is_not_passed_off_as_data(self):
        url, _ = self.serve(64 << 30)
        with self.assertRaisesRegex(OSError, "Range"):
            Upstream(url).get(1 << 30, (1 << 30) + 4095)


class CredentialsTests(unittest.TestCase):
    """WebDAV shares and the like answer 401 until the link's user:pass is sent as Basic auth."""

    SIZE = 1 << 20

    def serve(self):
        wanted = "Basic " + base64.b64encode(b"me:s3cret").decode()
        seen = []

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                if self.headers.get("Authorization") != wanted:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="stub"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                start, end = (int(x) for x in self.headers["Range"].removeprefix("bytes=").split("-"))
                end = min(end, CredentialsTests.SIZE - 1)
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{CredentialsTests.SIZE}")
                self.send_header("Content-Length", str(end - start + 1))
                self.end_headers()
                self.wfile.write(bytes(end - start + 1))

        server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        server.daemon_threads = True
        server.handle_error = lambda *a: None
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_port, seen

    def test_a_link_carrying_user_and_password_authenticates(self):
        port, seen = self.serve()
        cache = RangeCache(f"http://me:s3cret@127.0.0.1:{port}/movie.iso")
        self.addCleanup(cache.close)
        self.assertEqual(cache.size, self.SIZE)
        self.assertEqual(seen, ["Basic " + base64.b64encode(b"me:s3cret").decode()])

    def test_credentials_are_not_sent_to_a_host_the_link_redirects_to(self):
        self.assertEqual(media.credentials("https://me:s3cret@host/x")["Authorization"],
                         "Basic " + base64.b64encode(b"me:s3cret").decode())
        self.assertEqual(media.credentials("https://elsewhere/x"), {})

    def test_escaped_characters_in_the_password_are_decoded(self):
        self.assertEqual(media.credentials("https://me%40mail.com:p%40ss@host/x")["Authorization"],
                         "Basic " + base64.b64encode(b"me@mail.com:p@ss").decode())


if __name__ == "__main__":
    unittest.main()
