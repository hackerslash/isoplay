"""Engine-layer behaviour that needs no libVLC instance and no libmpv.

MpvEngine helpers import nothing from libmpv (the import only happens inside `binding()`, when
an engine is actually asked for), and both engines' property-reading methods are called unbound
on stubs, the same way test_player exercises the app.
"""
import os
import unittest
from unittest.mock import Mock, call

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import vlc  # noqa: E402

from isoplay.engine import VlcEngine  # noqa: E402
from isoplay.mpv import MpvEngine, end_to_event, fit_zoom, track_entries, track_label  # noqa: E402


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


class _Player:
    """Stand-in for a python-mpv MPV: properties by attribute name, options by subscript."""

    def __init__(self, **props):
        object.__setattr__(self, "_props", props)
        object.__setattr__(self, "_set", {})
        object.__setattr__(self, "commands", [])
        object.__setattr__(self, "observed", {})

    def __getattr__(self, name):
        try:
            return self._props[name]
        except KeyError as e:
            raise AttributeError(name) from e

    def __setattr__(self, name, value):
        self._set[name] = value  # mpv treats writes of both kinds the same

    def __setitem__(self, name, value):
        self._set[name] = value

    def observe_property(self, name, callback):
        self.observed[name] = callback

    def command(self, *args):
        self.commands.append(args)


def _unbound(**props):
    engine = MpvEngine.__new__(MpvEngine)
    engine.player = _Player(**props)
    return engine, engine.player


class MpvPropertiesTests(unittest.TestCase):
    """python-mpv reads properties by attribute (core_idle -> core-idle), not by subscripts,
    which reach options; the real spike run caught the engine reading the wrong layer."""

    def test_state_comes_from_properties_with_dash_names(self):
        engine, _ = _unbound(playlist_count=1, core_idle=False, percent_pos=42.0)
        self.assertTrue(engine.has_media())
        self.assertTrue(engine.playing())
        self.assertAlmostEqual(engine.position(), 0.42)

    def test_a_player_without_things_loaded_degrades_quietly(self):
        engine, _ = _unbound(playlist_count=0, core_idle=True,
                             time_pos=None, duration=None, percent_pos=None)  # mpv's idle answers
        self.assertFalse(engine.has_media())
        self.assertFalse(engine.playing())
        self.assertEqual(engine.position(), -1.0)
        self.assertEqual(engine.time(), 0)
        self.assertEqual(engine.length(), 0)

    def test_the_menu_flag_is_a_property(self):
        self.assertTrue(_unbound(disc_menu_active=1)[0].menu_active())
        self.assertFalse(_unbound()[0].menu_active())  # property not implemented yet: not a menu

    def test_chapters_map_like_libvlc_chapters_did(self):
        self.assertEqual(_unbound(chapters=4, chapter=2)[0].chapter_count(), 4)
        self.assertEqual(_unbound(chapters=None, chapter=None)[0].chapter_count(), 0)
        self.assertEqual(_unbound(chapter=-1)[0].chapter(), 0)  # before the first chapter

    def test_each_fit_mode_touches_its_own_levers(self):
        engine = _unbound(video_params={"w": 800, "h": 400})[0]
        MpvEngine.set_fit(engine, "fill", 800, 400)  # same shape as the window: no zoom left
        self.assertEqual(engine.player._set, {"video_aspect_override": "no", "video_zoom": 1.0})
        MpvEngine.set_fit(engine, "fit", 800, 400)  # mpv's own letterboxed fit, zero zoom
        self.assertEqual(engine.player._set, {"video_aspect_override": "no", "video_zoom": 0})
        MpvEngine.set_fit(engine, "stretch", 800, 400)
        self.assertEqual(engine.player._set, {"video_aspect_override": "800:400", "video_zoom": 0})

    def test_fill_without_a_picture_leaves_mpv_s_own_fit(self):
        engine, _ = _unbound()  # nothing is playing, no video-params to zoom against
        MpvEngine.set_fit(engine, "fill", 800, 400)
        self.assertEqual(engine.player._set, {"video_aspect_override": "no", "video_zoom": 0.0})

    def test_tracks_read_off_the_property_list(self):
        engine = _unbound(track_list=[{"type": "audio", "id": 1, "lang": "eng", "selected": True}],
                          aid=1, sid="no")[0]
        tracks = MpvEngine.tracks(engine)
        self.assertEqual(tracks["audio"], ([(-1, "Off"), (1, "eng")], 1))
        self.assertEqual(tracks["sub"][0], [(-1, "Off")])
        MpvEngine.select_track(engine, "sub", -1)
        self.assertEqual(engine.player._set, {"sid": "no"})


