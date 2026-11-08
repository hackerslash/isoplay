"""Playback engines behind one small interface, so libVLC and libmpv can sit in the same app.

The interface (futureplan item 1, step 3) is what `app.Player` needs of an engine, and nothing
more; the streaming layer in `media.py` feeds both engines the same loopback URL. Engine names:
"vlc" (default today) and "mpv" (the item-1 prototype; see `isoplay/mpv.py` and `spike_mpv.py`).

The app polls rather than subscribes: `time`, `length`, `position`, `playing` and `menu_active`
are cheap to call every 100 ms. Events (`error`, `end`, `vout`, `buffer:%d`) arrive by the
callback given to `set_bridge`, from an engine thread, like libVLC's events do today.
Playback state that per-open caches depend on (which of a disc's titles is its menu) belongs to
each engine: libVLC reports menus implicitly through its title list, while mpv has the explicit
`disc-menu-active` property.
"""
import os
import sys

from isoplay import runtime  # noqa: F401 - sets the VLC environment before vlc loads

import vlc  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QFrame, QWidget  # noqa: E402

from isoplay.media import UA  # noqa: E402


class Engine:
    """The interface both engines implement. Type of `kind`: None, "dvd" or "bluray"."""

    name = "engine"

    # --- build and the widget video is drawn in -----------------------------------------------------

    def surface(self, parent) -> QWidget:
        """The widget video is shown in. A QFrame libVLC paints its native view over, or the
        widget that draws libmpv frames itself."""
        raise NotImplementedError

    def settle(self, surface):
        """Called once, after the surface is in the window, when it has native resources."""

    def shutdown(self):
        """The window is closing."""

    # --- media ---------------------------------------------------------------------------------------

    def open(self, mrl, kind=None, menus=True):
        """Play a URL or path, which may be a disc image served over loopback."""
        raise NotImplementedError

    def close(self):
        """Stop the current source (the start screen is showing again)."""
        raise NotImplementedError

    def has_media(self) -> bool:
        raise NotImplementedError

    # --- state, polled -------------------------------------------------------------------------------

    def playing(self) -> bool:
        raise NotImplementedError

    def time(self) -> int:
        """Position in ms, or a value <= 0 when unknown."""
        raise NotImplementedError

    def length(self) -> int:
        raise NotImplementedError

    def position(self) -> float:
        """0..1, or < 0 when unknown."""
        raise NotImplementedError

    def menu_active(self) -> bool:
        """A disc menu is on screen waiting for a choice."""

    def seeking(self) -> bool:
        """A seek is in flight: the picture is frozen until it lands. libVLC reports seeks
        through buffering events instead, so only the mpv engine answers True."""
        return False
        raise NotImplementedError

    # --- transport -----------------------------------------------------------------------------------

    def pause(self):
        """Toggle."""
        raise NotImplementedError

    def seek(self, ms):
        raise NotImplementedError

    def set_position(self, fraction):
        raise NotImplementedError

    def step_chapter(self, d):
        raise NotImplementedError

    def chapter_count(self) -> int:
        raise NotImplementedError

    def chapter(self) -> int:
        raise NotImplementedError

    def set_volume(self, value):
        raise NotImplementedError

    def toggle_mute(self) -> bool:
        """Return the muted state after toggling."""
        raise NotImplementedError

    # --- tracks ----------------------------------------------------------------------------------------

    def tracks(self) -> dict:
        """{'audio': ([(tid, label)], current_tid), 'sub': (...)}; the id -1 means Off."""
        raise NotImplementedError

    def select_track(self, kind, tid):
        raise NotImplementedError

    def subtitles(self, uri) -> bool:
        raise NotImplementedError

    # --- appearance ------------------------------------------------------------------------------------

    def set_fit(self, mode, width, height):
        """One of "fit" (whole picture), "fill" (crop the overflow), "stretch" (distort)."""
        raise NotImplementedError

    # --- disc menus ------------------------------------------------------------------------------------

    def navigate(self, action):
        """One of "up", "down", "left", "right", "select", "popup"."""
        raise NotImplementedError

    def menu_mouse(self, x, y, click):
        """Mouse over the video, at x/y in surface pixels (device pixels). A no-op in engines
        that can't be told about the mouse (libVLC 3 on macOS)."""

    def set_bridge(self, emit):
        """`emit(name)` for the events error, end, vout and buffer:pct, from an engine thread."""


