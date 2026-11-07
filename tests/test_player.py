"""Player behaviour that needs no VLC instance: methods are called unbound on a stub."""
import os
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from isoplay.app import Player


def waiting(**overrides):
    player = Mock(buffering=False, osd_until=0.0, busy_until=float("inf"), disc="bluray", use_menus=True)
    player.mp.get_time.return_value = 5000
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

    def test_full_buffer_clears_only_after_buffering(self):
        player = waiting(buffering=True, busy_until=0.0)
        Player.on_event(player, "buffer:100")
        player.flash.assert_called_once_with("")


if __name__ == "__main__":
    unittest.main()
