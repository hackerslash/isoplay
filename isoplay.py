#!/usr/bin/env python3
"""isoplay — play Blu-ray/DVD ISOs (with menus) and ordinary media, local or over HTTP."""
import os
import re
import sys
import threading
import urllib.parse
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
VLC_DIR = BASE / "vlc"
LIB = {"darwin": "lib/libvlc.dylib", "win32": "libvlc.dll"}.get(sys.platform, "lib/libvlc.so.5")
if VLC_DIR.is_dir():
    os.environ["PYTHON_VLC_LIB_PATH"] = str(VLC_DIR / LIB)
    os.environ["PYTHON_VLC_MODULE_PATH"] = os.environ["VLC_PLUGIN_PATH"] = str(VLC_DIR / "plugins")
    jars = sorted((VLC_DIR / "plugins").rglob("libbluray-j2se-*.jar"))
    if jars:
        os.environ["LIBBLURAY_CP"] = str(jars[-1])
    if sys.platform == "win32":
        os.add_dll_directory(str(VLC_DIR))
    elif sys.platform.startswith("linux"):  # libvlc's DT_NEEDED libvlccore is outside the loader path
        import ctypes
        ctypes.CDLL(str(VLC_DIR / "lib/libvlccore.so.9"), mode=ctypes.RTLD_GLOBAL)
if (BASE / "jre").is_dir():
    os.environ["JAVA_HOME"] = str(BASE / "jre")
if sys.platform.startswith("linux"):
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")  # libVLC embeds into X11 windows only

import time  # noqa: E402

import vlc  # noqa: E402
from PySide6.QtCore import QObject, QPoint, QRect, QSettings, QSize, Qt, QTimer, Signal  # noqa: E402
from PySide6.QtGui import QAction, QCursor, QIcon, QImage, QKeySequence, QPainter, QPixmap  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QMainWindow, QMenu,
    QPushButton, QSlider, QStackedWidget, QToolButton, QVBoxLayout, QWidget,
)

UA = "Mozilla/5.0 isoplay"


def is_url(src):
    return src.split("://", 1)[0].lower() in ("http", "https", "ftp")


BLOCK = 4 << 20


class RangeCache:
    """Local HTTP endpoint for a remote disc image, backed by a block cache with parallel read-ahead.

    libbluray/dvdnav seek constantly and each upstream request costs ~1 s, so menus need the cache;
    one upstream connection (~1.5 MB/s on worker hosts) is too slow for Blu-ray, so playback needs
    several blocks in flight at once.
    """
    # ponytail: in-memory LRU (keep*4 MiB); switch to a disk cache if RAM gets tight

    def __init__(self, url, keep=64, ahead=6):
        self.url, self.keep, self.ahead = url, keep, ahead
        self.blocks, self.pending, self.lock = OrderedDict(), {}, threading.Lock()
        self.pool = ThreadPoolExecutor(ahead + 2)
        self.size = self._size()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        self.server.handle_error = lambda *a: None  # players drop connections on every seek
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.mrl = f"http://127.0.0.1:{self.server.server_port}/disc.iso"

    def _get(self, start, end):
        req = Request(self.url, headers={"Range": f"bytes={start}-{end}", "User-Agent": UA})
        return urlopen(req, timeout=30)

    def _size(self):
        with self._get(0, 0) as r:
            if r.status != 206:
                raise OSError("server ignores Range requests, so a disc cannot be streamed")
            return int(r.headers["Content-Range"].rsplit("/", 1)[1])

    def _load(self, i):
        start = i * BLOCK
        end = min(start + BLOCK, self.size) - 1
        try:
            for attempt in range(3):
                try:
                    with self._get(start, end) as r:
                        data = r.read()
                    if len(data) == end - start + 1:
                        break
                except OSError:
                    if attempt == 2:
                        raise
            else:
                raise OSError(f"short read at block {i}")
            with self.lock:
                self.blocks[i] = data
                while len(self.blocks) > self.keep:
                    self.blocks.popitem(last=False)
            return data
        finally:
            with self.lock:
                self.pending.pop(i, None)

    def block(self, i):
        with self.lock:
            if i in self.blocks:
                self.blocks.move_to_end(i)
                f = Future()
                f.set_result(self.blocks[i])
                return f
            if i not in self.pending:
                self.pending[i] = self.pool.submit(self._load, i)
            return self.pending[i]

    def read(self, pos, n):
        out = bytearray()
        while n > 0 and pos < self.size:
            i, off = divmod(pos, BLOCK)
            b = self.block(i).result()[off:off + n]
            out += b
            pos += len(b)
            n -= len(b)
        return bytes(out)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.pool.shutdown(wait=False, cancel_futures=True)

    def _handler(self):
        cache = self
        last = (cache.size - 1) // BLOCK

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _head(self):
                start, end, partial = 0, cache.size - 1, False
                m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
                if m and (m[1] or m[2]):
                    partial = True
                    if m[1]:
                        start = int(m[1])
                        end = min(int(m[2]), end) if m[2] else end
                    else:
                        start = max(0, cache.size - int(m[2]))
                if start >= cache.size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{cache.size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                self.send_response(206 if partial else 200)
                if partial:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{cache.size}")
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Type", "application/octet-stream")
                self.end_headers()
                return start, end

            def do_HEAD(self):
                self._head()

            def do_GET(self):
                r = self._head()
                if not r:
                    return
                pos, end = r
                first = pos // BLOCK
                try:
                    while pos <= end:
                        i, off = divmod(pos, BLOCK)
                        if i > first:  # reader is streaming, not probing: keep blocks in flight
                            for j in range(i + 1, min(i + cache.ahead, last) + 1):
                                cache.block(j)
                        chunk = cache.block(i).result()[off:off + end - pos + 1]
                        self.wfile.write(chunk)
                        pos += len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception:
                    self.close_connection = True

        return H


VIDEO_EXT = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm", ".ts", ".m2ts", ".mts", ".wmv", ".flv",
             ".mpg", ".mpeg", ".vob", ".m3u8", ".mpd", ".mp3", ".flac", ".ogg", ".opus", ".wav", ".m4a"}
