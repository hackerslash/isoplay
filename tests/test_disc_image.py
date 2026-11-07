"""Socket-free regressions for ISO9660 Blu-ray streaming and the HTTP view."""
import io
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from unittest.mock import Mock, patch
from types import SimpleNamespace

import pycdlib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from isoplay import media
from isoplay.app import Player
from isoplay.disc_image import CacheFile, project_iso9660
from PySide6.QtWidgets import QApplication, QMenu


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
    def test_opening_menu_only_for_multiple_discs(self):
        for roots in (("/FEATURE", "/BONUS"), ("",), None):
            with self.subTest(roots=roots):
                view = SimpleNamespace(discs=roots, disc_root=roots[0]) if roots else None
                proxy = SimpleNamespace(view=view) if roots else None
                player = SimpleNamespace(token=1, ask_disc=True, current="image.iso", use_menus=True,
                                         chooser=Mock(), stack=Mock(), mp=Mock(), vlc=Mock(),
                                         remember=Mock(), flash=Mock(), menu_btn=Mock(), act_discmenu=Mock())
                result = ("http://127.0.0.1/disc.iso", "bluray" if roots else None, proxy)
                Player.start(player, 1, result)
                if roots and len(roots) > 1:
                    player.mp.play.assert_not_called()
                    player.chooser.set_discs.assert_called_once_with(roots)
                    player.stack.setCurrentWidget.assert_called_once_with(player.chooser)
                    self.assertEqual(player.pending_disc, (1, result))
                    player.play = Mock()
                    Player.choose_disc(player, "/BONUS")
                    player.play.assert_called_once_with("image.iso", menus=True, disc_root="/BONUS")
                else:
                    player.chooser.set_discs.assert_not_called()
                    player.mp.play.assert_called_once()

    def test_choosing_default_disc_starts_without_asking_again(self):
        result = ("http://127.0.0.1/disc.iso", "bluray", None)
        player = SimpleNamespace(pending_disc=(1, result), disc_root="/FEATURE", stack=Mock(),
                                 video=object(), start=Mock(), play=Mock())
        Player.choose_disc(player, "/FEATURE")
        player.start.assert_called_once_with(1, result)
        player.play.assert_not_called()
        self.assertIsNone(player.pending_disc)

    def test_select_bonus_disc_and_return_to_feature(self):
        with fixture() as fp:
            cache = FileCache(fp)
            for root, contents in ((None, b"INDX0200/FEATURE"),
                                   ("/BONUS", b"INDX0200/BONUS"),
                                   ("/FEATURE", b"INDX0200/FEATURE")):
                view = project_iso9660(cache, root=root)
                try:
                    self.assertEqual(view.discs, ("/FEATURE", "/BONUS"))
                    self.assertEqual(view.disc_root, root if root is not None else "/FEATURE")
                    iso = pycdlib.PyCdlib()
                    iso.open_fp(CacheFile(view))
                    try:
                        with iso.open_file_from_iso(udf_path="/BDMV/index.bdmv") as index:
                            self.assertEqual(index.read(), contents)
                    finally:
                        iso.close()
                finally:
                    view.close()

    def test_invalid_disc_selection_does_not_silently_play_feature(self):
        with fixture() as fp:
            with self.assertRaisesRegex(ValueError, "disc folder"):
                project_iso9660(FileCache(fp), root="/MISSING")

    def test_main_title_fallback_keeps_selected_disc(self):
        calls = []
        class Playback:
            current = "https://example.com/container.iso"
            disc_root = "/BONUS"
            use_menus = True
            def play(self, src, **options):
                calls.append((src, options))
        player = Playback()
        Player.replay_main(player)
        self.assertEqual(calls, [(player.current, {"menus": False, "disc_root": "/BONUS"})])

    def test_disc_menu_action_switches_to_bonus(self):
        app = QApplication.instance() or QApplication([])
        menu = QMenu()
        calls = []
        player = SimpleNamespace(
            proxy=SimpleNamespace(view=SimpleNamespace(discs=("/THE_ODYSSEY", "/SPECIAL_FEATURES"))),
            disc_root="/THE_ODYSSEY", current="https://example.com/container.iso", use_menus=True,
            play=lambda src, **options: calls.append((src, options)),
        )
        player.switch_disc = lambda root: Player.switch_disc(player, root)
        Player.add_disc_choices(player, menu)
        choices = menu.actions()[0].menu()
        self.assertEqual(choices.title(), "Discs in This Image")
        self.assertEqual([a.text() for a in choices.actions()], ["The Odyssey", "Special Features"])
        self.assertTrue(choices.actions()[0].isChecked())
        choices.actions()[1].trigger()
        self.assertEqual(calls, [(player.current, {"menus": True, "disc_root": "/SPECIAL_FEATURES"})])
        menu.close()

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
            with patch.object(media, "ThreadingHTTPServer", Server), patch.object(media.RangeCache, "_get", get):
                _, kind, cache = media.prepare("https://example.com/container.iso")
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
