"""Player behaviour that needs no VLC instance: methods are called unbound on a stub."""
import os
import time
import unittest
from unittest import mock
from unittest.mock import Mock, call
from email.message import Message
from urllib.error import HTTPError

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from isoplay.app import Player
from isoplay.media import prepare


def waiting(**overrides):
    player = Mock(buffering=False, osd_until=0.0, busy_until=float("inf"), disc="bluray", use_menus=True,
                  deadline=0.0, stalls=[], stall_from=0.0, seek_from=0.0, menu_seen=False, proxy=None)
    player.engine.time.return_value = 5000
    player.engine.length.return_value = 900_000
    player.engine.position.return_value = 0.5
    player.engine.playing.return_value = True
    player.engine.has_media.return_value = True
    player.engine.seeking.return_value = False
    player.engine.menu_active.return_value = False
    player.buffering_text = lambda: Player.buffering_text(player)
    player.menu_active = lambda: Player.menu_active(player)
    player.arriving = lambda: Player.arriving(player)
    player.__dict__.update(overrides)
    return player


class BufferingTests(unittest.TestCase):
    def test_buffering_does_not_interrupt_a_loading_message(self):
        player = waiting()
        Player.on_event(player, "buffer:0")
        player.flash.assert_not_called()

    def test_buffering_shows_once_nothing_else_is_waiting(self):
        player = waiting(busy_until=0.0)
        Player.on_event(player, "buffer:35")
        player.flash.assert_called_once_with("Buffering", sticky=True, busy=True, percent=35)

    def test_buffering_keeps_updating_once_it_has_started(self):
        player = waiting(buffering=True)
        Player.on_event(player, "buffer:60")
        player.flash.assert_called_once_with("Buffering", sticky=True, busy=True, percent=60)

    def stall(self, player, seconds):
        player.buffering = False
        Player.on_event(player, "buffer:10")
        player.stall_from -= seconds  # as if it had buffered that long
        Player.on_event(player, "buffer:100")
        player.buffering = False

    def test_repeated_long_stalls_say_the_connection_is_too_slow_with_the_rate_arriving(self):
        player = waiting(busy_until=0.0, proxy=Mock(rate=Mock(return_value=5_300_000)))
        for _ in range(3):
            self.stall(player, 5)
        Player.on_event(player, "buffer:10")
        text = player.flash.call_args.args[0]
        self.assertEqual(text, "Buffering\nSlow connection (5.3 MB/s)")

    def test_brief_buffering_after_seeks_and_menus_never_says_too_slow(self):
        player = waiting(busy_until=0.0, proxy=Mock(rate=Mock(return_value=5_300_000)))
        for _ in range(10):
            self.stall(player, 1)
        Player.on_event(player, "buffer:10")
        self.assertEqual(player.flash.call_args.args[0], "Buffering")

    def test_one_stall_is_just_buffering(self):
        player = waiting(busy_until=0.0, proxy=Mock(rate=Mock(return_value=5_300_000)))
        Player.on_event(player, "buffer:10")
        player.flash.assert_called_once_with("Buffering", sticky=True, busy=True, percent=10)

    def test_a_disc_menu_waiting_for_a_choice_is_not_buffering(self):
        player = waiting(busy_until=0.0)
        player.engine.menu_active.return_value = True  # the top menu is up
        Player.on_event(player, "buffer:0")
        player.flash.assert_not_called()
        self.assertEqual(player.stalls, [])

    def test_a_buffering_spinner_already_up_clears_when_the_menu_appears(self):
        player = waiting(buffering=True)
        player.engine.menu_active.return_value = True
        Player.on_event(player, "buffer:0")
        player.flash.assert_called_once_with("")

    def test_full_buffer_clears_only_after_buffering(self):
        player = waiting(buffering=True, busy_until=0.0)
        Player.on_event(player, "buffer:100")
        player.flash.assert_called_once_with("")

    def test_buffering_replaces_a_seek_position_toast(self):
        import time as _time
        player = waiting(busy_until=0.0, osd_until=_time.monotonic() + 1.2)  # "0:05 / 1:30" still up
        Player.on_event(player, "buffer:35")
        player.flash.assert_called_once_with("Buffering", sticky=True, busy=True, percent=35)

    def test_buffering_shows_for_a_seek_back_to_zero(self):
        player = waiting(busy_until=0.0)
        player.engine.time.return_value = 0  # time-pos resets while the seek lands
        Player.on_event(player, "buffer:10")
        player.flash.assert_called_once_with("Buffering", sticky=True, busy=True, percent=10)

    def test_a_stall_with_no_fill_counts_spins_without_a_number(self):
        player = waiting(busy_until=0.0)
        Player.on_event(player, "buffer:0")
        player.flash.assert_called_once_with("Buffering", sticky=True, busy=True, percent=None)


