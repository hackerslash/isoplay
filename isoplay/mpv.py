"""The libmpv engine: futureplan item 1, the prototype behind the same interface as libVLC.

What mpv 0.42 (released 2026-11-05) brings with it:

- direct playback of .iso images of DVD-Video, DVD-Audio and Blu-ray (auto-detected by the
  stream layer, including ours at http://127.0.0.1:PORT/disc.iso);
- disc menus again: a `--disc-menu` option, a synthetic "Disc Menu" edition, a `discnav`
  command with up/down/left/right/select/popup and - decisive for macOS - `mouse-move` and
  `mouse-click` actions, plus a `disc-menu-active` property to know a menu is up. libVLC 3 has
  no public way to send mouse coordinates to a menu, which is what item 1 is about;
- its own network layer on libcurl with HTTP/2 and connection reuse.

Input reaches the menu through mpv's input layer: the `mouse <x> <y> [<button>]` command updates
`mouse-pos` (pixel coordinates of the synthetic render window, whose size is the FBO we render
into), and an argument-less `discnav mouse-move` / `discnav mouse-click` reads and maps
`mouse-pos` into source-video coordinates itself. We forward Qt mouse events that way rather
than repositioning buttons by hand.

Rendering: `MpvRenderContext` draws into a QOpenGLWidget's own framebuffer (`paintGL` into
`defaultFramebufferObject()` with FLIP_Y), so video is an ordinary Qt widget and the four
floating Overlay windows the libVLC path needs (Qt can't paint over a native surface) can stay
ordinary widgets.

python-mpv 1.0.8 exposes the render API we need (MpvRenderContext, MpvOpenGLFBO,
MpvGlGetProcAddressFn); the mpv.py single file it ships as is also fine to vendor. The libmpv
import is lazy: machines without it keep working with libVLC untouched.

Open questions the spike (spike_mpv.py) has to settle with real discs are in docs/spike-mpv.md.
"""
import ctypes
import ctypes.util
import os
import re

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QOpenGLContext
from PySide6.QtOpenGLWidgets import QOpenGLWidget

from isoplay.media import UA


def _find_libmpv():
    """Where libmpv is, if anywhere: LIBMPV_PATH wins, then the loader's own search, then the
    places a mac or linux install actually puts it (ctypes' search misses Homebrew)."""
    path = os.environ.get("LIBMPV_PATH")
    if path:
        return path
    found = ctypes.util.find_library("mpv")
    if found:
        return found
    candidates = ("/opt/homebrew/lib/libmpv.2.dylib",  # Homebrew on Apple silicon
                  "/usr/local/lib/libmpv.2.dylib",  # Homebrew on Intel, or a manual install
                  "/opt/homebrew/opt/mpv/lib/libmpv.2.dylib",
                  "/usr/lib/libmpv.so.2", "/usr/lib64/libmpv.so.2")  # usual Linux So names
    return next((c for c in candidates if os.path.exists(c)), None)


def binding():
    """The python-mpv module, loaded only when the mpv engine is asked for. python-mpv locates
    libmpv with ctypes.util.find_library, which misses, among others, Homebrew's lib dir - so the
    discovered path is lent to it until libmpv is where the loader already looks."""
    if not os.environ.get("ISOPLAY_DEBUG_MPV_RAW"):
        found = _find_libmpv()
        if not found:
            hint = ctypes.util.find_library("mpv")
            raise SystemExit(
                "the mpv engine can't find libmpv."
                "\n  macOS    brew install mpv   (then LIBMPV_PATH=/opt/homebrew/opt/mpv/lib/libmpv.2.dylib)"
                "\n  Windows  put libmpv-2.dll next to the app, or LIBMPV_PATH=<its path>"
                "\n  Linux    sudo apt install libmpv2 (or install mpv)")
        _raw_search = ctypes.util.find_library
        ctypes.util.find_library = (lambda name: found if name == "mpv" else _raw_search(name))
    try:
        import mpv
    except OSError as e:
        raise SystemExit(f"the mpv engine can't load libmpv ({e})") from e
    return mpv


# --- pure helpers (no libmpv needed at module import; tests run them with stubs) ------------------------


def end_to_event(reason):
    """Map an end-file reason onto isoplay's event names (MpvEventEndFile: 0 EOF, 4 ERROR;
    RESTARTED/QUIT/ABORTED keep quiet, exactly like the libVLC events the old path used)."""
    return {0: "end", 4: "error"}.get(reason)


def track_label(item):
    """One line for the track menu, from a track-list entry."""
    parts = [item.get("title"), " ".join(filter(None, (item.get("lang"), item.get("codec"))))]
    return " ".join(p for p in parts if p)


