import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

import isoplay
from isoplay import RangeCache, disc_kind, fmt, iso_signature, prepare, suffix

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
assert prepare(str(tmp / "film.mkv")) == (str(tmp / "film.mkv"), None, None)
mrl, kind, _ = prepare(str(tmp / "Disc"))
assert kind == "bluray" and mrl.startswith("bluray:///"), mrl
assert prepare(str(tmp / "Disc/BDMV"))[0] == mrl
assert fmt(3_725_000) == "1:02:05"

DATA = image(b"BEA01", b"VIDEO_TS")


class Up(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        a, b = map(int, re.fullmatch(r"bytes=(\d+)-(\d+)", self.headers["Range"]).groups())
        body = DATA[a:b + 1]
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {a}-{a + len(body) - 1}/{len(DATA)}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


up = ThreadingHTTPServer(("127.0.0.1", 0), Up)
threading.Thread(target=up.serve_forever, daemon=True).start()
isoplay.BLOCK = 1000
c = RangeCache(f"http://127.0.0.1:{up.server_port}/x.iso", keep=3, ahead=2)
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
assert len(c.blocks) <= 3
c.close()

isoplay.BLOCK = 1 << 20
url = f"http://127.0.0.1:{up.server_port}/download?id=7"  # no extension: sniffed, then proxied
mrl, kind, proxy = prepare(url)
assert kind == "dvd" and mrl.startswith("http://127.0.0.1:") and proxy
proxy.close()
print("ok")