DISC_EXT = {".iso", ".img", ".bin", ".udf"}


def suffix(src):
    path = urllib.parse.urlparse(src).path if is_url(src) else src
    return Path(urllib.parse.unquote(path)).suffix.lower()


def iso_signature(data):
    """ISO 9660 (CD001) or UDF (BEA01) volume descriptor at sector 16: DVD5/9, BD25/50/66/100."""
    return data[32769:32774] in (b"CD001", b"BEA01")


def disc_kind(data):
    if b"BDMV" in data:
        return "bluray"
    if b"VIDEO_TS" in data:
        return "dvd"
    return None


def sniff(url):
    try:
        req = Request(url, headers={"Range": "bytes=32768-32775", "User-Agent": UA})
        with urlopen(req, timeout=20) as r:
            return r.status == 206 and iso_signature(b"\0" * 32768 + r.read(8))
    except OSError:
        return False


def prepare(src):
    """How libVLC should open src: (mrl, "bluray" | "dvd" | None, RangeCache | None). Blocks on I/O."""
    ext = suffix(src)
    if not is_url(src):
        p = Path(src)
        if p.is_dir():
            for sub, kind in (("BDMV", "bluray"), ("VIDEO_TS", "dvd")):
                if (p / sub).is_dir() or (p.name.upper() == sub):
                    root = p if (p / sub).is_dir() else p.parent
                    return ("bluray" if kind == "bluray" else "dvd") + root.as_uri()[4:], kind, None
            return src, None, None
        if ext in VIDEO_EXT:
            return src, None, None
        with open(src, "rb") as f:
            data = f.read(BLOCK)
        return src, (disc_kind(data) if iso_signature(data) else None), None
    if ext in VIDEO_EXT or (ext not in DISC_EXT and not sniff(src)):
        return src, None, None
    proxy = RangeCache(src)
    data = proxy.read(0, BLOCK)
    if not iso_signature(data):
        proxy.close()
        return src, None, None
    return proxy.mrl, disc_kind(data), proxy


def fmt(ms):
    s = max(ms, 0) // 1000
    return f"{s // 3600}:{s // 60 % 60:02}:{s % 60:02}"


