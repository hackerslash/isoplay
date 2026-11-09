"""A remote DVD image played to its first seconds through the loopback cache and libmpv, headless.

The origin is slow and throttles every fifth request with a 429, like a debrid host. Needs libmpv
0.42+ with tools/mpv-stream-discs.patch (the bundle's in CI, else LIBMPV_PATH or the system one).
"""
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
bundled = ROOT / "dist" / ("isoplay.app/Contents/Frameworks" if sys.platform == "darwin" else "isoplay/_internal") / "mpv"
lib = bundled / {"darwin": "libmpv.2.dylib", "win32": "libmpv-2.dll"}.get(sys.platform, "libmpv.so.2")
if lib.is_file():
    os.environ["LIBMPV_PATH"] = str(lib)
    if sys.platform == "win32":
        os.add_dll_directory(str(bundled))

from isoplay import media  # noqa: E402
from isoplay.diskcache import DiskCache  # noqa: E402
from isoplay.mpv import binding  # noqa: E402

DISC = (ROOT / "tests/fixtures/tiny-dvd.iso").read_bytes()


class Origin(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests = 0

    def log_message(self, *a):
        pass

    def do_GET(self):
        Origin.requests += 1
        time.sleep(0.03)
        if Origin.requests % 5 == 0:
            self.send_response(429)
            self.send_header("Retry-After", "0")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        m = re.fullmatch(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
        start, end = int(m[1]), min(int(m[2] or len(DISC) - 1), len(DISC) - 1)
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {start}-{end}/{len(DISC)}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        self.wfile.write(DISC[start:end + 1])


class RemoteDiscTests(unittest.TestCase):
    def setUp(self):
        try:
            self.mpv = binding()
        except SystemExit as e:
            self.skipTest(str(e))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        media.DISK = DiskCache(tempfile.mkdtemp(), 1 << 30)
        self.addCleanup(setattr, media, "DISK", None)

    def play(self):
        Origin.requests = 0
        mrl, kind, proxy, *_ = media.prepare(f"http://127.0.0.1:{self.server.server_port}/tiny-dvd.iso")
        self.assertEqual(kind, "dvd")
        # printed as it comes, not on failure: a crash in libmpv takes the process before any except runs
        player = self.mpv.MPV(vo="null", ao="null", config="no", loglevel="v",
                              log_handler=lambda level, prefix, text: print(f"[{prefix}] {text.rstrip()}",
                                                                            file=sys.stderr, flush=True))
        try:
            player["dvd-device"] = mrl
            player["disc-menu"] = "no"
            player.command("loadfile", "dvd://", "replace")
            player.wait_until_playing(timeout=30)
            deadline = time.monotonic() + 20
            while (player.time_pos or 0) < 1.0:
                self.assertLess(time.monotonic(), deadline, "the disc never played a second")
                time.sleep(0.1)
            self.assertAlmostEqual(player.duration, 4.0, delta=0.5)
        finally:
            player.terminate()
            proxy.close()
        return Origin.requests

    def test_a_throttled_remote_dvd_plays_and_reopens_from_disk(self):
        first = self.play()
        self.assertGreater(len(list(media.DISK.root.glob("??/*"))), 0)
        self.assertLess(self.play(), first)


if __name__ == "__main__":
    unittest.main()
