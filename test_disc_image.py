"""Socket-free regressions for ISO9660 Blu-ray streaming and the HTTP view."""
import io
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from unittest.mock import patch

import pycdlib

import isoplay
from disc_image import CacheFile, project_iso9660


class FileCache:
    def __init__(self, fp):
        self.fp = fp
        self.size = fp.seek(0, 2)
        self.reads = []

    def read(self, pos, size):
        self.reads.append((pos, size))
        self.fp.seek(pos)
        return self.fp.read(size)


class FixtureISO(pycdlib.PyCdlib):
    """Write large media as a sparse file so tests never copy gigabytes."""

    def _output_file_data(self, outfp, blocksize, ino):
        length = ino.get_data_length()
        if length < 1 << 20:
            yield from super()._output_file_data(outfp, blocksize, ino)
            return
        start = ino.extent_location() * 2048
        for offset, data in ((0, b"START"), (4294965246, b"BOUNDARY"), (length - 3, b"END")):
            outfp.seek(start + offset)
            outfp.write(data)
        outfp.seek(start + ((length + 2047) // 2048) * 2048)
        yield length


def fixture(large=False, udf=False, nested=True):
    iso = FixtureISO()
    iso.new(interchange_level=3, udf="2.60" if udf else None)
    def directory(path):
        iso.add_directory(iso_path=path, **({"udf_path": path} if udf else {}))
    def file(path, data, length=None):
        iso.add_fp(io.BytesIO(data), len(data) if length is None else length, iso_path=path,
                   **({"udf_path": path.split(";")[0]} if udf else {}))
    for root, size in (("/BONUS", 20), ("/FEATURE", (5 << 30) + 37 if large else 100)) if nested else (("", 100),):
        if root:
            directory(root)
        directory(root + "/BDMV")
        directory(root + "/BDMV/STREAM")
        file(root + "/BDMV/INDEX.BDMV;1", b"INDX0200" + root.encode())
        file(root + "/BDMV/STREAM/00001.M2TS;1", bytes(size) if size < 1000 else b"", size)
    fp = tempfile.TemporaryFile()
    iso.write_fp(fp)
    iso.close()
    return fp


class ProjectionTests(unittest.TestCase):
    def test_nested_feature_and_multi_extent_payload(self):
        with fixture(large=True) as fp:
            cache = FileCache(fp)
            view = project_iso9660(cache)
            self.addCleanup(view.close)
            # Building the view must only read the container's metadata.
            self.assertLess(sum(n for _, n in cache.reads), 1 << 20)
            projected = pycdlib.PyCdlib()
            projected.open_fp(CacheFile(view))
            self.addCleanup(projected.close)
            with projected.open_file_from_iso(udf_path="/BDMV/index.bdmv") as index:
                self.assertEqual(index.read(), b"INDX0200/FEATURE")
            record = projected.get_record(udf_path="/BDMV/STREAM/00001.m2ts")
            self.assertEqual(record.get_data_length(), (5 << 30) + 37)
            with projected.open_file_from_iso(udf_path="/BDMV/STREAM/00001.m2ts") as video:
                self.assertEqual(video.read(5), b"START")
                video.seek(4294965246)
                self.assertEqual(video.read(8), b"BOUNDARY")
                video.seek(-3, 2)
                self.assertEqual(video.read(), b"END")
            # Exercise concurrent seeks at the server's positional-read seam.
            with ThreadPoolExecutor(4) as pool:
                self.assertTrue(all(pool.map(lambda _: view.read(32768, 6) == b"\x01CD001", range(16))))

    def test_root_disc(self):
        with fixture(nested=False) as fp:
            view = project_iso9660(FileCache(fp))
            self.addCleanup(view.close)
            iso = pycdlib.PyCdlib()
            iso.open_fp(CacheFile(view))
            self.addCleanup(iso.close)
            self.assertTrue(iso.has_udf())
            self.assertIsNotNone(iso.get_record(udf_path="/BDMV/index.bdmv"))

    def test_udf_passthrough(self):
        with fixture(udf=True) as fp:
            self.assertIsNone(project_iso9660(FileCache(fp)))

    def test_prepare_and_http_ranges_use_projected_size(self):
        class Server:
            server_port = 12345
            def __init__(self, *args): pass
            def serve_forever(self): pass
            def shutdown(self): pass
            def server_close(self): pass
        class Response(io.BytesIO):
            status = 206
        with fixture() as fp:
            fp.seek(0)
            data = fp.read()
            def get(cache, start, end):
                response = Response(data[start:end + 1])
                response.headers = {"Content-Range": f"bytes {start}-{end}/{len(data)}"}
                return response
            with patch.object(isoplay, "ThreadingHTTPServer", Server), patch.object(isoplay.RangeCache, "_get", get):
                _, kind, cache = isoplay.prepare("https://example.com/container.iso")
            try:
                self.assertEqual(kind, "bluray")
                self.assertIsNotNone(cache.view)
                size = cache.view.size
                self.assertNotEqual(size, cache.size)
                for command, rng in (("GET", "bytes=32768-32775"), ("GET", "bytes=-40"),
                                     ("HEAD", None), ("GET", f"bytes={size}-")):
                    h = cache._handler().__new__(cache._handler())
                    h.headers = Message()
                    if rng:
                        h.headers["Range"] = rng
                    h.command, h.request_version, h.requestline = command, "HTTP/1.1", ""
                    h.wfile = io.BytesIO()
                    getattr(h, "do_" + command)()
                    headers, body = h.wfile.getvalue().split(b"\r\n\r\n", 1)
                    if rng == "bytes=32768-32775":
                        self.assertIn(f"Content-Range: bytes 32768-32775/{size}".encode(), headers)
                        self.assertEqual(body, cache.view.read(32768, 8))
                    elif rng == "bytes=-40":
                        self.assertEqual(body, cache.view.read(size - 40, 40))
                    elif command == "HEAD":
                        self.assertIn(f"Content-Length: {size}".encode(), headers)
                        self.assertEqual(body, b"")
                    else:
                        self.assertIn(b"416", headers)
            finally:
                cache.close()


if __name__ == "__main__":
    unittest.main()