CSS = """
QMainWindow, #welcome { background: #000; }
#welcome QLabel { color: #8e8e93; font-size: 13px; }
#welcome QLabel#title { color: #f2f2f7; font-size: 30px; font-weight: 600; }
#welcome QPushButton, #welcome QToolButton { color: #f2f2f7; background: #2c2c2e; border: none;
    border-radius: 8px; padding: 0 18px; min-height: 32px; font-size: 13px; }
#welcome QPushButton:hover, #welcome QToolButton:hover { background: #3a3a3c; }
#welcome QToolButton:checked { background: #0a84ff; }
#panel { background: rgba(30, 30, 32, 222); border-radius: 12px; }
#panel QPushButton { background: transparent; border: none; border-radius: 6px; padding: 5px; }
#panel QPushButton:hover { background: rgba(255, 255, 255, 30); }
#panel QLabel { color: #d1d1d6; font-size: 12px; }
#panel QSlider::groove:horizontal { height: 4px; background: rgba(255,255,255,55); border-radius: 2px; }
#panel QSlider::sub-page:horizontal { background: #f2f2f7; border-radius: 2px; }
#panel QSlider::handle:horizontal { width: 12px; height: 12px; margin: -4px 0; border-radius: 6px;
    background: #ffffff; }
#osd { color: #f2f2f7; background: rgba(30, 30, 32, 222); border-radius: 8px; padding: 6px 12px;
    font-size: 13px; }
"""


ICONS = {  # 24x24 path data (Material Symbols, Apache-2.0)
    "play": "M8 5v14l11-7z",
    "pause": "M6 5h4v14H6zm8 0h4v14h-4z",
    "menu": "M4 6h16v2H4zm0 5h16v2H4zm0 5h16v2H4z",
    "audio": "M12 3v10.55A4 4 0 1 0 14 17V7h4V3z",
    "subs": "M20 4H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2zM4 12h4v2H4zm10 6H4v-2h10z"
            "m6 0h-4v-2h4zm0-4H10v-2h10z",
    "volume": "M3 9v6h4l5 5V4L7 9H3zm13.5 3A4.5 4.5 0 0 0 14 7.97v8.05A4.48 4.48 0 0 0 16.5 12zM14 3.23v2.06"
              "a7 7 0 0 1 0 13.42v2.06A9 9 0 0 0 14 3.23z",
    "muted": "M16.5 12A4.5 4.5 0 0 0 14 7.97v2.21l2.45 2.45c.03-.2.05-.41.05-.63zM19 12c0 .94-.2 1.82-.54 2.64"
             "l1.51 1.51A8.8 8.8 0 0 0 21 12a9 9 0 0 0-7-8.77v2.06A7 7 0 0 1 19 12zM4.27 3 3 4.27 7.73 9H3v6h4"
             "l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.06a9 9 0 0 0 3.69-1.81L19.73 21 21 19.73l-9-9z"
             "M12 4 9.91 6.09 12 8.18V4z",
    "more": "M6 10a2 2 0 1 0 0 4 2 2 0 0 0 0-4zm6 0a2 2 0 1 0 0 4 2 2 0 0 0 0-4zm6 0a2 2 0 1 0 0 4 2 2 0 0 0 0-4z",
    "full": "M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z",
    "unfull": "M5 16h3v3h2v-5H5v2zm3-8H5v2h5V5H8v3zm6 11h2v-3h3v-2h-5v5zm2-11V5h-2v5h5V8h-3z",
}


def svg_icon(svg, size=48):
    """Render SVG markup or an ICONS key to a QIcon (QSvgRenderer, so no image-format plugin needed)."""
    if svg in ICONS:
        svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="#f2f2f7" d="{ICONS[svg]}"/></svg>'
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    QSvgRenderer(svg.encode() if isinstance(svg, str) else svg).render(p)
    p.end()
    return QIcon(QPixmap.fromImage(img))