class VlcEngine(Engine):
    """libVLC 3 today: a native vout painted over the Qt window, driven by python-vlc."""

    name = "libVLC"

    def __init__(self, instance=None):
        self.instance = instance
        if self.instance is None:
            import os
            self.instance = vlc.Instance(["--no-video-title-show", "--network-caching=3000"]
                                         + (["-vv"] if os.environ.get("ISOPLAY_DEBUG") else []))
        self.mp = self.instance.media_player_new()
        self.mp.video_set_key_input(False)
        self.mp.video_set_mouse_input(True)
        self._menu_titles = None  # libVLC reports menus through its list of titles
        self.emit = None

    def surface(self, parent):
        frame = QFrame(parent)
        frame.setStyleSheet("background:#000")
        frame.setAttribute(Qt.WA_NativeWindow)
        return frame

    def settle(self, surface):
        wid = int(surface.winId())
        {"darwin": self.mp.set_nsobject, "win32": self.mp.set_hwnd}.get(sys.platform, self.mp.set_xwindow)(wid)
        ev = self.mp.event_manager()
        for t, name in ((vlc.EventType.MediaPlayerEncounteredError, "error"),
                        (vlc.EventType.MediaPlayerVout, "vout"),
                        (vlc.EventType.MediaPlayerEndReached, "end")):
            ev.event_attach(t, lambda e, n=name: self.emit(n))
        ev.event_attach(vlc.EventType.MediaPlayerBuffering,
                        lambda e: self.emit(f"buffer:{e.u.new_cache:.0f}"))

    def set_bridge(self, emit):
        self.emit = emit

    # --- media ---------------------------------------------------------------------------------------

    def open(self, mrl, kind=None, menus=True):
        media = self.instance.media_new(mrl) if "://" in mrl else self.instance.media_new_path(mrl)
        media.add_option(f":http-user-agent={UA}")
        if mrl.lower().endswith(".evo"):
            media.add_option(":demux=avformat")
        if kind == "dvd" and menus and not mrl.startswith("dvd:"):
            media.add_option(":demux=dvdnav")  # auto-probe picks raw PS over HTTP and loses menus
        if kind == "bluray" and not menus:
            media.add_option(":no-bluray-menu")
        self.mp.set_media(media)
        self.mp.play()
        self._menu_titles = None

    def close(self):
        self.mp.stop()

    def has_media(self):
        return self.mp.get_media() is not None

    # --- state ---------------------------------------------------------------------------------------

    def playing(self):
        return self.mp.is_playing()

    def time(self):
        return self.mp.get_time()

    def length(self):
        return self.mp.get_length()

    def position(self):
        return self.mp.get_position()

    def menu_active(self):
        """A disc menu waits for a choice, which libVLC reports as buffering at 0%."""
        if self._menu_titles is None:  # read once a play: python-vlc never frees the list it returns
            titles = list(self.mp.get_full_title_descriptions() or ())
            if not titles:
                return False  # not parsed yet
            self._menu_titles = {i for i, d in enumerate(titles) if d.flags & 1}  # libvlc_title_menu
        return self.mp.get_title() in self._menu_titles

    # --- transport -----------------------------------------------------------------------------------

    def pause(self):
        self.mp.pause()

    def seek(self, ms):
        self.mp.set_time(ms)

    def set_position(self, fraction):
        self.mp.set_position(fraction)

    def step_chapter(self, d):
        self.mp.next_chapter() if d > 0 else self.mp.previous_chapter()

    def chapter_count(self):
        return self.mp.get_chapter_count() or 0

    def chapter(self):
        return max(0, self.mp.get_chapter())

    def set_volume(self, value):
        self.mp.audio_set_volume(value)

    def toggle_mute(self):
        self.mp.audio_toggle_mute()
        return bool(self.mp.audio_get_mute())

    def tracks(self):
        def described(describe, current):
            tracks = describe() or []
            return [(tid, label.decode(errors="replace") if isinstance(label, bytes) else str(label))
                    for tid, label in tracks], current

        audio = described(self.mp.audio_get_track_description, self.mp.audio_get_track())
        subs = described(self.mp.video_get_spu_description, self.mp.video_get_spu())
        return {"audio": audio, "sub": subs}

    def select_track(self, kind, tid):
        {"audio": self.mp.audio_set_track, "sub": self.mp.video_set_spu}[kind](tid)

    def subtitles(self, uri):
        return self.mp.add_slave(vlc.MediaSlaveType.subtitle, uri, True) == 0

    # --- appearance ------------------------------------------------------------------------------------

    def set_fit(self, mode, width, height):
        """Fill crops the overflow, stretch distorts; both track the window's own shape."""
        shape = f"{max(width, 1)}:{max(height, 1)}"
        self.mp.video_set_aspect_ratio(shape if mode == "stretch" else None)
        self.mp.video_set_crop_geometry(shape if mode == "fill" else None)

    # --- disc menus ------------------------------------------------------------------------------------

    NAVIGATE = {"up": vlc.NavigateMode.up, "down": vlc.NavigateMode.down,
                "left": vlc.NavigateMode.left, "right": vlc.NavigateMode.right,
                "select": vlc.NavigateMode.activate, "popup": vlc.NavigateMode.popup}

    def navigate(self, action):
        self.mp.navigate(self.NAVIGATE[action])

    def menu_mouse(self, x, y, click):
        """libVLC 3 has no public way to send mouse coordinates to a menu, and its macOS vout
        (caopengllayer) never sees mouse events from a Qt window. This is what the mpv prototype
        exists to fix (futureplan items 1-2)."""


def create_engine(name=None):
    """The engine to run under: ISOPLAY_ENGINE=mpv opts into the prototype; libVLC otherwise."""
    name = (name or os.environ.get("ISOPLAY_ENGINE", "vlc")).lower()
    if name in ("mpv", "libmpv"):
        from isoplay.mpv import MpvEngine
        return MpvEngine()
    return VlcEngine()
