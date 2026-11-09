"""A disc folder on a WebDAV share, listed and projected without a socket to the outside."""
import base64
import os
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pycdlib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from isoplay import media, webdav
from isoplay.disc_image import CacheFile

DISC = {"Movie/BDMV/index.bdmv": b"INDX0200",
        "Movie/BDMV/MovieObject.bdmv": b"MOBJ0200",
        "Movie/BDMV/PLAYLIST/00000.mpls": b"MPLS0200",
        "Movie/BDMV/STREAM/00001.m2ts": b"\x47" * 6144,
        "Movie/readme.txt": b"notes"}
AUTH = "Basic " + base64.b64encode(b"me:s3cret").decode()


def share(files, require_auth=False):
    """A WebDAV server answering PROPFIND and ranged GET for the given {path: bytes}."""
    hits = []

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def guard(self):
            if require_auth and self.headers.get("Authorization") != AUTH:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="stub"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return False
            return True

        def here(self):
            return urllib.parse.unquote(self.path).strip("/")

        def do_PROPFIND(self):
            if not self.guard():
                return
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            prefix = self.here() + "/" if self.here() else ""
            entries, seen = [f'<response><href>/{prefix}</href><propstat><prop>'
                             f'<resourcetype><collection/></resourcetype></prop></propstat></response>'], set()
            for path in files:
                if not path.startswith(prefix):
                    continue
                rest = path[len(prefix):]
                head, slash, _ = rest.partition("/")
                if slash:
                    if head in seen:
                        continue
                    seen.add(head)
                    entries.append(f'<response><href>/{prefix}{head}/</href><propstat><prop>'
                                   f'<resourcetype><collection/></resourcetype></prop></propstat></response>')
                else:
                    entries.append(f'<response><href>/{prefix}{rest}</href><propstat><prop>'
                                   f'<resourcetype/><getcontentlength>{len(files[path])}'
                                   f'</getcontentlength></prop></propstat></response>')
            body = f'<?xml version="1.0"?><multistatus xmlns="DAV:">{"".join(entries)}</multistatus>'.encode()
            self.send_response(207)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.guard():
                return
            data = files.get(self.here())
            if data is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            hits.append(self.here())
            start, end = (int(x) for x in self.headers["Range"].removeprefix("bytes=").split("-"))
            end = min(end, len(data) - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            self.wfile.write(data[start:end + 1])

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    server.daemon_threads = True
    server.handle_error = lambda *a: None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, hits


class ShareTests(unittest.TestCase):
    def open(self, files=DISC, require_auth=False, user=""):
        server, hits = share(files, require_auth)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.hits = hits
        return f"http://{user}127.0.0.1:{server.server_port}/"

    def test_a_bluray_folder_on_a_share_is_projected_as_udf(self):
        mrl, kind, proxy, discs, root = media.prepare(self.open())
        self.addCleanup(proxy.close)
        self.assertEqual((kind, discs, root), ("bluray", ("Movie",), "Movie"))
        self.assertTrue(mrl.startswith("http://127.0.0.1:"))
        iso = pycdlib.PyCdlib()
        iso.open_fp(CacheFile(proxy.view))
        self.addCleanup(iso.close)
        with iso.open_file_from_iso(udf_path="/BDMV/index.bdmv") as index:
            self.assertEqual(index.read(), b"INDX0200")
        with iso.open_file_from_iso(udf_path="/BDMV/STREAM/00001.m2ts") as stream:
            self.assertEqual(stream.read(8), b"\x47" * 8)
        self.assertNotIn("Movie/readme.txt", self.hits)  # only the disc's own files are fetched

    def test_listing_the_share_does_not_fetch_any_payload(self):
        url = self.open()
        files = webdav.walk(url, media.propfind)
        self.assertEqual(sorted(files), sorted(DISC))
        self.assertEqual(files["Movie/BDMV/STREAM/00001.m2ts"][1], 6144)
        self.assertEqual(self.hits, [])

    def test_a_share_without_a_disc_plays_its_largest_media_file(self):
        url = self.open({"clips/small.mkv": b"a" * 16, "clips/big.mkv": b"b" * 64, "clips/notes.txt": b"x"})
        mrl, kind, proxy, _, _ = media.prepare(url)
        self.addCleanup(proxy.close)
        self.assertEqual((kind, proxy.view.name), (None, "big.mkv"))
        self.assertTrue(mrl.endswith("/big.mkv"))
        self.assertEqual(proxy.view.read(0, 64), b"b" * 64)

    def test_credentials_in_the_link_reach_both_the_listing_and_the_files(self):
        url = self.open(require_auth=True, user="me:s3cret@")
        _, kind, proxy, _, _ = media.prepare(url)
        self.addCleanup(proxy.close)
        self.assertEqual(kind, "bluray")
        self.assertEqual(proxy.view.read(32768, 6), b"\x01CD001")

    def test_a_share_needing_credentials_says_so(self):
        with self.assertRaises(OSError) as caught:
            media.prepare(self.open(require_auth=True))
        self.assertIn("user name and password", str(caught.exception))

    def test_a_share_rejecting_the_credentials_says_so(self):
        with self.assertRaisesRegex(OSError, "rejected"):
            media.prepare(self.open(require_auth=True, user="me:wrong@"))

    def test_listing_survives_full_url_hrefs_split_propstats_and_other_encodings(self):
        body = (b'<?xml version="1.0"?><multistatus xmlns="DAV:">'
                b'<response><href>http://host/My%20Disc/</href>'
                b'<propstat><prop><getcontentlength/></prop><status>HTTP/1.1 404</status></propstat>'
                b'<propstat><prop><resourcetype><collection/></resourcetype></prop></propstat></response>'
                b'<response><href>http://host/My%20Disc/a%2Bb.m2ts</href>'
                b'<propstat><prop><resourcetype/></prop></propstat>'
                b'<propstat><prop><getcontentlength>42</getcontentlength></prop></propstat></response>'
                b'</multistatus>')
        files = webdav.walk("http://me:pw@host/My Disc/", lambda url: body)
        self.assertEqual(files, {"a+b.m2ts": ("http://me:pw@host/My%20Disc/a%2Bb.m2ts", 42)})

    def test_read_ahead_grows_with_sequential_reading_not_with_a_glance(self):
        class Upstream:
            throttles, closed = 0, False

            def get(self, start, end):
                return media._Body(206, {}, bytes(end - start + 1))

            def close(self):
                pass

        mib = 1 << 20
        blocks = media.Blocks("http://host/f", size=1 << 30, upstream=Upstream(), bs=mib, streams=64)
        self.addCleanup(blocks.close)
        blocks.prefetch(500 * mib)  # a glance at one spot
        held = lambda: set(blocks.pending) | {i for _, i in blocks.memory.items}  # noqa: E731
        self.assertLessEqual(max(held()), 500 + 1 + blocks.run)
        for i in range(64):  # playback reading straight through
            blocks.prefetch(i * mib)
            blocks.read(i * mib, mib)
        self.assertGreaterEqual(max(held() - {500}), 63 + blocks.ahead // 2)

    def test_closing_wakes_a_read_stuck_on_a_silent_host(self):
        class Silent(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(206)
                self.send_header("Content-Range", "bytes 0-99/100")
                self.send_header("Content-Length", "100")
                self.end_headers()
                time.sleep(10)  # headers sent, body never comes

        server = ThreadingHTTPServer(("127.0.0.1", 0), Silent)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        upstream = media.Upstream(f"http://127.0.0.1:{server.server_port}/f")
        outcome = []

        def read():
            try:
                upstream.get(0, 99)
            except Exception as e:
                outcome.append(e)

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        time.sleep(0.5)
        started = time.monotonic()
        upstream.close()
        reader.join(5)
        self.assertLess(time.monotonic() - started, 2)
        self.assertIsInstance(outcome[0], OSError)

    def test_a_throttling_host_is_retried_and_counted(self):
        answers = [429, 206]

        class Throttling(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                status = answers.pop(0)
                self.send_response(status)
                if status == 429:
                    self.send_header("Retry-After", "0")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_header("Content-Range", "bytes 0-3/4")
                self.send_header("Content-Length", "4")
                self.end_headers()
                self.wfile.write(b"data")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Throttling)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        upstream = media.Upstream(f"http://127.0.0.1:{server.server_port}/f")
        self.addCleanup(upstream.close)
        with upstream.get(0, 3) as r:
            self.assertEqual(r.read(), b"data")
        self.assertEqual(upstream.throttles, 1)

    def test_a_failing_over_host_is_retried_and_counted(self):
        answers = [503, 502, 206]

        class Failover(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                status = answers.pop(0)
                self.send_response(status)
                if status == 206:
                    self.send_header("Content-Range", "bytes 0-3/4")
                    self.send_header("Content-Length", "4")
                    self.end_headers()
                    self.wfile.write(b"data")
                    return
                self.send_header("Retry-After", "0")
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Failover)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        upstream = media.Upstream(f"http://127.0.0.1:{server.server_port}/f")
        self.addCleanup(upstream.close)
        with upstream.get(0, 3) as r:
            self.assertEqual(r.read(), b"data")
        self.assertEqual(upstream.throttles, 2)

    def test_a_dead_host_stays_an_error(self):
        class Gone(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(500)
                self.send_header("Content-Length", "5")
                self.end_headers()
                self.wfile.write(b"oops!")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Gone)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        upstream = media.Upstream(f"http://127.0.0.1:{server.server_port}/f")
        self.addCleanup(upstream.close)
        with self.assertRaisesRegex(OSError, "500"):
            upstream.get(0, 3)

    def test_an_error_page_does_not_poison_the_next_range(self):
        answers = [404, 206]

        class Flaky(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                status = answers.pop(0)
                self.send_response(status)
                body = b"missing" if status == 404 else b"data"
                if status == 206:
                    self.send_header("Content-Range", "bytes 0-3/4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Flaky)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        upstream = media.Upstream(f"http://127.0.0.1:{server.server_port}/f")
        self.addCleanup(upstream.close)
        with self.assertRaisesRegex(OSError, "404"):
            upstream.get(0, 3)
        with upstream.get(0, 3) as r:  # the 404's body must not leak into this range
            self.assertEqual(r.read(), b"data")

    def test_a_closed_stream_stops_without_touching_the_network(self):
        upstream = media.Upstream("http://127.0.0.1:1/f")  # nothing listens here
        upstream.close()
        with self.assertRaisesRegex(OSError, "closed"):
            upstream.get(0, 99)

    def test_read_ahead_connections_grow_while_needed_and_halve_when_throttled(self):
        class Upstream:
            throttles, closed = 0, False

            def get(self, start, end):
                return media._Body(206, {}, bytes(end - start + 1))

            def close(self):
                pass

        blocks = media.Blocks("http://host/f", size=1 << 30, upstream=Upstream(), bs=1 << 20, streams=10)
        self.addCleanup(blocks.close)
        start = blocks._streams()
        for _ in range(200):  # more to fetch than connections, and every fetch succeeds
            blocks.starved = True
            blocks._adapt(True)
        self.assertEqual(blocks._streams(), 10)
        blocks._adapt(False)
        self.assertEqual(blocks._streams(), 5)
        for _ in range(200):  # succeeding with nothing waiting adds nothing
            blocks._adapt(True)
        self.assertEqual(blocks._streams(), 5)
        self.assertLess(start, 10)

    def test_a_share_holds_one_memory_budget_across_its_files(self):
        memory = media.Memory(10)
        for name in "abcdef":
            memory.put((name, 0), b"xxxx")
        self.assertLessEqual(memory.size, 10)
        self.assertEqual([key for key, _ in memory.items.items()], [("e", 0), ("f", 0)])
        self.assertEqual(memory.get(("e", 0)), b"xxxx")
        memory.put(("g", 0), b"xxxx")
        self.assertNotIn(("f", 0), memory)  # f was older than e once e was read
        self.assertIn(("e", 0), memory)

    def test_the_arrival_rate_counts_recent_fetches(self):
        class Upstream:
            throttles, closed = 0, False

            def get(self, start, end):
                return media._Body(206, {}, bytes(end - start + 1))

            def close(self):
                pass

        blocks = media.Blocks("http://host/f", size=64 << 20, upstream=Upstream(), bs=1 << 20)
        self.addCleanup(blocks.close)
        blocks.read(0, 8 << 20)
        self.assertAlmostEqual(blocks.rate(), (8 << 20) / media.RATE_WINDOW)
        blocks.arrivals[0] = (time.monotonic() - media.RATE_WINDOW - 1, blocks.arrivals[0][1])
        self.assertLess(blocks.rate(), (8 << 20) / media.RATE_WINDOW)

    def test_a_plain_web_server_is_not_taken_for_a_share(self):
        with self.assertRaisesRegex(OSError, "WebDAV"):
            webdav.walk("http://127.0.0.1:1/", lambda url: b"<html><br></html>")  # invalid XML
        with self.assertRaisesRegex(OSError, "WebDAV"):  # valid XML, but nothing DAV in it
            webdav.walk("http://127.0.0.1:1/", lambda url: b"<html><body>hello</body></html>")

    def test_the_walk_stops_rather_than_following_a_share_without_end(self):
        def endless(url):
            return (b'<?xml version="1.0"?><multistatus xmlns="DAV:">'
                    b'<response><href>/deep/</href><propstat><prop>'
                    b'<resourcetype><collection/></resourcetype></prop></propstat></response>'
                    b'</multistatus>')
        self.assertEqual(webdav.walk("http://host/", endless), {})


if __name__ == "__main__":
    unittest.main()