class MpvOpenTests(unittest.TestCase):
    """Discs go through bd:// or dvd:// with the source as the device, so libbluray and
    libdvdnav read it with their block callbacks -- local paths and loopback URLs alike."""

    def test_a_bluray_opens_in_menu_mode_through_the_disc_backend(self):
        engine, player = _unbound()
        MpvEngine.open(engine, "http://127.0.0.1:5/disc.iso", kind="bluray", menus=True)
        self.assertEqual(player._set["bluray-device"], "http://127.0.0.1:5/disc.iso")
        self.assertEqual(player._set["disc-menu"], "yes")
        self.assertEqual(player.commands[-1], ("loadfile", "bd://", "replace"))

    def test_a_dvd_without_menus_still_goes_through_dvdnav(self):
        engine, player = _unbound()
        MpvEngine.open(engine, "/discs/film.iso", kind="dvd", menus=False)
        self.assertEqual(player._set["dvd-device"], "/discs/film.iso")
        self.assertEqual(player._set["disc-menu"], "no")
        self.assertEqual(player.commands[-1], ("loadfile", "dvd://", "replace"))

    def test_a_plain_video_is_loaded_as_itself(self):
        engine, player = _unbound()
        MpvEngine.open(engine, "/films/a.mkv")
        self.assertEqual(player.commands[-1], ("loadfile", "/films/a.mkv", "replace"))
        self.assertNotIn("dvd-device", player._set)


class MpvNetworkTests(unittest.TestCase):
    """mpv's own network layer is tuned for isoplay's inputs: the loopback proxy behind discs
    and remote https straight behind plain videos."""

    def test_network_options_reach_the_player(self):
        engine, _ = _unbound()
        MpvEngine._apply_network_options(engine)
        self.assertEqual(engine.player._set["cache"], "yes")
        self.assertEqual(engine.player._set["demuxer-readahead-secs"], "10")
        self.assertEqual(engine.player._set["stream-buffer-size"], "1048576")
        self.assertEqual(engine.player._set["network-timeout"], "30")
        self.assertIn("reconnect", engine.player._set["stream-lavf-o"])
        self.assertEqual(engine.player._set["force-seekable"], "yes")

    def test_a_missing_option_does_not_sink_the_engine(self):
        engine, player = _unbound()
        real_setitem = _Player.__setitem__

        def fail_once(self, name, value):
            if name == "stream-lavf-o":
                raise RuntimeError("no such option")
            real_setitem(self, name, value)

        original = _Player.__setitem__
        _Player.__setitem__ = fail_once
        try:
            MpvEngine._apply_network_options(engine)  # must not raise
        finally:
            _Player.__setitem__ = original
        self.assertEqual(player._set["cache"], "yes")


class MpvBufferTests(unittest.TestCase):
    """Seeks and stalls collapse into one buffering state: seeking or stalled means waiting,
    whatever the cache percentage still reads."""

    def engine(self):
        engine, player = _unbound()
        events = []
        engine.emit = events.append
        engine._cache_state, engine._stalled, engine._seeking = None, False, False
        MpvEngine._observe(engine)
        self.assertEqual(set(player.observed),
                         {"paused-for-cache", "cache-buffering-state", "seeking", "video-format"})
        return engine, player, events

    def fire(self, player, name, value):
        player.observed[name](name, value)

    def test_a_seek_reports_waiting_then_filling(self):
        _, player, events = self.engine()
        self.fire(player, "seeking", True)  # small files have no cache events of their own
        self.assertEqual(events, ["buffer:0"])
        self.fire(player, "seeking", False)
        self.assertEqual(events, ["buffer:0", "buffer:100"])

    def test_a_full_cache_does_not_clear_a_seek(self):
        _, player, events = self.engine()
        self.fire(player, "seeking", True)
        self.fire(player, "cache-buffering-state", 100)
        self.assertEqual(events, ["buffer:0"])  # the seek still holds the spinner
        self.fire(player, "seeking", False)
        self.assertEqual(events[-1], "buffer:100")  # landing re-sends the fill it held back

    def test_a_stall_inside_a_seek_stays_waiting(self):
        _, player, events = self.engine()
        self.fire(player, "seeking", True)
        self.fire(player, "paused-for-cache", True)
        self.fire(player, "cache-buffering-state", 100)  # libbluray blocks on our callback at 100
        self.fire(player, "paused-for-cache", False)
        self.assertNotIn("buffer:100", events)  # nothing cleared the spinner mid-seek
        self.fire(player, "seeking", False)
        self.assertEqual(events[-1], "buffer:100")

    def test_open_resets_seek_and_stall_state(self):
        engine, player = _unbound()
        engine._stalled, engine._seeking, engine._cache_state, engine._vout = True, True, 50, True
        MpvEngine.open(engine, "/films/a.mkv")
        self.assertEqual((engine._stalled, engine._seeking, engine._cache_state), (False, False, None))

    def test_seeking_reads_the_property(self):
        self.assertTrue(MpvEngine.seeking(_unbound(seeking=True)[0]))
        self.assertFalse(MpvEngine.seeking(_unbound(seeking=False)[0]))
        self.assertFalse(MpvEngine.seeking(_unbound()[0]))  # unset while nothing plays


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
