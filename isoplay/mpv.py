"""The playback engine: libmpv 0.42+, patched by tools/build_mpv.py to open disc images over a
stream, so disc menus play from isoplay's loopback URL and take the mouse.

Video is drawn by MpvRenderContext into a QOpenGLWidget, so it is an ordinary Qt widget and
mouse events reach it. Menu positions go to discnav normalized 0..1 against the drawn picture;
the `mouse` command's mouse-pos is in OSD units, so pixels would misland.
"""
import collections
import ctypes
import ctypes.util
import os
import queue
import re
import sys
import threading
import time

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QOpenGLContext
from PySide6.QtOpenGLWidgets import QOpenGLWidget

from isoplay.media import UA


LIBMPV = ("/opt/homebrew/lib/libmpv.2.dylib", "/usr/local/lib/libmpv.2.dylib")  # ctypes misses Homebrew


def binding():
    """python-mpv, loaded against LIBMPV_PATH or the first libmpv found; python-mpv itself only
    asks ctypes.util.find_library, so the path is lent to that for the import."""
    found = (os.environ.get("LIBMPV_PATH") or ctypes.util.find_library("mpv")
             or next((c for c in LIBMPV if os.path.exists(c)), None))
    search = ctypes.util.find_library
    if found:
        ctypes.util.find_library = lambda name: found if "mpv" in name else search(name)
    try:
        import mpv
    except OSError as e:
        raise SystemExit(f"the mpv engine can't load libmpv ({e}); set LIBMPV_PATH") from e
    finally:
        ctypes.util.find_library = search
    return mpv


def end_to_event(reason):
    """Map an end-file reason onto isoplay's event names (MpvEventEndFile: 0 EOF, 4 ERROR;
    RESTARTED/QUIT/ABORTED keep quiet)."""
    return {0: "end", 4: "error"}.get(reason)


# ISO 639 codes (mpv reports 3-letter bibliographic, sometimes 2-letter) to the names a
# track menu shows.
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
    """One line for the track menu: title, language name and friendly codec."""
    title = (item.get("title") or "").strip()
    lang = LANGS.get((item.get("lang") or "").lower(), item.get("lang") or "")
    codec = CODECS.get((item.get("codec") or "").lower(), item.get("codec") or "")
    label = " · ".join(p for p in (title, lang) if p)
    if codec:
        label = f"{label} ({codec})" if label else codec
    return label


def track_entries(track_list, kind, selected):
    """([(tid, label)], current_tid) for audio or subtitle tracks, Off first. Discs often list a
    language twice, so repeats are numbered from their second showing."""
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


# Set one at a time after construction, so a libmpv missing one still starts. Discs arrive over
# the loopback proxy and plain videos straight over https.
OPTIONS = {
    "osc": "no",  # only exists in builds with Lua
    "cache": "yes",  # always keep a demuxer cache, even for inputs mpv deems local
    "demuxer-readahead-secs": "10",  # headroom past the play head (default 1); seeks stall less
    "stream-buffer-size": "1048576",  # fewer round trips per megabyte (default 128 KiB)
    "network-timeout": "30",  # idle seconds before a dead socket errors (the open deadline is 45)
    "curl-connect-timeout": "15",
    "curl-max-retries": "8",  # resumes at the next missing byte after a drop or an HTTP/2 reset
    "curl-max-request-size": str(16 << 20),  # hosts that throttle one long-lived connection
    "force-seekable": "yes",  # hosts that omit Accept-Ranges still honor Range on GET
}


# Polled by the app every tick. Read from values mpv pushes, never by asking it: a get blocks
# until mpv's core is free, and a disc seek over a slow link holds the core for seconds.
STATE = ("time-pos", "duration", "percent-pos", "core-idle", "pause", "mute", "disc-menu-active", "seeking",
         "chapters", "chapter", "track-list", "aid", "sid", "playlist-count", "video-params")
SPDIF = "ac3,eac3,dts,dts-hd,truehd"
USERINFO = re.compile(r"(://)[^/\s@]+@")


