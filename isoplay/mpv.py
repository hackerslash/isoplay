"""The libmpv engine: futureplan item 1, the prototype behind the same interface as libVLC.

What mpv 0.42 (released 2026-11-05) brings with it:

- direct playback of .iso images of DVD-Video, DVD-Audio and Blu-ray (auto-detected by the
  stream layer, including ours at http://127.0.0.1:PORT/disc.iso);
- disc menus again: a `--disc-menu` option, a synthetic "Disc Menu" edition, a `discnav`
  command with up/down/left/right/select/popup and - decisive for macOS - `mouse-move` and
  `mouse-click` actions, plus a `disc-menu-active` property to know a menu is up. libVLC 3 has
  no public way to send mouse coordinates to a menu, which is what item 1 is about;
- its own network layer on libcurl with HTTP/2 and connection reuse.

Input reaches the menu through mpv's input layer: `discnav mouse-move` / `discnav mouse-click`
take normalized 0..1 coordinates (top-left 0,0) and map them from the render window into
source-video coordinates themselves. Qt mouse events are forwarded that way — normalized by
the widget's own size, so device pixels and OSD scaling never enter it — rather than
repositioning buttons by hand. (The `mouse` command's `mouse-pos` is in OSD units, not window
pixels, so pixel coordinates misland; the explicit discnav arguments skip that layer.)

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


# ISO 639 codes (mpv reports 3-letter bibliographic, sometimes 2-letter) to the names a
# track menu shows, the way VLC's own descriptions read.
LANGS = {
    "eng": "English", "en": "English",
    "fra": "French", "fre": "French", "fr": "French",
    "deu": "German", "ger": "German", "de": "German",
    "ita": "Italian", "it": "Italian",
    "spa": "Spanish", "es": "Spanish",
    "por": "Portuguese", "pt": "Portuguese",
    "nld": "Dutch", "dut": "Dutch", "nl": "Dutch",
    "ces": "Czech", "cze": "Czech", "cs": "Czech",
    "slk": "Slovak", "slo": "Slovak", "sk": "Slovak",
    "pol": "Polish", "pl": "Polish",
    "rus": "Russian", "ru": "Russian",
    "ukr": "Ukrainian", "uk": "Ukrainian",
    "bul": "Bulgarian", "bg": "Bulgarian",
    "hrv": "Croatian", "hr": "Croatian",
    "srp": "Serbian", "sr": "Serbian",
    "ell": "Greek", "gre": "Greek", "el": "Greek",
    "hun": "Hungarian", "hu": "Hungarian",
    "ron": "Romanian", "rum": "Romanian", "ro": "Romanian",
    "zho": "Chinese", "chi": "Chinese", "zh": "Chinese",
    "yue": "Cantonese",
    "kor": "Korean", "ko": "Korean",
    "jpn": "Japanese", "ja": "Japanese",
    "ara": "Arabic", "ar": "Arabic",
    "heb": "Hebrew", "he": "Hebrew",
    "dan": "Danish", "da": "Danish",
    "fin": "Finnish", "fi": "Finnish",
    "nor": "Norwegian", "no": "Norwegian", "nob": "Norwegian", "nno": "Norwegian",
    "swe": "Swedish", "sv": "Swedish",
    "tur": "Turkish", "tr": "Turkish",
    "hin": "Hindi", "hi": "Hindi",
    "tha": "Thai", "th": "Thai",
    "vie": "Vietnamese", "vi": "Vietnamese",
    "ind": "Indonesian", "id": "Indonesian",
    "msa": "Malay", "may": "Malay", "ms": "Malay",
    "cat": "Catalan", "ca": "Catalan",
    "eus": "Basque", "eu": "Basque",
    "glg": "Galician", "gl": "Galician",
}

# FFmpeg codec names to the words on the box: what mpv reports, not what people read.
CODECS = {
    "truehd": "TrueHD", "mlp": "MLP",
    "ac3": "AC-3", "eac3": "E-AC-3",
    "dts": "DTS", "dca": "DTS",
    "aac": "AAC", "mp3": "MP3", "mp2": "MP2",
    "flac": "FLAC", "opus": "Opus", "vorbis": "Vorbis",
    "pcm_bluray": "PCM",
    "hdmv_pgs_subtitle": "PGS", "pgssub": "PGS",
    "dvd_subtitle": "VobSub", "vobsub": "VobSub",
    "dvb_subtitle": "DVB", "xsub": "XSUB",
    "subrip": "SubRip", "ass": "ASS", "ssa": "SSA",
    "webvtt": "WebVTT", "mov_text": "TX3G", "eia_608": "EIA-608",
}


def track_label(item):
    """One line for the track menu, VLC-style: title, language name and friendly codec."""
    title = (item.get("title") or "").strip()
    lang = LANGS.get((item.get("lang") or "").lower(), item.get("lang") or "")
    codec = CODECS.get((item.get("codec") or "").lower(), item.get("codec") or "")
    label = " · ".join(p for p in (title, lang) if p)
    if codec:
        label = f"{label} ({codec})" if label else codec
    return label


def track_entries(track_list, kind, selected):
    """([(tid, label)], current_tid) for audio or subtitle tracks, with Off first, like libVLC's
    description lists, which is the shape the app's track menu is built for. Identical discs
    list the same language twice, so repeats are numbered from their second showing."""
    items = [t for t in track_list or () if t.get("type") == kind]
    rows = [(-1, "Off")]
    seen = {}
    for t in items:
        label = track_label(t) or f"Track {t['id']}"
        seen[label] = seen.get(label, 0) + 1
        if seen[label] > 1:
            label = f"{label} ({seen[label]})"
        rows.append((t["id"], label))
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


# mpv's own network layer, tuned for isoplay's inputs (mpv 0.42 option names). Discs arrive
# over the loopback proxy and plain videos straight over remote https, so mpv's cache,
# readahead and reconnects all matter. Applied best-effort after construction, one option at
# a time, so a libmpv missing one still starts.
NETWORK_OPTIONS = {
    "cache": "yes",  # always keep a demuxer cache, even for inputs mpv deems local
    "demuxer-readahead-secs": "10",  # headroom past the play head (default 1); seeks stall less
    "stream-buffer-size": "1048576",  # fewer round trips per megabyte (default 128 KiB)
    "network-timeout": "30",  # idle seconds before a dead socket errors (the open deadline is 45)
    "stream-lavf-o": "reconnect=1,reconnect_streamed=1,reconnect_on_network_error=1,"
                      "reconnect_delay_max=5",  # flaky hosts: FFmpeg redials instead of failing
    "force-seekable": "yes",  # hosts that omit Accept-Ranges still honor Range on GET
}


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
            options = dict(
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
            if os.environ.get("ISOPLAY_DEBUG"):
                options["loglevel"] = "info"  # libmpv's own story of the disc, line by line
                options["log_handler"] = lambda level, prefix, text: print(
                    f"[mpv {level} {prefix}] {text.strip()}", flush=True)
            self.player = self.mpv.MPV(**options)
        self.video_widget = None
        self.render_context = None
        self.emit = None
        self._vout = False  # once per open, a video track actually being drawn
        self._cache_state = None  # mpv's cache fill percentage, when it has a cache at all
        self._stalled = False     # paused-for-cache: mpv wants data and none is arriving
        self._seeking = False     # a seek is in flight: the picture is frozen by definition
        self._apply_network_options()
        self._observe()
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

    def _apply_network_options(self):
        for name, value in NETWORK_OPTIONS.items():
            try:
                self.player[name] = value
            except Exception as e:  # noqa: BLE001 - an older or newer libmpv without this option
                if os.environ.get("ISOPLAY_DEBUG"):
                    print(f"[mpv-engine] option {name} not applied: {e!r}", flush=True)

    def _report(self):
        """One buffering state from three flags: seeking or stalled means waiting for data,
        whatever the cache percentage still reads (libbluray keeps it at 100 while blocked on
        our read callback, and a seek has no fill to report at all)."""
        if self._seeking or self._stalled:
            self._emit("buffer:0")
        elif self._cache_state is None:
            self._emit("buffer:100")  # no demuxer cache (a local file): nothing to report
        else:
            self._emit(f"buffer:{max(0, min(100, int(self._cache_state)))}")

    def _observe(self):
        player = self.player

        def _stalled(name, value):
            self._stalled = bool(value)
            self._report()

        def _cache(name, value):
            self._cache_state = value if isinstance(value, (int, float)) else None
            if self._cache_state is None:
                return
            if self._stalled or self._seeking:
                if self._cache_state >= 100:
                    return  # stale full-reading while blocked; the stall owns the overlay
                self._emit(f"buffer:{max(0, int(self._cache_state))}")  # real progress mid-stall
            else:
                self._report()

        def _seeking(name, value):
            self._seeking = bool(value)
            self._report()

        def _video(name, value):
            if value and not self._vout:
                self._vout = True
                self._emit("vout")

        player.observe_property("paused-for-cache", _stalled)
        player.observe_property("cache-buffering-state", _cache)
        player.observe_property("seeking", _seeking)
        player.observe_property("video-format", _video)

    def _emit(self, name):
        if os.environ.get("ISOPLAY_DEBUG"):
            print(f"[mpv-engine] event {name}", flush=True)
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
        if os.environ.get("ISOPLAY_DEBUG"):
            print(f"[mpv-engine] open {mrl} kind={kind or 'plain'} menus={menus}", flush=True)
        self._vout = False
        self._stalled = self._seeking = False
        self._cache_state = None
        player = self.player
        player.pause = False
        try:  # reset what set_fit may have changed last time
            player.video_aspect_override = "no"
            player.video_zoom = 0
        except Exception:  # noqa: BLE001 - exact option names differ between mpv versions
            pass
        if kind in ("dvd", "bluray"):
            player["disc-menu"] = "yes" if menus else "no"
            # Both a local path and isoplay's loopback URL go through the disc backend's
            # device option, so a streamed image is read by bd_open_stream/dvdnav_open_stream
            # (patched libmpv) exactly like VLC does it. Stock mpv only accepts local paths.
            if kind == "bluray":
                player["bluray-device"] = mrl
                target = "bd://"
            else:
                player["dvd-device"] = mrl
                target = "dvd://"
        else:
            target = mrl
        player.command("loadfile", target, "replace")

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
        # an idle player answers None, not an exception, and menu editions report -1
        return float(value) if isinstance(value, (int, float)) and value >= 0 else 0.0

    def time(self):
        return int(self._seconds("time-pos") * 1000)

    def length(self):
        return int(self._seconds("duration") * 1000)

    def position(self):
        try:
            value = self.player.percent_pos
        except Exception:  # noqa: BLE001
            return -1.0
        return value / 100.0 if isinstance(value, (int, float)) else -1.0

    def menu_active(self):
        try:
            return bool(self.player.disc_menu_active)
        except Exception:  # noqa: BLE001 - unset while nothing plays
            return False

    def seeking(self):
        try:
            return bool(self.player.seeking)
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
        # "no" means Off and None means nothing loaded; neither is a track id to hand back
        return value if isinstance(value, int) else -1

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
            player.video_aspect_override = f"{max(width, 1)}:{max(height, 1)}"
            player.video_zoom = 0
        elif mode == "fill":
            player.video_aspect_override = "no"  # "no" is -2, mpv's own aspect
            player.video_zoom = fit_zoom(width, height, params.get("w"), params.get("h"))
        else:
            player.video_aspect_override = "no"
            player.video_zoom = 0

    # --- disc menus ------------------------------------------------------------------------------------

    def navigate(self, action):
        if os.environ.get("ISOPLAY_DEBUG"):
            print(f"[mpv-engine] discnav {action} menu-active={self.menu_active()}", flush=True)
        try:
            self.player.command("discnav", action)
        except Exception as e:  # noqa: BLE001 - nav before a disc is up (or on plain video) is noise
            if os.environ.get("ISOPLAY_DEBUG"):
                print(f"[mpv-engine] discnav {action} failed: {e!r}", flush=True)

    def menu_mouse(self, x, y, click):
        """Normalized 0..1 position in the render window (top-left 0,0), then let mpv map it
        onto the disc itself; click activates the button under the cursor."""
        x = max(0.0, min(1.0, float(x)))
        y = max(0.0, min(1.0, float(y)))
        action = "mouse-click" if click else "mouse-move"
        if os.environ.get("ISOPLAY_DEBUG"):
            print(f"[mpv-engine] discnav {action} {x:.4f} {y:.4f} "
                  f"menu-active={self.menu_active()}", flush=True)
        try:
            self.player.command("discnav", action, f"{x:.4f}", f"{y:.4f}")
        except Exception as e:  # noqa: BLE001 - nav before a disc is up (or on plain video) is noise
            if os.environ.get("ISOPLAY_DEBUG"):
                print(f"[mpv-engine] discnav {action} failed: {e!r}", flush=True)


# --- the widget -----------------------------------------------------------------------------------------


class MpvVideoWidget(QOpenGLWidget):
    """Draws mpv's frames into the Qt window itself, so Qt sees every video pixel: overlays,
    toasts and the loader become ordinary widgets, and mouse events reach disc menus."""

    frame_ready = Signal()

    def __init__(self, engine, parent):
        super().__init__(parent)
        self.engine = engine
        self.setMouseTracking(True)  # hover must reach disc menus with no button held
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
        active = self.engine.menu_active()
        if os.environ.get("ISOPLAY_DEBUG"):
            pos = event.position()
            print(f"[mpv-engine] qt {'click' if click else 'move'} "
                  f"{pos.x():.0f},{pos.y():.0f} of {self.width()}x{self.height()} "
                  f"menu-active={active}", flush=True)
        if not active:
            return  # normal playback: the disc has no menu to take the mouse
        w, h = max(1, self.width()), max(1, self.height())
        pos = event.position()
        self.engine.menu_mouse(pos.x() / w, pos.y() / h, click)
