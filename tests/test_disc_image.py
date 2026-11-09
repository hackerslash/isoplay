"""Socket-free regressions for ISO9660 Blu-ray streaming and the HTTP view."""
import io
import os
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from unittest.mock import Mock, patch
from types import SimpleNamespace

import pycdlib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from isoplay import media
from isoplay.app import Player
from isoplay.disc_image import CacheFile, project_iso9660, project_media, project_zip
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


def iso_fixture(files):
    """An ISO9660 image holding exactly the given {path: payload}."""
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3)
    for path, data in files.items():
        parent = path.rpartition("/")[0]
        if parent and parent not in [d for d, _, _ in iso.walk(iso_path="/")]:
            iso.add_directory(iso_path=parent)
        iso.add_fp(io.BytesIO(data), len(data), iso_path=path + ";1")
    fp = tempfile.TemporaryFile()
    iso.write_fp(fp)
    iso.close()
    return fp


def zip_fixture(names, compress=zipfile.ZIP_STORED):
    fp = tempfile.TemporaryFile()
    with zipfile.ZipFile(fp, "w", compress) as z:
        for name in names:
            z.writestr(name, b"\0" * 4096)
    return fp


class Server:
    """Stand in for the loopback server so the suite stays socket-free."""
    server_port = 12345
    def __init__(self, *args): pass
    def serve_forever(self): pass
    def shutdown(self): pass
    def server_close(self): pass


class ZipProjectionTests(unittest.TestCase):
    DVD = ["Movie Rip/VIDEO_TS.IFO", "Movie Rip/VTS_01_0.IFO", "Movie Rip/VTS_01_1.VOB", "Movie Rip/notes.txt"]

    def test_loose_dvd_files_are_republished_under_video_ts(self):
        with zip_fixture(self.DVD) as fp:
            view = project_zip(FileCache(fp))
        self.assertEqual(view.kind, "dvd")
        self.assertEqual(view.disc_root, "Movie Rip")
        self.assertEqual(sorted(next(view.udf.walk(udf_path="/VIDEO_TS"))[2]),
                         ["VIDEO_TS.IFO", "VTS_01_0.IFO", "VTS_01_1.VOB"])  # notes.txt left out

    def test_payload_is_read_straight_from_the_archive(self):
        with zip_fixture(["Rip/VIDEO_TS.IFO", "Rip/VTS_01_1.VOB"]) as fp:
            cache = FileCache(fp)
            view = project_zip(cache)
            fp.seek(0)
            raw = fp.read()
            # Every byte the image serves for a file must be the archive's own bytes, uncopied.
            for start, length, _ in view.files:
                self.assertEqual(view.read(start, length), b"\0" * 4096)
            self.assertLess(view.size, len(raw) + (1 << 20))

    def test_a_compressed_disc_is_refused_rather_than_half_played(self):
        with zip_fixture(self.DVD, zipfile.ZIP_DEFLATED) as fp:
            with self.assertRaises(OSError) as caught:
                project_zip(FileCache(fp))
        self.assertIn("compressed", str(caught.exception))

    def test_a_zip_of_loose_media_plays_its_biggest_file(self):
        with zip_fixture(["holiday/notes.txt", "holiday/clip.mp4"]) as fp:
            view = project_zip(FileCache(fp))
            self.addCleanup(view.close)
            self.assertEqual((view.name, view.kind, view.size), ("clip.mp4", None, 4096))
            self.assertEqual(view.read(0, 4096), b"\0" * 4096)

    def test_a_zip_with_nothing_playable_is_refused(self):
        with zip_fixture(["holiday/notes.txt"]) as fp:
            with self.assertRaises(OSError) as caught:
                project_zip(FileCache(fp))
        self.assertIn("No playable media", str(caught.exception))

    def test_a_compressed_video_says_so_rather_than_claiming_there_is_none(self):
        with zip_fixture(["holiday/clip.mp4"], zipfile.ZIP_DEFLATED) as fp:
            with self.assertRaises(OSError) as caught:
                project_zip(FileCache(fp))
        self.assertIn("compressed", str(caught.exception))