def track_entries(track_list, kind, selected):
    """([(tid, label)], current_tid) for audio or subtitle tracks, with Off first, like libVLC's
    description lists, which is the shape the app's track menu is built for."""
    items = [t for t in track_list or () if t.get("type") == kind]
    rows = [(-1, "Off")] + [(t["id"], track_label(t) or f"Track {t['id']}") for t in items]
    current = next((t["id"] for t in items if t.get("selected")), selected)
    return rows, current


def fit_zoom(width, height, video_w, video_h):
    """Video zoom relative to mpv's own fit, so the picture covers the widget ("fill"): the cover
    scale divided by the box scale, both from the same source-pixel geometry."""
    if not video_w or not video_h or not width or not height:
        return 0.0
    cover = max(width / video_w, height / video_h)
    box = min(width / video_w, height / video_h)
    return cover / box if box else 0.0


# --- the engine -----------------------------------------------------------------------------------------


class MpvEngine:
    """libmpv 0.42: menus take mouse input, and video is drawn into a Qt widget by MpvRenderContext."""

    name = "libmpv"

    def __init__(self, _player=None):
        self.mpv = binding()
        if _player is not None:  # tests (or the spike) inject a stub instead of libmpv
            self.player = _player
        else:
            # vo=libmpv hands drawing to MpvRenderContext. mpv's own window, OSC and key bindings
            # are off: the app is the window, and it forwards only what a disc menu needs.
            self.player = self.mpv.MPV(
                config="no",
                vo="libmpv",
                idle="yes",  # stay alive between sources instead of quitting with nothing loaded
                keep_open="no",
                osc="no",
                input_default_bindings="no",
                save_watch_history="no",
                user_agent=UA,
                hwdec="auto-safe",
            )
        self.video_widget = None
        self.render_context = None
        self.emit = None
        self._vout = False  # once per open, a video track actually being drawn
        version = getattr(self.player, "mpv_version", "") or ""
        match = re.search(r"v(\d+)\.(\d+)\.(\d+)", version)
        if match and tuple(map(int, match.groups())) < (0, 42, 0):
            # disc menus (the --disc-menu option, discnav, the Disc Menu edition) arrived in
            # 0.42.0; before that this engine would only ever show the main title.
            raise SystemExit(f"the mpv engine needs mpv 0.42.0 or newer for disc menus; {version.strip()} is installed.")
        player = self.player

        @player.event_callback("end-file")
        def _end(event):
            name = end_to_event(event.data.reason)
            if name:
                self._emit(name)

        def _cache(name, value):
            self._emit(f"buffer:{100 if not value else 0}")  # mpv means "stalled on data" by it

        def _video(name, value):
            if value and not self._vout:
                self._vout = True
                self._emit("vout")

        player.observe_property("paused-for-cache", _cache)
        player.observe_property("video-format", _video)

    def _emit(self, name):
        if self.emit:
            self.emit(name)

    def set_bridge(self, emit):
        self.emit = emit

    # --- build and the widget video is drawn in -----------------------------------------------------

    def surface(self, parent):
        self.video_widget = MpvVideoWidget(self, parent)
        return self.video_widget

    def settle(self, surface):
        pass  # the render context is created by the widget's initializeGL, once there is a GL context

    def shutdown(self):
        try:
            self.player.terminate()
        except Exception:  # noqa: BLE001 - a closing app shouldn't die on engine shutdown
            pass

    # --- media ---------------------------------------------------------------------------------------

    def open(self, mrl, kind=None, menus=True):
        self._vout = False
        player = self.player
        player.pause = False
        for name in ("aspect", "video-zoom"):  # reset what set_fit may have changed last time
            try:
                player[name] = "no" if name == "aspect" else "0"
            except Exception:  # noqa: BLE001 - exact option names differ between mpv versions
                pass
        if kind in ("dvd", "bluray"):
            player["disc-menu"] = "yes" if menus else "no"
        player.command("loadfile", mrl, "replace")

    def close(self):
        self.player.command("stop")

    def has_media(self):
        try:
            return bool(self.player.playlist_count)
        except Exception:  # noqa: BLE001 - unavailable before anything loaded
            return False

    # --- state ---------------------------------------------------------------------------------------

    def playing(self):
        try:
            return not self.player.core_idle
        except Exception:  # noqa: BLE001
            return False

    def _seconds(self, name):
        try:
            value = getattr(self.player, name.replace("-", "_"))
        except Exception:  # noqa: BLE001 - nothing loaded, or a menu edition has no estimate
            return 0.0
        return value if value >= 0 else 0.0

    def time(self):
        return int(self._seconds("time-pos") * 1000)

    def length(self):
        return int(self._seconds("duration") * 1000)

    def position(self):
        try:
            return self.player.percent_pos / 100.0
        except Exception:  # noqa: BLE001
            return -1.0

    def menu_active(self):
        try:
            return bool(self.player.disc_menu_active)
        except Exception:  # noqa: BLE001 - unset while nothing plays
            return False

    # --- transport -----------------------------------------------------------------------------------

    def pause(self):
        self.player.pause = not bool(self.player.pause)

    def seek(self, ms):
        self.player.command("seek", ms / 1000.0, "absolute")

    def set_position(self, fraction):
        self.player.command("seek", fraction * 100, "absolute-percent")

    def step_chapter(self, d):
        self.player.command("add", "chapter", d)

    def chapter_count(self):
        try:
            return self.player.chapters or 0
        except Exception:  # noqa: BLE001
            return 0

    def chapter(self):
        try:
            return max(0, self.player.chapter)
        except Exception:  # noqa: BLE001
            return 0

    def set_volume(self, value):
        self.player.volume = value

    def toggle_mute(self):
        self.player.mute = not bool(self.player.mute)
        return bool(self.player.mute)

    # --- tracks -----------------------------------------------------------------------------------------

    def tracks(self):
        items = self.player.track_list or ()
        return {"audio": track_entries(items, "audio", self._currently("aid")),
                "sub": track_entries(items, "sub", self._currently("sid"))}

    def _currently(self, prop):
        try:
            value = getattr(self.player, prop)
        except Exception:  # noqa: BLE001
            return -1
        if isinstance(value, str):  # "no" means Off, and there is no track id to hand back
            return -1
        return value

    def select_track(self, kind, tid):
        self.player["aid" if kind == "audio" else "sid"] = "no" if tid == -1 else tid

    def subtitles(self, uri):
        self.player.command("sub-add", uri, "select")
        return True

    # --- appearance ------------------------------------------------------------------------------------

    def set_fit(self, mode, width, height):
        """fit is mpv's own letterboxed fit; fill zooms the picture so it covers the window;
        stretch overrides the aspect to the window's (exact) shape."""
        params = {}
        try:
            params = self.player.video_params or {}
        except Exception:  # noqa: BLE001 - nothing is playing yet
            pass
        player = self.player
        if mode == "stretch":
            player.aspect = f"{max(width, 1)}:{max(height, 1)}"
            player["video-zoom"] = "0"
        elif mode == "fill":
            player["aspect"] = "no"
            player["video-zoom"] = str(fit_zoom(width, height, params.get("w"), params.get("h")))
        else:
            player["aspect"] = "no"
            player["video-zoom"] = "0"

    # --- disc menus ------------------------------------------------------------------------------------

    def navigate(self, action):
        try:
            self.player.command("discnav", action)
        except Exception:  # noqa: BLE001 - nav before a disc is up (or on plain video) is noise
            pass

    def menu_mouse(self, x, y, click):
        """Pixel position in the render window (the FBO the video is drawn into), then let mpv map
        it onto the disc itself; click activates the button under the cursor."""
        player = self.player
        try:
            player.command("mouse", int(x), int(y))
            player.command("discnav", "mouse-click" if click else "mouse-move")
        except Exception:  # noqa: BLE001 - same: menus that take no mouse reject quietly
            pass