class Overlay(QWidget):
    """Floating child window: the video is a native libVLC surface, so Qt widgets can't paint over it."""

    def __init__(self, owner, clickable):
        flags = Qt.Tool | Qt.FramelessWindowHint | Qt.WindowDoesNotAcceptFocus | Qt.NoDropShadowWindowHint
        super().__init__(owner, flags if clickable else flags | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setStyleSheet(CSS)


class Bridge(QObject):
    prepared = Signal(int, object)
    event = Signal(str)


class Player(QMainWindow):
    NAV = {Qt.Key_Up: vlc.NavigateMode.up, Qt.Key_Down: vlc.NavigateMode.down,
           Qt.Key_Left: vlc.NavigateMode.left, Qt.Key_Right: vlc.NavigateMode.right,
           Qt.Key_Return: vlc.NavigateMode.activate, Qt.Key_Enter: vlc.NavigateMode.activate}

    def __init__(self):
        super().__init__()
        self.setWindowTitle("isoplay")
        self.resize(1100, 640)
        self.setAcceptDrops(True)
        self.setStyleSheet(CSS)
        self.settings = QSettings("isoplay", "isoplay")
        self.vlc = vlc.Instance(["--no-video-title-show", "--network-caching=3000"]
                                + (["-vv"] if os.environ.get("ISOPLAY_DEBUG") else []))
        self.mp = self.vlc.media_player_new()
        self.mp.video_set_key_input(False)
        self.mp.video_set_mouse_input(True)
        self.disc = self.proxy = self.current = None
        self.token = 0
        self.last_pos, self.last_move = QPoint(), 0.0

        self.bridge = Bridge()
        self.bridge.prepared.connect(self.start)
        self.bridge.event.connect(self.on_event)
        ev = self.mp.event_manager()
        for t, name in ((vlc.EventType.MediaPlayerEncounteredError, "error"),
                        (vlc.EventType.MediaPlayerVout, "vout"),
                        (vlc.EventType.MediaPlayerEndReached, "end")):
            ev.event_attach(t, lambda e, n=name: self.bridge.event.emit(n))

        self.act_open = self.action("Open File…", QKeySequence.Open, self.browse)
        self.act_url = self.action("Open URL…", "Ctrl+L", self.ask_url)
        self.act_paste = self.action("Play Copied Link", QKeySequence.Paste, self.paste)
        self.act_menus = self.action("Use Disc Menus", None, self.menus_changed)
        self.act_menus.setCheckable(True)
        self.act_menus.setChecked(self.settings.value("menus", True, type=bool))
        self.act_main = self.action("Replay Without Menus (Main Title)", None, self.replay_main)
        self.act_full = self.action("Fullscreen", "F", self.toggle_full)
        if sys.platform == "darwin":  # native menu bar costs no window space
            m = self.menuBar().addMenu("File")
            for a in (self.act_open, self.act_url, self.act_paste):
                m.addAction(a)
            m = self.menuBar().addMenu("Playback")
            for a in (self.act_menus, self.act_main, self.act_full):
                m.addAction(a)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.welcome_page())
        self.video = QFrame()
        self.video.setStyleSheet("background:#000")
        self.video.setAttribute(Qt.WA_NativeWindow)
        self.stack.addWidget(self.video)
        self.setCentralWidget(self.stack)
        wid = int(self.video.winId())
        {"darwin": self.mp.set_nsobject, "win32": self.mp.set_hwnd}.get(sys.platform, self.mp.set_xwindow)(wid)

        self.controls = Overlay(self, clickable=True)
        self.build_controls()
        self.osd = Overlay(self, clickable=False)
        self.osd_label = QLabel(self.osd, objectName="osd")
        self.osd_until = 0.0

        self.timer = QTimer(self, interval=200, timeout=self.tick)
        self.timer.start()

    def action(self, text, key, slot):
        a = QAction(text, self)
        if key:
            a.setShortcut(QKeySequence(key))
        a.triggered.connect(slot)
        self.addAction(a)
        return a

    def welcome_page(self):
        page = QWidget(objectName="welcome")
        v = QVBoxLayout(page)
        v.addStretch()
        title = QLabel("isoplay", objectName="title", alignment=Qt.AlignCenter)
        sub = QLabel("Blu-ray · UHD · DVD images with menus, and any video — files or links",
                     alignment=Qt.AlignCenter)
        row = QHBoxLayout()
        row.addStretch()
        for text, slot in (("Open File…", self.browse), ("Open URL…", self.ask_url)):
            row.addWidget(QPushButton(text, clicked=slot, cursor=Qt.PointingHandCursor))
        menus = QToolButton(cursor=Qt.PointingHandCursor)
        menus.setDefaultAction(self.act_menus)
        menus.setText("Disc menus")
        row.addWidget(menus)
        row.addStretch()
        mod = "⌘" if sys.platform == "darwin" else "Ctrl+"
        hint = QLabel(f"Drop a file here  ·  {mod}V plays a copied link", alignment=Qt.AlignCenter)
        for w in (title, sub):
            v.addWidget(w)
        v.addSpacing(18)
        v.addLayout(row)
        v.addSpacing(10)
        v.addWidget(hint)
        v.addStretch()
        return page

    def build_controls(self):
        panel = QFrame(self.controls, objectName="panel")
        lay = QVBoxLayout(self.controls)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(panel)
        row = QHBoxLayout(panel)
        row.setContentsMargins(10, 4, 10, 4)
        row.setSpacing(4)

        def btn(icon, tip, slot):
            b = QPushButton(toolTip=tip, focusPolicy=Qt.NoFocus, cursor=Qt.PointingHandCursor,
                            icon=svg_icon(icon), iconSize=QSize(20, 20))
            if slot:
                b.clicked.connect(slot)
            row.addWidget(b)
            return b

        self.play_btn = btn("pause", "Play/Pause (Space)", self.mp.pause)
        self.play_icons = {True: svg_icon("pause"), False: svg_icon("play")}
        self.pos_lbl = QLabel("0:00:00")
        row.addWidget(self.pos_lbl)
        self.seek = QSlider(Qt.Horizontal, maximum=10000, focusPolicy=Qt.NoFocus)
        self.seek.sliderReleased.connect(lambda: self.mp.set_position(self.seek.value() / 10000))
        row.addWidget(self.seek, 1)
        self.len_lbl = QLabel("0:00:00")
        row.addWidget(self.len_lbl)
        self.menu_btn = btn("menu", "Disc menu (M)", lambda: self.mp.navigate(vlc.NavigateMode.popup))
        self.audio_btn = btn("audio", "Audio track", lambda: self.track_menu(
            self.audio_btn, self.mp.audio_get_track_description, self.mp.audio_get_track, self.mp.audio_set_track))
        self.sub_btn = btn("subs", "Subtitles", lambda: self.track_menu(
            self.sub_btn, self.mp.video_get_spu_description, self.mp.video_get_spu, self.mp.video_set_spu))
        self.mute_btn = btn("volume", "Mute", self.toggle_mute)
        self.vol = QSlider(Qt.Horizontal, maximum=100, value=100, focusPolicy=Qt.NoFocus, fixedWidth=70,
                           toolTip="Volume (↑/↓ on video files)")
        self.vol.valueChanged.connect(self.mp.audio_set_volume)
        row.addWidget(self.vol)
        more = btn("more", "More", None)
        menu = QMenu(more)
        for a in (self.act_open, self.act_url, self.act_paste, None, self.act_menus, self.act_main):
            menu.addAction(a) if a else menu.addSeparator()
        more.setMenu(menu)
        more.setStyleSheet("QPushButton::menu-indicator { width: 0; }")
        self.full_btn = btn("full", "Fullscreen (F)", self.toggle_full)

    def toggle_mute(self):
        self.mp.audio_toggle_mute()
        self.mute_btn.setIcon(svg_icon("volume" if self.mp.audio_get_mute() == 0 else "muted"))

    def track_menu(self, button, describe, current, select):
        menu = QMenu(self)
        cur = current()
        for tid, name in describe() or []:
            a = menu.addAction(name.decode(errors="replace") if isinstance(name, bytes) else str(name))
            a.setCheckable(True)
            a.setChecked(tid == cur)
            a.triggered.connect(lambda _=False, t=tid: select(t))
        if menu.isEmpty():
            menu.addAction("None").setEnabled(False)
        menu.exec(button.mapToGlobal(QPoint(0, -menu.sizeHint().height())))

    # --- opening ---------------------------------------------------------------------------------

    def browse(self):
        f, _ = QFileDialog.getOpenFileName(
            self, "Open", self.settings.value("dir", ""),
            "Discs and video (*.iso *.img *.mkv *.mp4 *.m4v *.m2ts *.ts *.avi *.mov *.webm *.vob);;All files (*)")
        if f:
            self.settings.setValue("dir", str(Path(f).parent))
            self.play(f)

    def ask_url(self):
        clip = QApplication.clipboard().text().strip()
        url, ok = QInputDialog.getText(self, "Open URL", "Link to a disc image or video:",
                                       text=clip if is_url(clip) else "")
        if ok and url.strip():
            self.play(url.strip())

    def paste(self):
        text = QApplication.clipboard().text().strip()
        if is_url(text) or (text and Path(text).exists()):
            self.play(text)
        else:
            self.flash("Clipboard has no link or file path")

    def menus_changed(self):
        self.settings.setValue("menus", self.act_menus.isChecked())

    def replay_main(self):
        if self.current:
            self.act_menus.setChecked(False)
            self.menus_changed()
            self.play(self.current)

    def play(self, src):
        if not src:
            return
        self.token += 1
        self.mp.stop()
        if self.proxy:
            self.proxy.close()
            self.proxy = None
        self.current, self.disc = src, None
        name = urllib.parse.unquote(src.rstrip("/").rsplit("/", 1)[-1].split("?")[0])
        self.setWindowTitle(name or "isoplay")
        self.stack.setCurrentWidget(self.video)
        self.flash("Opening…", sticky=True)
        token = self.token

        def work():
            try:
                result = prepare(src)
            except Exception as e:  # noqa: BLE001 — shown to the user
                result = e
            self.bridge.prepared.emit(token, result)

        threading.Thread(target=work, daemon=True).start()

    def start(self, token, result):
        if token != self.token:  # a newer play() superseded this one
            if isinstance(result, tuple) and result[2]:
                result[2].close()
            return
        if isinstance(result, Exception):
            self.flash(f"Can't open: {result}", seconds=8)
            return
        mrl, self.disc, self.proxy = result
        menus = self.act_menus.isChecked()
        media = self.vlc.media_new(mrl) if "://" in mrl else self.vlc.media_new_path(mrl)
        media.add_option(f":http-user-agent={UA}")
        if self.disc == "dvd" and menus and not mrl.startswith("dvd:"):
            media.add_option(":demux=dvdnav")  # auto-probe picks raw PS over HTTP and loses menus
        if self.disc == "bluray" and not menus:
            media.add_option(":no-bluray-menu")
        self.mp.set_media(media)
        self.mp.play()
        label = {"bluray": "Blu-ray", "dvd": "DVD"}.get(self.disc)
        self.flash(f"{label} — loading {'menu' if menus else 'main title'}…" if label else "Loading…", sticky=True)
        self.menu_btn.setVisible(bool(self.disc) and menus)

    def on_event(self, name):
        if name == "vout":
            self.flash({"bluray": "Blu-ray", "dvd": "DVD"}.get(self.disc, ""), seconds=2 if self.disc else 0)
        elif name == "error":
            hint = "  — try Replay Without Menus (⋯)" if self.disc and self.act_menus.isChecked() else ""
            self.flash(f"Can't play this{hint}", seconds=10)

    # --- overlay -----------------------------------------------------------------------------------

    def flash(self, text, seconds=3.0, sticky=False):
        self.osd_label.setText(text)
        self.osd_label.adjustSize()
        self.osd.resize(self.osd_label.size())
        self.osd_until = float("inf") if sticky else (time.monotonic() + seconds if text else 0)
        self.place()

    def place(self):
        top_left = self.video.mapToGlobal(QPoint(0, 0)) if self.stack.currentWidget() is self.video \
            else self.stack.mapToGlobal(QPoint(0, 0))
        area = QRect(top_left, self.stack.size())
        w = min(820, area.width() - 32)
        self.controls.setGeometry(area.center().x() - w // 2, area.bottom() - 18 - 44, w, 44)
        self.osd.move(area.left() + 16, area.top() + 16)

    def tick(self):
        now = time.monotonic()
        pos = QCursor.pos()
        if pos != self.last_pos:
            self.last_pos = pos
            if self.frameGeometry().contains(pos):
                self.last_move = now
        active = QApplication.applicationState() == Qt.ApplicationActive and not self.isMinimized()
        on_video = self.stack.currentWidget() is self.video
        playing = self.mp.is_playing()
        show = active and on_video and (not playing or now - self.last_move < 2.5
                                        or self.controls.geometry().contains(pos))
        if show != self.controls.isVisible():
            self.place()
            self.controls.setVisible(show)
        osd = active and on_video and now < self.osd_until
        if osd != self.osd.isVisible():
            self.place()
            self.osd.setVisible(osd)
        if show:
            if self.play_btn.icon().cacheKey() != self.play_icons[bool(playing)].cacheKey():
                self.play_btn.setIcon(self.play_icons[bool(playing)])
            if not self.seek.isSliderDown():
                self.seek.setValue(int(self.mp.get_position() * 10000))
            self.pos_lbl.setText(fmt(self.mp.get_time()))
            self.len_lbl.setText(fmt(self.mp.get_length()))

    def moveEvent(self, e):
        self.place()
        super().moveEvent(e)

    def resizeEvent(self, e):
        self.place()
        super().resizeEvent(e)

    def toggle_full(self):
        self.full_btn.setIcon(svg_icon("full" if self.isFullScreen() else "unfull"))
        self.showNormal() if self.isFullScreen() else self.showFullScreen()
        QTimer.singleShot(300, self.place)

    # --- input -------------------------------------------------------------------------------------

    def keyPressEvent(self, e):
        k, shift = e.key(), e.modifiers() & Qt.ShiftModifier
        disc_nav = self.disc and self.act_menus.isChecked()
        if k in (Qt.Key_Left, Qt.Key_Right) and (shift or not disc_nav):
            self.mp.set_time(max(0, self.mp.get_time() + (10000 if k == Qt.Key_Right else -10000)))
        elif k in (Qt.Key_Up, Qt.Key_Down) and not disc_nav:
            self.vol.setValue(self.vol.value() + (5 if k == Qt.Key_Up else -5))
        elif k in self.NAV and disc_nav:
            self.mp.navigate(self.NAV[k])
        elif k == Qt.Key_Space:
            self.mp.pause()
        elif k == Qt.Key_M:
            self.mp.navigate(vlc.NavigateMode.popup)
        elif k in (Qt.Key_PageUp, Qt.Key_PageDown):
            self.mp.next_chapter() if k == Qt.Key_PageDown else self.mp.previous_chapter()
        elif k == Qt.Key_Escape and self.isFullScreen():
            self.toggle_full()
        else:
            super().keyPressEvent(e)
        self.last_move = time.monotonic()

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() or e.mimeData().hasText():
            e.acceptProposedAction()

    def dropEvent(self, e):
        md = e.mimeData()
        if md.hasUrls():
            u = md.urls()[0]
            self.play(u.toLocalFile() if u.isLocalFile() else u.toString())
        else:
            self.play(md.text().strip())

    def closeEvent(self, e):
        self.timer.stop()
        self.mp.stop()
        if self.proxy:
            self.proxy.close()
        super().closeEvent(e)


def check():
    """Bundle smoke test for CI: libVLC loads, and the BD-J jar and JVM are where libbluray looks.
    Exit code only on Windows: a windowed exe has no stdout, and an uncaught error opens a dialog."""
    say = print if sys.stdout else (lambda *a: None)
    try:
        say("libvlc", vlc.libvlc_get_version().decode())
        assert vlc.Instance(["--no-video"]) is not None, "libvlc failed to start"
        if VLC_DIR.is_dir():
            assert Path(os.environ.get("LIBBLURAY_CP", "")).is_file(), "libbluray jar missing"
            jvm = {"darwin": "lib/server/libjvm.dylib", "win32": "bin/server/jvm.dll"}.get(sys.platform, "lib/server/libjvm.so")
            assert (BASE / "jre" / jvm).is_file(), "bundled JRE missing"
            say("jar", os.environ["LIBBLURAY_CP"])
    except Exception as e:
        say("check failed:", e)
        os._exit(1)
    say("ok")


def main():
    if "--check" in sys.argv:
        return check()
    app = QApplication(sys.argv)
    app.setApplicationName("isoplay")
    app.setWindowIcon(svg_icon((BASE / "assets/icon.svg").read_bytes(), 256))
    w = Player()
    w.show()
    if len(sys.argv) > 1:
        w.play(sys.argv[1])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
