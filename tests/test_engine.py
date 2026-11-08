"""Engine-layer behaviour that needs no libVLC instance and no libmpv.

MpvEngine helpers import nothing from libmpv (the import only happens inside `binding()`, when
an engine is actually asked for), and VlcEngine methods are called unbound on stubs, the same
way test_player exercises the app.
"""
import os
import unittest
from unittest.mock import Mock, call

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import vlc  # noqa: E402

from isoplay.engine import VlcEngine  # noqa: E402
from isoplay.mpv import end_to_event, fit_zoom, track_entries, track_label  # noqa: E402


class MpvEventTests(unittest.TestCase):
    def test_end_file_reasons_map_onto_app_events(self):
        self.assertEqual(end_to_event(0), "end")  # EOF
        self.assertEqual(end_to_event(4), "error")  # ERROR
        self.assertIsNone(end_to_event(1))  # RESTARTED: a menu hand-off, not an end
        self.assertIsNone(end_to_event(3))  # QUIT: the user closed it
        self.assertIsNone(end_to_event(None))


class MpvTrackTests(unittest.TestCase):
    def test_off_first_and_selected_alongside_it(self):
        tracks = [{"type": "audio", "id": 1, "lang": "eng", "codec": "dts", "selected": False},
                  {"type": "audio", "id": 2, "title": "Commentary", "lang": "eng", "selected": True},
                  {"type": "video", "id": 0, "selected": True}]  # never listed
        rows, current = track_entries(tracks, "audio", 1)
        self.assertEqual([tid for tid, _ in rows], [-1, 1, 2])
        self.assertEqual(current, 2)

    def test_the_menu_lever_is_the_argument_when_nothing_is_selected(self):
        rows, current = track_entries([{"type": "sub", "id": 3, "lang": "fre"}], "sub", 3)
        self.assertEqual(current, 3)
        self.assertEqual(rows[0], (-1, "Off"))

    def test_other_kinds_are_kept_out(self):
        rows, _ = track_entries([{"type": "video", "id": 1}], "sub", -1)
        self.assertEqual(rows, [(-1, "Off")])

    def test_labels_use_what_the_disc_gives(self):
        self.assertEqual(track_label({"title": "Commentary", "lang": "eng"}), "Commentary eng")
        self.assertEqual(track_label({"lang": "jpn", "codec": "opus"}), "jpn opus")
        self.assertEqual(track_label({"id": 7}), "")  # the caller falls back to Track 7


class MpvFitTests(unittest.TestCase):
    def test_fill_zooms_to_cover(self):
        self.assertAlmostEqual(fit_zoom(1600, 900, 800, 900), 2.0)  # wide window, 1:1 material
        self.assertAlmostEqual(fit_zoom(1600, 900, 1600, 900), 1.0)  # same shape needs no zoom
        self.assertEqual(fit_zoom(0, 0, 1280, 720), 0.0)  # nothing is playing yet


class VlcMenuTests(unittest.TestCase):
    def engine(self, flags):
        player = Mock()
        player.get_full_title_descriptions.return_value = [Mock(flags=f) for f in flags]
        player.get_title.return_value = 0
        engine = VlcEngine.__new__(VlcEngine)
        engine._menu_titles, engine.mp = None, player
        return engine, player

    def test_flag_1_is_a_menu_and_the_list_is_read_once(self):
        engine, player = self.engine([3, 0])  # flags 3: menu and interactive; 0: a plain film
        self.assertTrue(VlcEngine.menu_active(engine))  # title 0 has the menu flag
        player.get_title.return_value = 1
        self.assertFalse(VlcEngine.menu_active(engine))
        self.assertEqual(player.get_full_title_descriptions.call_count, 1)  # cached; python-vlc leaks it

    def test_no_titles_yet_is_not_a_menu(self):
        engine, _ = self.engine([])
        self.assertFalse(VlcEngine.menu_active(engine))


class VlcTracksTests(unittest.TestCase):
    def engine(self):
        engine = VlcEngine.__new__(VlcEngine)
        engine.mp = Mock()
        engine.mp.audio_get_track.return_value = 2
        engine.mp.audio_get_track_description.return_value = [(2, "German"), (1, b"French")]
        engine.mp.video_get_spu.return_value = -1
        engine.mp.video_get_spu_description.return_value = [(-1, "Off"), (5, "English")]
        return engine

    def test_the_track_menu_shape_survives_unchanged(self):
        tracks = VlcEngine.tracks(self.engine())
        self.assertEqual(tracks["audio"], ([(2, "German"), (1, "French")], 2))
        self.assertEqual(tracks["sub"], ([(-1, "Off"), (5, "English")], -1))

    def test_selection_goes_to_the_matching_lever(self):
        engine = self.engine()
        VlcEngine.select_track(engine, "audio", 1)
        self.assertEqual(engine.mp.audio_set_track.call_args, call(1))
        VlcEngine.select_track(engine, "sub", -1)
        self.assertEqual(engine.mp.video_set_spu.call_args, call(-1))


class VlcFitTests(unittest.TestCase):
    def test_each_mode_uses_only_its_own_lever(self):
        engine = VlcEngine.__new__(VlcEngine)
        engine.mp = Mock()
        for mode, aspect, crop in [("fit", None, None),
                                   ("fill", None, "1600:900"),
                                   ("stretch", "1600:900", None)]:
            with self.subTest(mode=mode):
                VlcEngine.set_fit(engine, mode, 1600, 900)
                self.assertEqual(engine.mp.video_set_aspect_ratio.call_args, call(aspect))
                self.assertEqual(engine.mp.video_set_crop_geometry.call_args, call(crop))

    def test_a_collapsed_window_never_asks_for_a_zero_ratio(self):
        engine = VlcEngine.__new__(VlcEngine)
        engine.mp = Mock()
        VlcEngine.set_fit(engine, "fill", 0, 0)
        self.assertEqual(engine.mp.video_set_crop_geometry.call_args, call("1:1"))


class VlcNavigateTests(unittest.TestCase):
    def test_actions_reach_the_vlc_navigate_mode(self):
        engine = VlcEngine.__new__(VlcEngine)
        engine.mp = Mock()
        VlcEngine.navigate(engine, "select")
        self.assertEqual(engine.mp.navigate.call_args, call(vlc.NavigateMode.activate))
        VlcEngine.navigate(engine, "popup")
        self.assertEqual(engine.mp.navigate.call_args, call(vlc.NavigateMode.popup))

    def test_libvlc_cannot_be_given_the_mouse(self):
        self.assertIsNone(VlcEngine.menu_mouse(VlcEngine.__new__(VlcEngine), 10, 10, True))


if __name__ == "__main__":
    unittest.main()