class SeekWatchdogTests(unittest.TestCase):
    """A seek mpv never reports as buffering (small files have no cache events) still earns
    the spinner from the tick, after instant seeks had their grace period."""

    def seeking(self, **overrides):
        overrides.setdefault("busy_until", 0.0)
        player = waiting(**overrides)
        player.engine.seeking.return_value = True
        player.controls.isVisible.return_value = False  # skip the play/pause icon refresh
        return player

    def test_a_silent_seek_arms_quietly_then_shows_the_spinner(self):
        player = self.seeking()
        Player.tick(player)  # first sighting: start the clock, show nothing yet
        player.flash.assert_not_called()
        self.assertNotEqual(player.seek_from, 0.0)
        player.seek_from -= 1.0  # the seek has been hanging past its grace period
        Player.tick(player)
        player.flash.assert_called_once_with("Buffering", sticky=True, busy=True, percent=None)
        self.assertTrue(player.buffering)

    def test_a_finished_seek_resets_the_clock(self):
        player = self.seeking()
        Player.tick(player)
        player.engine.seeking.return_value = False
        Player.tick(player)
        player.flash.assert_not_called()
        self.assertEqual(player.seek_from, 0.0)

    def test_no_watchdog_while_an_open_is_already_showing(self):
        player = self.seeking(busy_until=float("inf"))  # "Loading" owns the screen
        player.seek_from = time.monotonic() - 1.0
        Player.tick(player)
        player.flash.assert_not_called()


class StuckMenuTests(unittest.TestCase):
    def stuck(self, length, frozen_for, opened_ago=10):
        now = time.monotonic()
        player = waiting(opened_at=now - opened_ago, stuck_at=(length - 400, now - frozen_for), notice=None)
        player.engine.length.return_value = length
        player.engine.time.return_value = length - 400
        player.engine.position.return_value = 1.0
        Player.watch_menu(player, now)
        return player

    def test_a_feature_frozen_at_its_end_just_after_opening_falls_back_to_the_main_title(self):
        player = self.stuck(9_111_800, frozen_for=9)
        player.replay_main.assert_called_once()
        self.assertIn("main title", player.notice)

    def test_not_before_it_has_been_frozen_a_while(self):
        self.stuck(9_111_800, frozen_for=2).replay_main.assert_not_called()

    def test_a_short_menu_clip_holding_its_last_frame_is_left_alone(self):
        self.stuck(45_000, frozen_for=60).replay_main.assert_not_called()

    def test_a_film_that_really_reached_its_end_is_left_alone(self):
        self.stuck(9_111_800, frozen_for=60, opened_ago=7200).replay_main.assert_not_called()

    def test_a_menu_that_ran_once_is_loading_not_dead(self):
        player = self.stuck(9_111_800, frozen_for=60)
        player.menu_seen = True  # disc-menu-active was true: BD-J boot, not a still failure
        player.replay_main.reset_mock()
        Player.watch_menu(player, time.monotonic())
        player.replay_main.assert_not_called()

    def test_a_menu_still_pulling_data_gets_more_time(self):
        player = self.stuck(9_111_800, frozen_for=60)
        player.proxy = Mock(rate=Mock(return_value=200_000))  # intros/JARs still arriving
        player.replay_main.reset_mock()
        Player.watch_menu(player, time.monotonic())
        player.replay_main.assert_not_called()
        self.assertEqual(player.stuck_at[0], player.engine.time.return_value)


class StallTests(unittest.TestCase):
    def test_a_black_screen_past_the_deadline_fails_the_open(self):
        player = waiting(deadline=time.monotonic() - 1, current="http://host/x.bin",
                         engine=Mock(time=Mock(return_value=0)))
        Player.tick(player)
        player.fail.assert_called_once()

    def test_a_slow_host_still_sending_data_gets_more_time(self):
        player = waiting(deadline=time.monotonic() - 1, current="http://host/x.iso",
                         proxy=Mock(rate=Mock(return_value=200_000)),
                         engine=Mock(time=Mock(return_value=0)))
        player.controls.isVisible.return_value = False
        Player.tick(player)
        player.fail.assert_not_called()
        self.assertGreater(player.deadline, time.monotonic())

    def test_a_host_that_stopped_sending_still_fails(self):
        player = waiting(deadline=time.monotonic() - 1, current="http://host/x.iso",
                         proxy=Mock(rate=Mock(return_value=0)),
                         engine=Mock(time=Mock(return_value=0)))
        Player.tick(player)
        player.fail.assert_called_once()

    def test_playback_clears_the_deadline_instead_of_failing(self):
        player = waiting(deadline=time.monotonic() - 1)
        player.controls.isVisible.return_value = False
        Player.tick(player)
        player.fail.assert_not_called()
        self.assertEqual(player.deadline, 0)

    def test_buffering_progress_pushes_the_deadline_back(self):
        player = waiting(deadline=time.monotonic() + 1)
        Player.on_event(player, "buffer:20")
        self.assertGreater(player.deadline, time.monotonic() + 30)


