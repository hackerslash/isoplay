"""Engine-layer behaviour that needs no libVLC instance and no libmpv.

MpvEngine helpers import nothing from libmpv (the import only happens inside `binding()`, when
an engine is actually asked for), and both engines' property-reading methods are called unbound
on stubs, the same way test_player exercises the app.
"""
import os
import unittest
from unittest.mock import Mock, call

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

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
        self.assertEqual(track_label({"title": "Commentary", "lang": "eng"}), "Commentary · English")
        self.assertEqual(track_label({"lang": "jpn", "codec": "opus"}), "Japanese (Opus)")
        self.assertEqual(track_label({"codec": "ac3"}), "AC-3")  # a track with no language
        self.assertEqual(track_label({"lang": "eng", "codec": "hdmv_pgs_subtitle"}), "English (PGS)")
        self.assertEqual(track_label({"id": 7}), "")  # the caller falls back to Track 7

    def test_unknown_codes_pass_through_instead_of_vanishing(self):
        self.assertEqual(track_label({"lang": "qaa", "codec": "somecodec"}), "qaa (somecodec)")

    def test_repeated_languages_are_numbered_from_their_second_showing(self):
        tracks = [{"type": "audio", "id": i, "lang": "spa", "codec": "ac3"} for i in (1, 2)]
        rows, _ = track_entries(tracks, "audio", 1)
        self.assertEqual([label for _, label in rows],
                         ["Off", "Spanish (AC-3)", "Spanish (AC-3) (2)"])


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
    import queue as _queue
    import threading as _threading
    engine = MpvEngine.__new__(MpvEngine)
    engine.player = _Player(**props)
    engine._mouse_lock = _threading.Lock()  # hover queue, like __init__ but with no worker
    engine._mouse_queue = _queue.Queue()
    engine._mouse_gen = 0
    engine._mouse_alive = True
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
        self.assertEqual(tracks["audio"], ([(-1, "Off"), (1, "English")], 1))
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

    def test_cache_fill_mid_seek_reports_progress_not_a_clear(self):
        _, player, events = self.engine()
        self.fire(player, "seeking", True)
        self.fire(player, "cache-buffering-state", 35)  # data actually arriving mid-stall
        self.assertEqual(events, ["buffer:0", "buffer:35"])
        self.fire(player, "cache-buffering-state", 100)  # stale full-reading: held back
        self.assertEqual(events, ["buffer:0", "buffer:35"])
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