class MpvEngine:
    _fit = "fit"

    def __init__(self):
        self.mpv = binding()
        self.log = collections.deque(maxlen=500)
        debug = os.environ.get("ISOPLAY_DEBUG")

        def log(level, prefix, text):
            self.log.append(f"[{level}] {prefix}: {text.rstrip()}")
            if debug:
                print(f"[mpv {level} {prefix}] {text.strip()}", flush=True)

        self.player = self.mpv.MPV(config="no", vo="libmpv", idle="yes", keep_open="no",
                                   input_default_bindings="no", save_watch_history="no", user_agent=UA,
                                   hwdec="auto-safe", loglevel="verbose" if debug == "verbose" else "info",
                                   log_handler=log)
        self._version = self.player.mpv_version
        if self.player.mpv_version_tuple < (0, 42, 0):
            self.player.terminate()
            raise SystemExit(f"the mpv engine needs mpv 0.42.0 or newer for disc menus; {self._version} is installed.")
        self.video_widget = None
        self.render_context = None
        self.emit = None
        self.state = {}
        self._vout = False
        self._cache_state = None
        self._stalled = False  # paused-for-cache
        self._seeking = False
        # discnav round trips can block behind stream reads, so hovers go through a worker that
        # forwards only the latest; a click bumps the generation and drops older hovers.
        self._mouse_lock = threading.Lock()
        self._mouse_queue = queue.Queue()
        self._mouse_gen = 0
        threading.Thread(target=self._mouse_drain, daemon=True).start()
        self._apply_options()
        self._observe()
        player = self.player

        @player.event_callback("end-file")
        def _end(event):
            name = end_to_event(event.data.reason)
            if name:
                self._emit(name)

    def _apply_options(self):
        options = dict(OPTIONS)
        if sys.platform != "darwin" and os.environ.get("SSL_CERT_FILE"):  # an OpenSSL libcurl has no system store
            options["tls-ca-file"] = os.environ["SSL_CERT_FILE"]
        for name, value in options.items():
            try:
                self.player[name] = value
            except Exception as e:  # noqa: BLE001 - an older or newer libmpv without this option
                self.log.append(f"[engine] option {name} not applied: {e!r}")

    def _send(self, *args):
        """A command or `set`, queued without waiting for mpv's core, in order with the others."""
        try:
            self.player.command_async(*args)
        except Exception as e:  # noqa: BLE001 - rejected outright, e.g. nothing loaded
            self.log.append(f"[engine] {args[0]} {args[1] if len(args) > 1 else ''} failed: {e!r}")

    def _set(self, name, value):
        self._send("set", name, value if isinstance(value, str) else str(value))

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

        def _state(name, value):
            self.state[name.replace("-", "_")] = value

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
            _state(name, value)
            self._seeking = bool(value)
            self._report()

        def _video(name, value):
            if value and not self._vout:
                self._vout = True
                self._emit("vout")

        for name in STATE:
            if name != "seeking":
                player.observe_property(name, _state)
        player.observe_property("paused-for-cache", _stalled)
        player.observe_property("cache-buffering-state", _cache)
        player.observe_property("seeking", _seeking)
        player.observe_property("video-format", _video)

    def _emit(self, name):
        if self.emit:
            self.emit(name)

    def set_bridge(self, emit):
        self.emit = emit

    def version(self):
        return self._version

    def log_tail(self):
        return USERINFO.sub(r"\1***@", "\n".join(self.log))

    # --- build and the widget video is drawn in -----------------------------------------------------

    def surface(self, parent):
        self.video_widget = MpvVideoWidget(self, parent)
        return self.video_widget

    def settle(self, surface):
        pass  # the render context is created by the widget's initializeGL, once there is a GL context

    def shutdown(self):
        self._mouse_queue.put_nowait(None)
        if self.render_context:  # mpv requires the render context freed, GL current, before terminate
            self.video_widget.makeCurrent()
            self.render_context.free()
            self.video_widget.doneCurrent()
            self.render_context = None
        self.player.terminate()

    def _mouse_drain(self):
        """Forward hover positions, latest only, until shutdown. Runs off the Qt thread so
        a move stuck behind a slow stream read never freezes the interface; anything
        queued behind a newer hover, or behind a click, is dropped unsent."""
        while True:
            position = self._mouse_queue.get()
            if position is None:
                return
            x, y, gen = position
            while True:  # a newer hover already supersedes this one
                try:
                    position = self._mouse_queue.get_nowait()
                except queue.Empty:
                    break
                if position is None:
                    return
                x, y, gen = position
            with self._mouse_lock:
                current = self._mouse_gen
            if gen != current:
                continue
            try:
                self.player.command("discnav", "mouse-move", f"{x:.4f}", f"{y:.4f}")
            except Exception:  # noqa: BLE001 - the menu went away mid-hover
                pass

    # --- media ---------------------------------------------------------------------------------------

    def open(self, mrl, kind=None, menus=True, start=0):
        self._vout = False
        self._stalled = self._seeking = False
        self._cache_state = None
        self.state.clear()
        self._set("pause", "no")
        if kind in ("dvd", "bluray"):
            self._set("disc-menu", "yes" if menus else "no")
            # A loopback URL as the device needs the patched libmpv (tools/build_mpv.py).
            self._set("bluray-device" if kind == "bluray" else "dvd-device", mrl)
            target = "bd://" if kind == "bluray" else "dvd://"
        else:
            target = mrl
        if start > 0:
            self._send("loadfile", target, "replace", -1, f"start={start:.3f}")
        else:
            self._send("loadfile", target, "replace")

    def close(self):
        self._send("stop")

    def has_media(self):
        return bool(self.state.get("playlist_count"))

    # --- state ---------------------------------------------------------------------------------------

    def playing(self):
        return self.state.get("core_idle") is False

    def _seconds(self, name):
        value = self.state.get(name)
        # an idle player has None, and menu editions report -1
        return float(value) if isinstance(value, (int, float)) and value >= 0 else 0.0

    def time(self):
        return int(self._seconds("time_pos") * 1000)

    def length(self):
        return int(self._seconds("duration") * 1000)

    def position(self):
        value = self.state.get("percent_pos")
        return value / 100.0 if isinstance(value, (int, float)) else -1.0

    def menu_active(self):
        return bool(self.state.get("disc_menu_active"))

    def seeking(self):
        return bool(self.state.get("seeking"))

    # --- transport -----------------------------------------------------------------------------------

    def pause(self):
        paused = not self.state.get("pause")
        self.state["pause"] = paused  # until mpv confirms, so a quick second press toggles back
        self._set("pause", "yes" if paused else "no")

    def seek(self, ms):
        self._send("seek", f"{ms / 1000.0:.3f}", "absolute")

    def set_position(self, fraction):
        self._send("seek", f"{fraction * 100:.3f}", "absolute-percent")

    def step_chapter(self, d):
        self._send("add", "chapter", str(d))

    def chapter_count(self):
        return self.state.get("chapters") or 0

    def chapter(self):
        return max(0, self.state.get("chapter") or 0)

    def set_volume(self, value):
        self._set("volume", int(value))

    def toggle_mute(self):
        muted = not self.state.get("mute")
        self.state["mute"] = muted
        self._set("mute", "yes" if muted else "no")
        return muted

    # --- tracks -----------------------------------------------------------------------------------------

    def tracks(self):
        items = self.state.get("track_list") or ()
        return {"audio": track_entries(items, "audio", self._currently("aid")),
                "sub": track_entries(items, "sub", self._currently("sid"))}

    def _currently(self, prop):
        value = self.state.get(prop)
        # "no"/False means Off and None means nothing loaded; neither is a track id to hand back
        return value if isinstance(value, int) and not isinstance(value, bool) else -1

    def select_track(self, kind, tid):
        self._set("aid" if kind == "audio" else "sid", "no" if tid == -1 else tid)

    def subtitles(self, uri):
        self._send("sub-add", uri, "select")
        return True

    def set_languages(self, audio, sub):
        """Comma lists of ISO 639 codes, "" for the file's own default; sub "no" starts with none."""
        self._set("alang", audio)
        self._set("slang", "" if sub == "no" else sub)
        self._set("sid", "no" if sub == "no" else "auto")

    def set_passthrough(self, on):
        self._set("audio-spdif", SPDIF if on else "")

    # --- appearance ------------------------------------------------------------------------------------

    def set_fit(self, mode, width, height):
        self._fit = mode
        self._set("video-aspect-override", f"{max(width, 1)}:{max(height, 1)}" if mode == "stretch" else "no")
        self._set("panscan", "1.0" if mode == "fill" else "0.0")

    # --- disc menus ------------------------------------------------------------------------------------

    def video_rect(self, width, height):
        """Where the picture is drawn inside a widget of width x height, in the same pixels:
        fit letterboxes it, fill crops the overflow, stretch distorts it edge to edge. Mouse
        positions map against this rect, not the widget, so clicks on the bars never reach
        the disc and 4:3 menus in a wide window still land."""
        params = self.state.get("video_params") or {}
        vw, vh = params.get("dw") or params.get("w"), params.get("dh") or params.get("h")
        if not vw or not vh or not width or not height:
            return 0.0, 0.0, float(max(1, width)), float(max(1, height))
        if self._fit == "stretch":
            return 0.0, 0.0, float(width), float(height)
        scale = (max if self._fit == "fill" else min)(width / vw, height / vh)
        dw, dh = vw * scale, vh * scale
        return (width - dw) / 2, (height - dh) / 2, dw, dh

    def navigate(self, action):
        self._send("discnav", action)

    def menu_mouse_async(self, x, y):
        with self._mouse_lock:
            gen = self._mouse_gen
        self._mouse_queue.put_nowait((x, y, gen))

    def menu_mouse(self, x, y, click):
        """Normalized 0..1 position in the render window (top-left 0,0), then let mpv map it
        onto the disc itself. A click first moves onto the button under the cursor, then
        activates it exactly the way Enter does: mouse-click's own activation silently
        no-ops on discs where its lookup misses (mpv reports success regardless), while
        select on the hovered button is the path keyboard navigation proves every time."""
        position = f"{max(0.0, min(1.0, x)):.4f}", f"{max(0.0, min(1.0, y)):.4f}"
        if click:
            with self._mouse_lock:  # queued hovers older than this are now stale
                self._mouse_gen += 1
        self._send("discnav", "mouse-move", *position)
        if click:
            self._send("discnav", "select")