def refusal(code, reason, content_type="application/octet-stream"):
    headers = Message()
    headers["Content-Type"] = content_type
    return HTTPError("https://host/x", code, reason, headers, None)


class EndedTests(unittest.TestCase):
    def test_a_file_that_ends_without_a_frame_is_a_failure_not_a_finish(self):
        player = waiting(deadline=time.monotonic() + 30, disc=None)
        Player.on_event(player, "end")
        player.fail.assert_called_once()
        player.flash.assert_not_called()

    def test_a_film_that_actually_played_still_finishes_normally(self):
        player = waiting(deadline=0.0, disc=None)
        Player.on_event(player, "end")
        player.fail.assert_not_called()
        player.flash.assert_called_once_with("Finished", seconds=3)

    def test_recent_records_the_film_only_once_a_frame_arrives(self):
        player = waiting(deadline=time.monotonic() + 30, current="/tmp/film.mkv")
        player.controls.isVisible.return_value = False
        player.engine.time.return_value = 0
        Player.tick(player)
        player.remember.assert_not_called()
        player.engine.time.return_value = 120
        Player.tick(player)
        player.remember.assert_called_once_with("/tmp/film.mkv", "bluray")

    def test_an_unplayable_file_never_reaches_recent(self):
        player = waiting(deadline=time.monotonic() + 30, current="/tmp/notes.json", disc=None)
        Player.on_event(player, "end")
        player.remember.assert_not_called()


class LinkTests(unittest.TestCase):
    def test_archives_are_refused_before_anything_opens(self):
        with self.assertRaises(OSError) as caught:
            prepare("https://host/movie.rar")
        self.assertIn("can't open .rar", str(caught.exception))

    def test_a_dead_link_raises_instead_of_reaching_the_player(self):
        with mock.patch("isoplay.media.urlopen", side_effect=refusal(404, "Not Found")):
            with self.assertRaises(OSError) as caught:
                prepare("https://host/x.mkv")
        self.assertIn("no longer available", str(caught.exception))

    def test_a_web_page_is_not_mistaken_for_a_video(self):
        page = mock.MagicMock(status=200)
        page.headers.get_content_type.return_value = "text/html"
        page.__enter__.return_value = page
        with mock.patch("isoplay.media.urlopen", return_value=page):
            with self.assertRaises(OSError) as caught:
                prepare("https://host/watch?v=1")
        self.assertIn("web page", str(caught.exception))

    def test_a_range_the_server_dislikes_is_not_treated_as_a_dead_link(self):
        with mock.patch("isoplay.media.urlopen", side_effect=refusal(416, "Range Not Satisfiable")):
            self.assertEqual(prepare("https://host/clip.mkv"),
                             ("https://host/clip.mkv", None, None, (), None))

    def test_a_web_page_behind_a_range_refusal_is_still_caught(self):
        with mock.patch("isoplay.media.urlopen", side_effect=refusal(416, "Range", "text/html")):
            with self.assertRaises(OSError) as caught:
                prepare("https://host/clip.mkv")
        self.assertIn("web page", str(caught.exception))


class FitTests(unittest.TestCase):
    def shape(self, mode):
        player = waiting(fit=mode)
        player.video.width.return_value, player.video.height.return_value = 1600, 900
        Player.apply_fit(player)
        return player.engine.set_fit.call_args

    def test_the_window_shape_is_handed_to_the_engine_each_time_it_is_needed(self):
        self.assertEqual(self.shape("fit"), call("fit", 1600, 900))
        self.assertEqual(self.shape("fill"), call("fill", 1600, 900))
        self.assertEqual(self.shape("stretch"), call("stretch", 1600, 900))

    def test_a_collapsed_window_still_names_its_extent(self):
        player = waiting(fit="fill")
        player.video.width.return_value = player.video.height.return_value = 0
        Player.apply_fit(player)
        self.assertEqual(player.engine.set_fit.call_args, call("fill", 0, 0))


class SubtitleTests(unittest.TestCase):
    def drop(self, path, playing=True):
        player = waiting()
        player.engine.has_media.return_value = bool(playing)
        event = Mock()
        event.mimeData.return_value.hasUrls.return_value = False
        event.mimeData.return_value.text.return_value = path
        Player.dropEvent(player, event)
        return player

    def test_a_subtitle_dropped_on_a_playing_film_is_loaded_not_played(self):
        player = self.drop("/tmp/film.srt")
        player.play.assert_not_called()
        player.load_subtitles.assert_called_once_with("/tmp/film.srt")

    def test_a_subtitle_dropped_with_nothing_playing_still_opens_normally(self):
        player = self.drop("/tmp/film.srt", playing=False)
        player.play.assert_called_once_with("/tmp/film.srt")

    def test_a_film_dropped_while_playing_replaces_it(self):
        player = self.drop("/tmp/other.mkv")
        player.play.assert_called_once_with("/tmp/other.mkv")


if __name__ == "__main__":
    unittest.main()