class MpvMouseTests(unittest.TestCase):
    """Disc-menu clicks go through discnav with normalized 0..1 coordinates, never pixels:
    mouse-pos is in OSD units, so FBO pixels misland."""

    def test_click_and_move_reach_discnav_normalized(self):
        engine, player = _unbound()
        MpvEngine.menu_mouse(engine, 0.5, 0.25, True)
        self.assertEqual(player.commands[-2], ("discnav", "mouse-move", "0.5000", "0.2500"))
        # then activates exactly the way Enter does: mouse-click's own activation silently
        # no-ops on discs where its lookup misses, while select is the proven path
        self.assertEqual(player.commands[-1], ("discnav", "select"))
        MpvEngine.menu_mouse(engine, 0.5, 0.25, False)
        self.assertEqual(player.commands[-1], ("discnav", "mouse-move", "0.5000", "0.2500"))

    def test_src_override_counterscales_a_wrong_mpv_base(self):
        import os as _os
        engine, player = _unbound(video_params={"w": 720, "h": 480})
        _os.environ["ISOPLAY_MPV_SRC"] = "720x576"  # mpv stuck on the PAL default
        try:
            MpvEngine.menu_mouse(engine, 0.5, 0.39, False)
        finally:
            del _os.environ["ISOPLAY_MPV_SRC"]
        # mpv multiplies by its 576-line base: 0.39 * 480/576 lands back on row 187
        self.assertEqual(player.commands[-1], ("discnav", "mouse-move", "0.5000", "0.3250"))

    def test_positions_outside_the_window_land_on_an_edge(self):
        engine, player = _unbound()
        MpvEngine.menu_mouse(engine, 2.0, -1.0, True)
        self.assertEqual(player.commands[-2], ("discnav", "mouse-move", "1.0000", "0.0000"))
        self.assertEqual(player.commands[-1], ("discnav", "select"))

    def test_widget_normalizes_pixels_and_ignores_non_menus(self):
        from isoplay.mpv import MpvVideoWidget
        QApplication.instance() or QApplication([])
        engine = Mock()
        engine.menu_active.return_value = True
        engine.video_rect.return_value = (0.0, 0.0, 200.0, 100.0)
        widget = MpvVideoWidget(engine, None)
        widget.resize(200, 100)
        widget.mouseMoveEvent(QMouseEvent(QEvent.Type.MouseMove, QPointF(50, 25),
                                          Qt.NoButton, Qt.NoButton, Qt.NoModifier))
        engine.menu_mouse_async.assert_called_once_with(0.25, 0.25)
        engine.menu_active.return_value = False  # normal playback: nothing to forward
        engine.menu_mouse.reset_mock()
        widget.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(10, 10),
                                           Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
        engine.menu_mouse.assert_not_called()

    def test_clicks_on_the_letterbox_bars_never_reach_the_disc(self):
        from isoplay.mpv import MpvVideoWidget
        QApplication.instance() or QApplication([])
        engine = Mock()
        engine.menu_active.return_value = True
        engine.video_rect.return_value = (140.0, 0.0, 840.0, 630.0)  # 4:3 menu in a wide window
        widget = MpvVideoWidget(engine, None)
        widget.resize(1120, 630)
        widget.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(70, 315),
                                           Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
        engine.menu_mouse.assert_not_called()
        widget.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(560, 315),
                                           Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
        engine.menu_mouse.assert_called_once_with(0.5, 0.5, True)

    def test_hover_moves_coalesce_to_the_latest_position(self):
        from isoplay.mpv import MpvVideoWidget
        QApplication.instance() or QApplication([])
        engine = Mock()
        engine.menu_active.return_value = True
        engine.video_rect.return_value = (0.0, 0.0, 200.0, 100.0)
        widget = MpvVideoWidget(engine, None)
        widget.resize(200, 100)

        def move(x, y):
            widget.mouseMoveEvent(QMouseEvent(QEvent.Type.MouseMove, QPointF(x, y),
                                              Qt.NoButton, Qt.NoButton, Qt.NoModifier))

        move(50, 25)
        engine.menu_mouse_async.assert_called_once_with(0.25, 0.25)
        move(50, 25)  # no movement: nothing to say
        move(60, 30)  # still inside the coalescing window: held back
        engine.menu_mouse_async.assert_called_once_with(0.25, 0.25)
        widget.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(60, 30),
                                           Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
        engine.menu_mouse.assert_called_with(0.3, 0.3, True)  # clicks stay synchronous

    def test_the_worker_forwards_only_the_latest_hover(self):
        import queue
        import threading
        import time
        engine, player = _unbound()
        engine._mouse_queue.put_nowait((0.1, 0.1, 0))
        engine._mouse_queue.put_nowait((0.2, 0.2, 0))
        engine._mouse_gen = 1  # a click landed since: both queued hovers are stale
        engine._mouse_queue.put_nowait((0.5, 0.5, 1))
        worker = threading.Thread(target=MpvEngine._mouse_drain, args=(engine,), daemon=True)
        worker.start()
        deadline = time.monotonic() + 5
        while ("discnav", "mouse-move", "0.5000", "0.5000") not in player.commands:
            assert time.monotonic() < deadline, "the worker never forwarded the hover"
            time.sleep(0.02)
        engine._mouse_alive = False
        engine._mouse_queue.put_nowait(None)
        worker.join(timeout=5)
        moves = [c for c in player.commands if c[1] == "mouse-move"]
        self.assertEqual(moves, [("discnav", "mouse-move", "0.5000", "0.5000")])

    def test_video_rect_letterboxes_fit_and_covers_fill(self):
        from isoplay.mpv import MpvEngine as E
        engine, _ = _unbound(video_params={"dw": 853, "dh": 480, "w": 720, "h": 480})
        E.set_fit(engine, "fit", 1120, 630)
        ox, oy, dw, dh = E.video_rect(engine, 1120, 630)  # same shape: no bars
        self.assertAlmostEqual(ox, 0.0, delta=1.0)
        self.assertAlmostEqual(dw, 1120.0, delta=1.0)
        engine2, _ = _unbound(video_params={"dw": 640, "dh": 480, "w": 720, "h": 480})
        E.set_fit(engine2, "fit", 1120, 630)  # a 4:3 menu pillarboxed in the wide window
        self.assertEqual(E.video_rect(engine2, 1120, 630), (140.0, 0.0, 840.0, 630.0))
        E.set_fit(engine2, "fill", 1120, 630)  # cropped, so it spills past the widget
        ox, oy, dw, dh = E.video_rect(engine2, 1120, 630)
        self.assertLess(oy, 0.0)
        self.assertGreater(dh, 630.0)
        E.set_fit(engine2, "stretch", 1120, 630)
        self.assertEqual(E.video_rect(engine2, 1120, 630), (0.0, 0.0, 1120.0, 630.0))
        self.assertEqual(E.video_rect(_unbound()[0], 1120, 630),  # nothing playing yet
                         (0.0, 0.0, 1120.0, 630.0))


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