class MpvVideoWidget(QOpenGLWidget):
    """Draws mpv's frames into the Qt window itself, so mouse events reach disc menus."""

    frame_ready = Signal()
    clicked = Signal()
    double_clicked = Signal()
    context_requested = Signal(QPoint)
    wheeled = Signal(int)

    def __init__(self, engine, parent):
        super().__init__(parent)
        self.engine = engine
        self.setMouseTracking(True)  # hover must reach disc menus with no button held
        self._gl_ref = None  # the ctypes callback has to outlive initializeGL's scope
        self._last_mouse = (0.0, 0.0, 0.0)  # last forwarded position and when: moves coalesce
        self._wheel = 0  # trackpads send fractions of a notch
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
        if event.button() == Qt.RightButton:
            self.context_requested.emit(event.globalPosition().toPoint())
        elif event.button() == Qt.LeftButton:
            if self.engine.menu_active():
                self._menu_mouse(event, click=True)
            else:
                self.clicked.emit()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.double_clicked.emit()

    def wheelEvent(self, event):
        self._wheel += event.angleDelta().y()
        steps = int(self._wheel / 120)
        if steps:
            self._wheel -= steps * 120
            self.wheeled.emit(steps)
        event.accept()

    def _menu_mouse(self, event, click):
        if not self.engine.menu_active():
            return
        w, h = max(1, self.width()), max(1, self.height())
        ox, oy, dw, dh = self.engine.video_rect(w, h)
        pos = event.position()
        nx, ny = (pos.x() - ox) / dw, (pos.y() - oy) / dh
        if not 0.0 <= nx <= 1.0 or not 0.0 <= ny <= 1.0:
            return  # on the letterbox bars, not the picture: nothing to forward
        if not click:
            # Tracking reports every pixel, and each report used to be a synchronous round
            # trip into mpv that blocks this thread; at tracking rates stale positions
            # queue behind the cursor, which is what hover lag is. Coalesce to the latest
            # position here, and let the engine's worker forward that off this thread.
            now = time.monotonic()
            lx, ly, lt = self._last_mouse
            if (nx, ny) == (lx, ly) or now - lt < 0.05:
                return
            self._last_mouse = (nx, ny, now)
            self.engine.menu_mouse_async(nx, ny)
        else:  # queued in order: the move lands before the select
            self.engine.menu_mouse(nx, ny, click)
