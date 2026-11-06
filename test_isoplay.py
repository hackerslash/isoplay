import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

import isoplay
from isoplay import RangeCache, disc_kind, fmt, is_iso

assert disc_kind(b"..\x08BDMV..") == "bluray"
assert disc_kind(b"..VIDEO_TS..") == "dvd"
assert disc_kind(b"nothing") is None
assert is_iso("https://h/x/Enthiran%202010%20DVD9.ISO")
assert is_iso("/a/b/disc.iso") and not is_iso("https://h/movie.mkv?x=.iso")
assert fmt(3_725_000) == "1:02:05"

DATA = bytes(range(256)) * 40  # 10240 bytes


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
assert c.read(10200, 500) == DATA[10200:]


def get(rng):
    with urlopen(Request(c.mrl, headers={"Range": rng})) as r:
        return r.status, r.headers["Content-Range"], r.read()


assert get("bytes=1500-") == (206, f"bytes 1500-10239/{len(DATA)}", DATA[1500:])
assert get("bytes=5-2005")[2] == DATA[5:2006]
assert get("bytes=-40")[2] == DATA[-40:]
try:
    get("bytes=99999-")
    raise AssertionError("expected 416")
except Exception as e:
    assert getattr(e, "code", None) == 416, e
assert len(c.blocks) <= 3
c.close()
print("ok")