# --- the widget -----------------------------------------------------------------------------------------


class MpvVideoWidget(QOpenGLWidget):
    """Draws mpv's frames into the Qt window itself, so Qt sees every video pixel: overlays,
    toasts and the loader become ordinary widgets, and mouse events reach disc menus."""

    frame_ready = Signal()

    def __init__(self, engine, parent):
        super().__init__(parent)
        self.engine = engine
        self._gl_ref = None  # the ctypes callback has to outlive initializeGL's scope
        self.frame_ready.connect(self.update, Qt.ConnectionType.QueuedConnection)

    def initializeGL(self):
        def proc(_ctx, name):
            if not name:
                return 0
            try:
                return int(QOpenGLContext.currentContext().getProcAddress(name.decode()))
            except Exception:  # noqa: BLE001
                return 0

        self._gl_ref = pointer = self.engine.mpv.MpvGlGetProcAddressFn(proc)

        def on_update():
            self.frame_ready.emit()  # called from an mpv thread; queued onto the Qt thread

        self.engine.render_context = ctx = self.engine.mpv.MpvRenderContext(
            self.engine.player, "opengl", opengl_init_params={"get_proc_address": pointer})
        ctx.update_cb = on_update

    def paintGL(self):
        if not self.engine.render_context:
            return
        self.engine.render_context.render(
            opengl_fbo={"w": int(self.width() * self.devicePixelRatioF()),
                        "h": int(self.height() * self.devicePixelRatioF()),
                        "fbo": int(self.defaultFramebufferObject()), "internal_format": 0},
            flip_y=True,
            block_for_target_time=True)

    def mouseMoveEvent(self, event):
        self._menu_mouse(event, click=False)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:  # right click is left to mpv's context menu bindings
            self._menu_mouse(event, click=True)

    def _menu_mouse(self, event, click):
        dpr = self.devicePixelRatioF()
        self.engine.menu_mouse(event.position().x() * dpr, event.position().y() * dpr, click)