class TitleLiftTests(unittest.TestCase):
    """Formats mpv plays but cannot navigate: the main title is served as itself."""

    def test_hd_dvd_joins_its_own_evo_set_only(self):
        with iso_fixture({"/HVDVD_TS/HV000M01.EVO": b"trailer",
                          "/HVDVD_TS/HV001M01.EVO": b"feature-one" * 40,
                          "/HVDVD_TS/HV001M02.EVO": b"feature-two" * 10,
                          "/HVDVD_TS/HV001I01.IFO": b"ifo"}) as fp:
            view = project_media(FileCache(fp))
            self.addCleanup(view.close)
            self.assertEqual(view.name, "HV001M01.evo")  # HV000M01 is a different title
            self.assertEqual(view.read(0, view.size), b"feature-one" * 40 + b"feature-two" * 10)

    def test_video_cd_concatenates_its_sequences(self):
        with iso_fixture({"/MPEGAV/AVSEQ01.DAT": b"one", "/MPEGAV/AVSEQ02.DAT": b"two"}) as fp:
            view = project_media(FileCache(fp))
            self.addCleanup(view.close)
            self.assertEqual((view.name, view.read(0, view.size)), ("AVSEQ01.dat", b"onetwo"))

    def test_an_image_with_nothing_playable_is_left_to_mpv(self):
        with iso_fixture({"/README.TXT": b"nothing here"}) as fp:
            self.assertIsNone(project_media(FileCache(fp)))

    def _local(self, name, fp):
        path = os.path.join(tempfile.mkdtemp(), name)
        fp.seek(0)
        with open(path, "wb") as out:
            out.write(fp.read())
        with patch.object(media, "ThreadingHTTPServer", Server):
            result = media.prepare(path)
        self.addCleanup(result[2].close)
        return result

    def test_a_local_zip_no_longer_has_to_be_extracted(self):
        with zip_fixture(["Rip/VIDEO_TS.IFO", "Rip/VTS_01_1.VOB"]) as fp:
            mrl, kind, _, _, root = self._local("rip.zip", fp)
        self.assertEqual((kind, root), ("dvd", "Rip"))
        self.assertEqual(mrl, "http://127.0.0.1:12345/disc.iso")

    def test_a_local_iso9660_bluray_gets_its_menus_like_a_linked_one(self):
        with fixture(nested=False) as fp:
            mrl, kind, proxy, _, _ = self._local("movie.iso", fp)
        self.assertEqual((kind, mrl), ("bluray", "http://127.0.0.1:12345/disc.iso"))
        projected = pycdlib.PyCdlib()
        projected.open_fp(CacheFile(proxy.view))
        self.addCleanup(projected.close)
        self.assertTrue(projected.has_udf())  # libbluray can read this, the file on disk it cannot

    def test_a_lifted_title_keeps_its_name_so_mpv_picks_the_demuxer(self):
        with iso_fixture({"/HVDVD_TS/HV001M01.EVO": b"feature"}) as fp:
            mrl, kind, proxy, _, _ = self._local("movie.iso", fp)
        self.assertEqual((kind, mrl), (None, "http://127.0.0.1:12345/HV001M01.evo"))
        self.assertEqual(proxy.view.read(0, 7), b"feature")

    def test_every_disc_in_the_archive_is_offered(self):
        with zip_fixture(["A/VIDEO_TS.IFO", "A/VTS_01_1.VOB", "B/BDMV/index.bdmv",
                          "B/BDMV/STREAM/00001.m2ts"]) as fp:
            view = project_zip(FileCache(fp))
        self.assertEqual(view.discs, ("A", "B"))
        with zip_fixture(["A/VIDEO_TS.IFO", "B/BDMV/index.bdmv"]) as fp:
            picked = project_zip(FileCache(fp), root="B")
        self.assertEqual((picked.kind, picked.disc_root), ("bluray", "B"))


