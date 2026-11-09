import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from isoplay import media


def partial(start, end, total, body, **extra):
    headers = {"Content-Range": f"bytes {start}-{end}/{total}"}
    headers.update(extra.pop("headers", {}))
    return {"status": 206, "body": body, "headers": headers, **extra}


class UpstreamTests(unittest.TestCase):
    def setUp(self):
        backoff = media.BACKOFF
        media.BACKOFF = 0
        self.addCleanup(setattr, media, "BACKOFF", backoff)

    def serve(self, steps):
        requests = []

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                requests.append({"path": self.path, "range": self.headers.get("Range"),
                                 "auth": self.headers.get("Authorization"),
                                 "if_range": self.headers.get("If-Range")})
                step = steps.pop(0) if steps else {"status": 500}
                body = step.get("body", b"")
                self.send_response(step.get("status", 200))
                for name, value in step.get("headers", {}).items():
                    self.send_header(name, value)
                length = step.get("length", len(body))
                if length is not None:
                    self.send_header("Content-Length", str(length))
                self.end_headers()
                if step.get("event"):
                    step["event"].set()
                if step.get("sleep"):
                    time.sleep(step["sleep"])
                try:
                    if body:
                        self.wfile.write(body)
                        self.wfile.flush()
                    if step.get("cut"):
                        self.close_connection = True
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                except OSError:
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        server.daemon_threads = True
        server.handle_error = lambda *a: None
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}", requests

    def upstream(self, url):
        upstream = media.Upstream(url)
        self.addCleanup(upstream.close)
        return upstream

    def test_throttling_is_retried_and_counted(self):
        base, requests = self.serve([
            {"status": 503, "headers": {"Retry-After": "0"}},
            partial(0, 3, 4, b"data"),
        ])
        upstream = self.upstream(base + "/file")
        with upstream.get(0, 3) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), b"data")
        self.assertEqual(upstream.throttles, 1)
        self.assertEqual(len(requests), 2)

    def test_redirect_is_remembered_and_expired_target_goes_back_to_origin(self):
        base, requests = self.serve([
            {"status": 302, "headers": {"Location": "/cdn"}},
            partial(0, 2, 3, b"abc"),
            partial(0, 2, 3, b"abc"),
            {"status": 410},
            partial(0, 2, 3, b"abc"),
        ])
        upstream = self.upstream(base + "/origin")
        with upstream.get(0, 2) as response:
            self.assertEqual(response.read(), b"abc")
        self.assertEqual(upstream.url, base + "/cdn")
        with upstream.get(0, 2) as response:
            self.assertEqual(response.read(), b"abc")
        self.assertEqual([r["path"] for r in requests], ["/origin", "/cdn", "/cdn"])
        with upstream.get(0, 2) as response:
            self.assertEqual(response.read(), b"abc")
        self.assertEqual([r["path"] for r in requests], ["/origin", "/cdn", "/cdn", "/cdn", "/origin"])
        self.assertEqual(upstream.url, base + "/origin")

    def test_a_cut_body_resumes_where_it_stopped(self):
        base, requests = self.serve([
            partial(2, 9, 10, b"cdef", length=8, cut=True),
            partial(6, 9, 10, b"ghij"),
        ])
        upstream = self.upstream(base + "/file")
        with upstream.get(2, 9) as response:
            self.assertEqual(response.read(), b"cdefghij")
        self.assertEqual([r["range"] for r in requests], ["bytes=2-9", "bytes=6-9"])

    def test_a_mismatched_content_range_is_retried(self):
        base, requests = self.serve([
            partial(5, 9, 10, b"zzzzz"),  # headers don't match the range we asked for
            partial(0, 9, 10, b"abcdefghij"),
        ])
        upstream = self.upstream(base + "/file")
        with upstream.get(0, 9) as response:
            self.assertEqual(response.read(), b"abcdefghij")
        self.assertEqual([r["range"] for r in requests], ["bytes=0-9", "bytes=0-9"])

    def test_a_wrong_content_range_is_not_accepted(self):
        base, requests = self.serve([partial(1, 3, 4, b"bcd") for _ in range(6)])
        upstream = self.upstream(base + "/file")
        with self.assertRaises(OSError):
            upstream.get(0, 3)
        self.assertEqual(len(requests), 6)

    def test_a_changed_etag_stops_playback(self):
        base, requests = self.serve([
            partial(0, 3, 4, b"data", headers={"ETag": '"one"'}),
            partial(0, 3, 4, b"data", headers={"ETag": '"two"'}),
        ])
        upstream = self.upstream(base + "/file")
        with upstream.get(0, 3) as response:
            self.assertEqual(response.read(), b"data")
        with self.assertRaisesRegex(OSError, "changed"):
            upstream.get(0, 3)
        self.assertEqual(requests[1]["if_range"], '"one"')
        self.assertEqual(len(requests), 2)

    def test_credentials_are_not_forwarded_to_another_host(self):
        """A redirect to another host drops the link's user:pass rather than leaking them."""
        requests = []

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                requests.append({"host": self.headers.get("Host"), "auth": self.headers.get("Authorization")})
                if self.path == "/origin":
                    self.send_response(302)
                    self.send_header("Location", self.alt)  # another host: same listener, other name
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    self.send_response(206)
                    self.send_header("Content-Range", "bytes 0-3/4")
                    self.send_header("Content-Length", "4")
                    self.end_headers()
                    self.wfile.write(b"data")

        server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        server.daemon_threads = True
        H.alt = f"http://localhost:{server.server_port}/"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        upstream = self.upstream(f"http://u:p@127.0.0.1:{server.server_port}/origin")
        with upstream.get(0, 3) as r:
            self.assertEqual(r.read(), b"data")
        self.assertEqual(requests[0]["auth"], "Basic dTpw")  # sent to the origin
        self.assertIsNone(requests[1]["auth"])  # but not to the host it redirected to

    def test_close_wakes_a_blocked_body_read(self):
        started = threading.Event()
        base, _ = self.serve([partial(0, 99, 100, b"", length=100, sleep=10, event=started)])
        upstream = self.upstream(base + "/file")
        outcome = []

        def read():
            try:
                upstream.get(0, 99)
            except Exception as e:
                outcome.append(e)

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        self.assertTrue(started.wait(2))
        before = time.monotonic()
        upstream.close()
        reader.join(2)
        self.assertFalse(reader.is_alive())
        self.assertLess(time.monotonic() - before, 2)
        self.assertEqual(str(outcome[0]), "the stream was closed")

    def test_a_whole_file_answer_is_not_a_nonzero_range(self):
        base, requests = self.serve([{"status": 200, "body": b"abcdef"}])
        upstream = self.upstream(base + "/file")
        with self.assertRaisesRegex(OSError, "Range"):
            upstream.get(1, 3)
        self.assertEqual(len(requests), 1)


if __name__ == "__main__":
    unittest.main()