class NestedImageTests(unittest.TestCase):
    """A release that ships the disc as an image inside the archive still gets its engine."""

    def zipped(self, name, fp):
        fp.seek(0)
        payload = fp.read()
        outer = tempfile.TemporaryFile()
        with zipfile.ZipFile(outer, "w", zipfile.ZIP_STORED) as z:
            z.writestr("readme.nfo", b"release notes")
            z.writestr(name, payload)
        return outer

    def test_a_bluray_iso_inside_a_zip_is_republished_as_udf(self):
        with fixture(nested=False) as inner, self.zipped("Movie/movie.iso", inner) as fp:
            view = project_zip(FileCache(fp))
            self.addCleanup(view.close)
            self.assertEqual(view.kind, "bluray")
            projected = pycdlib.PyCdlib()
            projected.open_fp(CacheFile(view))
            self.addCleanup(projected.close)
            self.assertIsNotNone(projected.get_record(udf_path="/BDMV/index.bdmv"))

    def test_a_udf_image_inside_a_zip_is_served_whole_so_its_menus_survive(self):
        with fixture(nested=False, udf=True) as inner, self.zipped("movie.iso", inner) as fp:
            inner.seek(0)
            raw = inner.read()
            view = project_zip(FileCache(fp))
            self.addCleanup(view.close)
            self.assertEqual((view.kind, view.name, view.size), ("bluray", "movie.iso", len(raw)))
            self.assertEqual(view.read(0, len(raw)), raw)

    def test_an_image_of_something_unnavigable_still_lifts_its_title(self):
        with iso_fixture({"/HVDVD_TS/HV001M01.EVO": b"feature"}) as inner, \
                self.zipped("hd.iso", inner) as fp:
            view = project_zip(FileCache(fp))
            self.addCleanup(view.close)
            self.assertEqual((view.kind, view.name), (None, "HV001M01.evo"))
            self.assertEqual(view.read(0, 7), b"feature")


class ProjectionTests(unittest.TestCase):
    def test_opening_menu_only_for_multiple_discs(self):
        for roots in (("/FEATURE", "/BONUS"), ("",), None):
            with self.subTest(roots=roots):
                proxy = SimpleNamespace(view=object()) if roots else None
                player = SimpleNamespace(token=1, ask_disc=True, current="image.iso", use_menus=True,
                                         chooser=Mock(), stack=Mock(), engine=Mock(),
                                         remember=Mock(), flash=Mock(), menu_btn=Mock(), act_discmenu=Mock(),
                                         apply_fit=Mock())
                result = ("http://127.0.0.1/disc.iso", "bluray" if roots else None, proxy,
                          roots or (), roots[0] if roots else None)
                Player.start(player, 1, result)
                if roots and len(roots) > 1:
                    player.engine.open.assert_not_called()
                    player.chooser.set_discs.assert_called_once_with(roots)
                    player.stack.setCurrentWidget.assert_called_once_with(player.chooser)
                    self.assertEqual(player.pending_disc, (1, result))
                    player.play = Mock()
                    Player.choose_disc(player, "/BONUS")
                    player.play.assert_called_once_with("image.iso", menus=True, disc_root="/BONUS")
                else:
                    player.chooser.set_discs.assert_not_called()
                    player.engine.open.assert_called_once()

    def test_choosing_default_disc_starts_without_asking_again(self):
        result = ("http://127.0.0.1/disc.iso", "bluray", None, ("/FEATURE",), "/FEATURE")
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
        _app = QApplication.instance() or QApplication([])
        menu = QMenu()
        calls = []
        player = SimpleNamespace(
            discs=("/THE_ODYSSEY", "/SPECIAL_FEATURES"), disc_root="/THE_ODYSSEY", current="https://example.com/container.iso", use_menus=True,
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
        class Response(io.BytesIO):
            status = 206
        with fixture() as fp:
            fp.seek(0)
            data = fp.read()
            def get(cache, start, end):
                response = Response(data[start:end + 1])
                response.headers = {"Content-Range": f"bytes {start}-{end}/{len(data)}"}
                return response
            with patch.object(media, "ThreadingHTTPServer", Server), patch.object(media.RangeCache, "_get", get), \
                    patch.object(media, "probe", lambda url: ("disc", None)):
                _, kind, cache, _, _ = media.prepare("https://example.com/container.iso")
            try:
                self.assertEqual(kind, "bluray")
                self.assertIsNotNone(cache.view)
                size = cache.view.size
                self.assertNotEqual(size, cache.size)
                handler = media._endpoint(cache)
                for command, rng in (("GET", "bytes=32768-32775"), ("GET", "bytes=-40"),
                                     ("HEAD", None), ("GET", f"bytes={size}-")):
                    h = handler.__new__(handler)
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
