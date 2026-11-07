#!/usr/bin/env python3
"""isoplay: play Blu-ray and DVD images with their menus, and ordinary video, from files or links."""
import json
import os
import re
import sys
import tempfile
import threading
import urllib.parse
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

from disc_image import project_iso9660

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
import certifi  # noqa: E402
os.environ.setdefault("SSL_CERT_FILE", certifi.where())  # frozen builds inherit the build host's missing trust store

if sys.platform.startswith("linux"):
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")  # libVLC embeds into X11 windows only

import time  # noqa: E402

import vlc  # noqa: E402
from PySide6.QtCore import (  # noqa: E402
    QEasingCurve, QEvent, QObject, QPoint, QPropertyAnimation, QRect, QSettings, QSize, Qt, QTimer, Signal,
)
from PySide6.QtGui import (  # noqa: E402
    QAction, QColor, QCursor, QFont, QIcon, QImage, QKeySequence, QPainter, QPen, QPixmap,
)
from PySide6.QtSvg import QSvgRenderer  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QCheckBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QMainWindow,
    QMenu, QPushButton, QSlider, QStackedWidget, QVBoxLayout, QWidget,
)

__version__ = "0.1.3"
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
        self.view = None
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

    def prefetch(self, pos):
        first = pos // BLOCK
        # Queue the requested block before its neighbours.
        for i in range(first, min(first + self.ahead + 1, (self.size + BLOCK - 1) // BLOCK)):
            self.block(i)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        if self.view:
            self.view.close()
        self.pool.shutdown(wait=False, cancel_futures=True)

    def _handler(self):
        cache = self
        last = (cache.size - 1) // BLOCK

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _head(self):
                size = cache.view.size if cache.view else cache.size
                start, end, partial = 0, size - 1, False
                m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
                if m and (m[1] or m[2]):
                    partial = True
                    if m[1]:
                        start = int(m[1])
                        end = min(int(m[2]), end) if m[2] else end
                    else:
                        start = max(0, size - int(m[2]))
                if start >= size or end < start:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                self.send_response(206 if partial else 200)
                if partial:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
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
                        if cache.view:
                            chunk = cache.view.read(pos, min(BLOCK, end - pos + 1))
                            self.wfile.write(chunk)
                            pos += len(chunk)
                            continue
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


def prepare(src, disc_root=None):
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
    try:
        data = proxy.read(0, BLOCK)
        if not iso_signature(data):
            proxy.close()
            return src, None, None
        kind = disc_kind(data)
        udf = any(data[s * 2048 + 1:s * 2048 + 6] in (b"BEA01", b"NSR02", b"NSR03")
                  for s in range(16, 32))
        if kind == "bluray" and not udf:
            proxy.view = project_iso9660(proxy, root=disc_root)
        return proxy.mrl, kind, proxy
    except Exception:
        proxy.close()
        raise


def fmt(ms):
    s = max(ms, 0) // 1000
    return f"{s // 3600}:{s // 60 % 60:02}:{s % 60:02}" if s >= 3600 else f"{s // 60}:{s % 60:02}"


def ago(ts, now=None):
    m = int(((now or time.time()) - ts) // 60)
    if m < 1:
        return "Just now"
    if m < 60:
        return f"{m} min ago"
    if m < 24 * 60:
        return f"{m // 60} h ago"
    if m < 48 * 60:
        return "Yesterday"
    return time.strftime("%-d %b" if sys.platform != "win32" else "%d %b", time.localtime(ts))


def display_name(src):
    return urllib.parse.unquote(src.rstrip("/").rsplit("/", 1)[-1].split("?")[0]) or src


def origin(src):
    return (urllib.parse.urlparse(src).hostname or src) if is_url(src) else str(Path(src).parent)


KIND = {"bluray": "Blu-ray", "dvd": "DVD"}
MAC = sys.platform == "darwin"
MOD = "⌘" if MAC else "Ctrl+"

INK, INK2, INK3 = "#f5f5f7", "rgba(235,235,245,0.62)", "rgba(235,235,245,0.38)"
CSS = f"""
* {{ outline: none; }}
QMainWindow {{ background: #000; }}
#welcome {{ background: #161618; }}
#welcome[dropping="true"] {{ background: #1b2330; }}
#appname {{ color: {INK}; font-size: 20px; font-weight: 600; }}
#version, #muted {{ color: {INK3}; font-size: 12px; }}
#section {{ color: {INK2}; font-size: 12px; font-weight: 600; }}
#action {{ color: {INK}; background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.07);
    border-radius: 8px; padding: 0 14px; min-height: 38px; font-size: 13px; text-align: left; }}
#action:hover {{ background: rgba(255,255,255,0.10); }}
#action:pressed {{ background: rgba(255,255,255,0.14); }}
#link {{ color: {INK3}; background: transparent; border: none; font-size: 12px; padding: 0; }}
#link:hover {{ color: {INK}; }}
#row {{ background: transparent; border-radius: 7px; }}
#row:hover {{ background: rgba(255,255,255,0.06); }}
#rowtitle {{ color: {INK}; font-size: 13px; }}
#rowmeta {{ color: {INK3}; font-size: 12px; }}
#empty {{ color: {INK3}; font-size: 13px; }}
QCheckBox {{ color: {INK2}; font-size: 12px; spacing: 8px; margin-left: 10px; }}
QCheckBox::indicator {{ width: 14px; height: 14px; border-radius: 4px; border: 1px solid rgba(255,255,255,0.28);
    background: rgba(255,255,255,0.04); }}
QCheckBox::indicator:checked {{ background: #0a84ff; border-color: #0a84ff; image: url(CHECK); }}
#panel {{ background: rgba(28,28,30,0.90); border: 1px solid rgba(255,255,255,0.10); border-radius: 12px; }}
#panel QPushButton {{ background: transparent; border: none; border-radius: 6px; padding: 4px; }}
#panel QPushButton:hover {{ background: rgba(255,255,255,0.10); }}
#panel QPushButton:pressed {{ background: rgba(255,255,255,0.16); }}
#time {{ color: {INK2}; font-size: 11px; }}
#panel QPushButton#time {{ padding: 0 2px; }}
#panel QPushButton#time:hover {{ background: transparent; color: {INK}; }}
QSlider::groove:horizontal {{ height: 3px; background: rgba(255,255,255,0.20); border-radius: 1px; }}
QSlider::sub-page:horizontal {{ background: rgba(255,255,255,0.90); border-radius: 1px; }}
QSlider::handle:horizontal {{ width: 11px; height: 11px; margin: -4px 0; border-radius: 5px; background: #fff; }}
#osd {{ background: rgba(28,28,30,0.90); border: 1px solid rgba(255,255,255,0.10); border-radius: 8px; }}
#osd QLabel {{ color: {INK}; font-size: 13px; font-weight: 500; }}
QToolTip {{ color: {INK}; background: #2c2c2e; border: 1px solid rgba(255,255,255,0.12); padding: 3px 6px; }}
"""
MENU_CSS = f"""
QMenu {{ background: #2c2c2e; border: 1px solid rgba(255,255,255,0.12); border-radius: 8px; padding: 4px; }}
QMenu::item {{ color: {INK}; padding: 5px 22px 5px 22px; border-radius: 5px; font-size: 13px; }}
QMenu::item:selected {{ background: #0a84ff; }}
QMenu::item:disabled {{ color: {INK3}; }}
QMenu::separator {{ height: 1px; background: rgba(255,255,255,0.10); margin: 4px 6px; }}
QMenu::indicator {{ width: 12px; left: 6px; }}
"""

ICONS = {  # 24x24 path data (Material Symbols, Apache-2.0)
    "play": "M8 5.14v13.72a1 1 0 0 0 1.52.85l10.9-6.86a1 1 0 0 0 0-1.7L9.52 4.29A1 1 0 0 0 8 5.14z",
    "pause": "M7 5h3a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1zm7 0h3a1 1 0 0 1 1 1v12"
             "a1 1 0 0 1-1 1h-3a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1z",
    "prev": "M6 6h2v12H6zm3.5 6 8.5 6V6z",
    "next": "M6 18l8.5-6L6 6v12zM16 6v12h2V6h-2z",
    "disc": "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm0 14.5a4.5 4.5 0 1 1 0-9 4.5 4.5 0 0 1 0 9zm0-5.5"
            "a1 1 0 1 0 0 2 1 1 0 0 0 0-2z",
    "tracks": "M20 4H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2zM4 12h4v2H4zm10 6H4v-2h10z"
              "m6 0h-4v-2h4zm0-4H10v-2h10z",
    "volume": "M3 9v6h4l5 5V4L7 9H3zm13.5 3A4.5 4.5 0 0 0 14 7.97v8.05A4.48 4.48 0 0 0 16.5 12zM14 3.23v2.06"
              "a7 7 0 0 1 0 13.42v2.06A9 9 0 0 0 14 3.23z",
    "muted": "M16.5 12A4.5 4.5 0 0 0 14 7.97v2.21l2.45 2.45c.03-.2.05-.41.05-.63zM19 12c0 .94-.2 1.82-.54 2.64"
             "l1.51 1.51A8.8 8.8 0 0 0 21 12a9 9 0 0 0-7-8.77v2.06A7 7 0 0 1 19 12zM4.27 3 3 4.27 7.73 9H3v6h4"
             "l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.06a9 9 0 0 0 3.69-1.81L19.73 21 21 19.73l-9-9z"
             "M12 4 9.91 6.09 12 8.18V4z",
    "full": "M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z",
    "unfull": "M5 16h3v3h2v-5H5v2zm3-8H5v2h5V5H8v3zm6 11h2v-3h3v-2h-5v5zm2-11V5h-2v5h5V8h-3z",
    "folder": "M10 4H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-8l-2-2z",
    "link": "M3.9 12A3.1 3.1 0 0 1 7 8.9h4V7H7a5 5 0 0 0 0 10h4v-1.9H7A3.1 3.1 0 0 1 3.9 12zM8 13h8v-2H8v2z"
            "m9-6h-4v1.9h4a3.1 3.1 0 0 1 0 6.2h-4V17h4a5 5 0 0 0 0-10z",
    "check": "M9 16.17 4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z",
}


def svg_icon(svg, size=48, color=INK):
    """Render SVG markup or an ICONS key to a QIcon (QSvgRenderer, so no image-format plugin needed)."""
    if svg in ICONS:
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
               f'<path fill="{color}" d="{ICONS[svg]}"/></svg>')
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    QSvgRenderer(svg.encode() if isinstance(svg, str) else svg).render(p)
    p.end()
    return QIcon(QPixmap.fromImage(img))


def tabular(widget):
    f = widget.font()
    f.setFeature(QFont.Tag("tnum"), 1)
    widget.setFont(f)
    return widget


class Overlay(QWidget):
    """Floating child window: the video is a native libVLC surface, so Qt widgets can't paint over it."""

    def __init__(self, owner, clickable):
        flags = Qt.Tool | Qt.FramelessWindowHint | Qt.WindowDoesNotAcceptFocus | Qt.NoDropShadowWindowHint
        super().__init__(owner, flags if clickable else flags | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setStyleSheet(owner.styleSheet())
        self.anim = QPropertyAnimation(self, b"windowOpacity", self, duration=180,
                                       easingCurve=QEasingCurve.OutCubic)
        self.anim.finished.connect(lambda: self.windowOpacity() < 0.01 and self.hide())
        self.shown = False

    def fade(self, show):
        if show == self.shown:
            return
        self.shown = show
        self.anim.stop()
        if show:
            if not self.isVisible():
                self.setWindowOpacity(0.0)
                self.show()
            self.anim.setDuration(140)
        else:
            self.anim.setDuration(320)
        self.anim.setStartValue(self.windowOpacity())
        self.anim.setEndValue(1.0 if show else 0.0)
        self.anim.start()


class Spinner(QWidget):
    """Indeterminate ring: a 270 degree arc turning once a second."""

    def __init__(self, parent=None, size=14):
        super().__init__(parent, fixedSize=QSize(size, size))
        self.angle = 0
        self.spin = QTimer(self, interval=16, timeout=self.turn)

    def turn(self):
        self.angle = (self.angle + 6) % 360
        self.update()

    def showEvent(self, e):
        self.spin.start()

    def hideEvent(self, e):
        self.spin.stop()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect().adjusted(2, 2, -2, -2)
        for color, start, span in ((QColor(255, 255, 255, 46), 0, 360), (QColor(INK), -self.angle, 270)):
            pen = QPen(color, 2)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawArc(r, start * 16, span * 16)


class RecentRow(QFrame):
    def __init__(self, item, on_open):
        super().__init__(objectName="row", cursor=Qt.PointingHandCursor)
        self.src, self.on_open = item["src"], on_open
        lay = QGridLayout(self)
        lay.setContentsMargins(10, 7, 10, 7)
        lay.setHorizontalSpacing(12)
        lay.setVerticalSpacing(1)
        title = QLabel(objectName="rowtitle")
        title.setText(title.fontMetrics().elidedText(item["name"], Qt.ElideMiddle, 360))
        meta = " · ".join(x for x in (KIND.get(item.get("kind")), origin(item["src"])) if x)
        lay.addWidget(title, 0, 0)
        lay.addWidget(QLabel(meta, objectName="rowmeta"), 1, 0)
        lay.addWidget(QLabel(ago(item["ts"]), objectName="rowmeta"), 0, 1, 2, 1, Qt.AlignRight | Qt.AlignVCenter)
        lay.setColumnStretch(0, 1)
        self.setToolTip(item["src"])

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.on_open(self.src)


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
        self.resize(1120, 630)
        self.setMinimumSize(640, 400)
        self.setAcceptDrops(True)
        check = Path(tempfile.gettempdir()) / "isoplay-check.svg"
        check.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
                         f'<path fill="#fff" d="{ICONS["check"]}"/></svg>')
        self.setStyleSheet(CSS.replace("CHECK", check.as_posix()) + ("" if MAC else MENU_CSS))
        self.settings = QSettings("isoplay", "isoplay")
        self.vlc = vlc.Instance(["--no-video-title-show", "--network-caching=3000"]
                                + (["-vv"] if os.environ.get("ISOPLAY_DEBUG") else []))
        self.mp = self.vlc.media_player_new()
        self.mp.video_set_key_input(False)
        self.mp.video_set_mouse_input(True)
        self.disc = self.proxy = self.current = None
        self.disc_root = None
        self.token = 0
        self.use_menus = self.remaining = True
        self.last_pos, self.last_move = QPoint(), 0.0
        self.cursor_hidden = False

        self.bridge = Bridge()
        self.bridge.prepared.connect(self.start)
        self.bridge.event.connect(self.on_event)
        ev = self.mp.event_manager()
        for t, name in ((vlc.EventType.MediaPlayerEncounteredError, "error"),
                        (vlc.EventType.MediaPlayerVout, "vout"),
                        (vlc.EventType.MediaPlayerEndReached, "end")):
            ev.event_attach(t, lambda e, n=name: self.bridge.event.emit(n))
        ev.event_attach(vlc.EventType.MediaPlayerBuffering,
                        lambda e: self.bridge.event.emit(f"buffer:{e.u.new_cache:.0f}"))

        self.build_actions()
        self.stack = QStackedWidget()
        self.welcome = self.build_welcome()
        self.stack.addWidget(self.welcome)
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
        bubble = QFrame(self.osd, objectName="osd")
        row = QHBoxLayout(bubble)
        row.setContentsMargins(11, 6, 12, 6)
        row.setSpacing(8)
        self.spinner = Spinner()
        self.osd_label = QLabel(styleSheet="background: transparent; border: none;")
        row.addWidget(self.spinner)
        row.addWidget(self.osd_label)
        QVBoxLayout(self.osd).setContentsMargins(0, 0, 0, 0)
        self.osd.layout().addWidget(bubble)
        self.osd_until = 0.0
        self.buffering = False

        self.timer = QTimer(self, interval=100, timeout=self.tick)
        self.timer.start()

    # --- structure ---------------------------------------------------------------------------------

    def action(self, text, key, slot, checkable=False):
        a = QAction(text, self, checkable=checkable)
        if key:
            a.setShortcut(QKeySequence(key))
        a.triggered.connect(slot)
        self.addAction(a)
        return a

    def build_actions(self):
        self.act_open = self.action("Open File…", QKeySequence.Open, self.browse)
        self.act_url = self.action("Open Location…", "Ctrl+L", self.ask_url)
        self.act_paste = self.action("Play Copied Link", QKeySequence.Paste, self.paste)
        self.act_close = self.action("Close Video", QKeySequence.Close, self.close_video)
        self.act_pause = self.action("Play/Pause", None, self.toggle_pause)
        self.act_discmenu = self.action("Disc Menu", None, lambda: self.mp.navigate(vlc.NavigateMode.popup))
        self.act_menus = self.action("Start Discs at Their Menu", None, self.menus_changed, checkable=True)
        self.act_menus.setChecked(self.settings.value("menus", True, type=bool))
        self.act_main = self.action("Play Main Title Without Menus", None, self.replay_main)
        self.act_prev = self.action("Previous Chapter", None, lambda: self.step(-1))
        self.act_next = self.action("Next Chapter", None, lambda: self.step(1))
        self.act_full = self.action("Enter Full Screen", "Ctrl+Meta+F" if MAC else "F11", self.toggle_full)
        if MAC:
            bar = self.menuBar()
            for title, acts in (("File", (self.act_open, self.act_url, self.act_paste, None, self.act_close)),
                                ("Playback", (self.act_pause, None, self.act_prev, self.act_next, None,
                                              self.act_discmenu, self.act_main, None, self.act_menus)),
                                ("Video", (self.act_full,))):
                m = bar.addMenu(title)
                for a in acts:
                    m.addAction(a) if a else m.addSeparator()

    def build_welcome(self):
        page = QWidget(objectName="welcome")
        outer = QVBoxLayout(page)
        outer.setContentsMargins(32, 40, 32, 28)
        col = QVBoxLayout()
        col.setSpacing(0)
        holder = QHBoxLayout()
        holder.addStretch()
        box = QWidget(fixedWidth=500)
        box.setLayout(col)
        holder.addWidget(box)
        holder.addStretch()
        outer.addStretch(2)
        outer.addLayout(holder)
        outer.addStretch(3)

        head = QHBoxLayout()
        head.setSpacing(14)
        logo = QLabel()
        logo.setPixmap(svg_icon((BASE / "assets/icon.svg").read_bytes(), 112).pixmap(56, 56))
        names = QVBoxLayout()
        names.setSpacing(2)
        names.addStretch()
        names.addWidget(QLabel("isoplay", objectName="appname"))
        names.addWidget(QLabel(f"Version {__version__}", objectName="version"))
        names.addStretch()
        head.addWidget(logo)
        head.addLayout(names)
        head.addStretch()
        col.addLayout(head)
        col.addSpacing(28)

        acts = QHBoxLayout()
        acts.setSpacing(10)
        for icon, text, key, slot in (("folder", "Open File…", f"{MOD}O", self.browse),
                                      ("link", "Open Location…", f"{MOD}L", self.ask_url)):
            b = QPushButton(objectName="action", cursor=Qt.PointingHandCursor, icon=svg_icon(icon, 36, "#9e9ea6"),
                            iconSize=QSize(16, 16), clicked=slot)
            row = QHBoxLayout(b)
            row.setContentsMargins(38, 0, 12, 0)
            row.addWidget(QLabel(text, styleSheet=f"color:{INK}; font-size:13px; background:transparent;"))
            row.addStretch()
            row.addWidget(QLabel(key, objectName="muted", styleSheet="background:transparent;"))
            acts.addWidget(b)
        col.addLayout(acts)
        col.addSpacing(30)

        top = QHBoxLayout()
        top.setContentsMargins(10, 0, 10, 0)  # line up with the row text, whose hover fill is inset
        top.addWidget(QLabel("Recent", objectName="section"))
        top.addStretch()
        self.clear_btn = QPushButton("Clear", objectName="link", cursor=Qt.PointingHandCursor,
                                     clicked=self.clear_recent)
        top.addWidget(self.clear_btn)
        col.addLayout(top)
        col.addSpacing(6)
        self.recent_box = QVBoxLayout()
        self.recent_box.setSpacing(0)
        col.addLayout(self.recent_box)
        col.addSpacing(22)
        menus = QCheckBox("Start discs at their menu", cursor=Qt.PointingHandCursor)
        menus.setChecked(self.act_menus.isChecked())
        menus.setContentsMargins(10, 0, 0, 0)
        menus.toggled.connect(self.act_menus.setChecked)
        menus.toggled.connect(self.menus_changed)
        self.act_menus.toggled.connect(menus.setChecked)
        col.addWidget(menus)
        self.refresh_recent()
        return page

    def build_controls(self):
        panel = QFrame(self.controls, objectName="panel")
        lay = QVBoxLayout(self.controls)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(panel)
        v = QVBoxLayout(panel)
        v.setContentsMargins(12, 6, 12, 9)
        v.setSpacing(2)

        def btn(icon, tip, slot, size=18):
            b = QPushButton(toolTip=tip, focusPolicy=Qt.NoFocus, cursor=Qt.PointingHandCursor,
                            icon=svg_icon(icon), iconSize=QSize(size, size))
            if slot:
                b.clicked.connect(slot)
            return b

        top = QGridLayout()
        top.setContentsMargins(0, 0, 0, 0)
        left = QHBoxLayout()
        left.setSpacing(4)
        self.mute_btn = btn("volume", "Mute", self.toggle_mute, 17)
        self.vol = QSlider(Qt.Horizontal, maximum=100, value=100, focusPolicy=Qt.NoFocus, fixedWidth=72)
        self.vol.valueChanged.connect(self.mp.audio_set_volume)
        left.addWidget(self.mute_btn)
        left.addWidget(self.vol)
        left.addStretch()
        mid = QHBoxLayout()
        mid.setSpacing(10)
        self.play_btn = btn("pause", "Play/Pause (Space)", self.toggle_pause, 26)
        self.play_icons = {True: svg_icon("pause"), False: svg_icon("play")}
        mid.addWidget(btn("prev", "Previous chapter", lambda: self.step(-1)))
        mid.addWidget(self.play_btn)
        mid.addWidget(btn("next", "Next chapter", lambda: self.step(1)))
        right = QHBoxLayout()
        right.setSpacing(2)
        right.addStretch()
        self.menu_btn = btn("disc", "Disc menu (M)", lambda: self.mp.navigate(vlc.NavigateMode.popup))
        self.tracks_btn = btn("tracks", "Audio, subtitles and disc options", self.tracks_menu)
        self.full_btn = btn("full", "Full screen (F)", self.toggle_full)
        for b in (self.menu_btn, self.tracks_btn, self.full_btn):
            right.addWidget(b)
        for i, part in enumerate((left, mid, right)):
            top.addLayout(part, 0, i, Qt.AlignVCenter | (Qt.AlignHCenter if i == 1 else Qt.AlignLeft))
            top.setColumnStretch(i, 1)
        v.addLayout(top)

        bottom = QHBoxLayout()
        bottom.setSpacing(10)
        self.pos_lbl = tabular(QLabel("0:00", objectName="time", minimumWidth=44,
                                      alignment=Qt.AlignRight | Qt.AlignVCenter))
        self.seek = QSlider(Qt.Horizontal, maximum=10000, focusPolicy=Qt.NoFocus)
        self.seek.sliderReleased.connect(lambda: self.mp.set_position(self.seek.value() / 10000))
        self.seek.sliderMoved.connect(lambda v: self.pos_lbl.setText(fmt(int(self.mp.get_length() * v / 10000))))
        self.len_btn = tabular(QPushButton("0:00", objectName="time", focusPolicy=Qt.NoFocus, minimumWidth=50,
                                           cursor=Qt.PointingHandCursor, toolTip="Show remaining or total time"))
        self.len_btn.clicked.connect(lambda: setattr(self, "remaining", not self.remaining))
        bottom.addWidget(self.pos_lbl)
        bottom.addWidget(self.seek, 1)
        bottom.addWidget(self.len_btn)
        v.addLayout(bottom)

    # --- recent ------------------------------------------------------------------------------------

    def recent(self):
        try:
            return json.loads(self.settings.value("recent", "[]"))
        except ValueError:
            return []

    def remember(self, src, kind):
        items = [i for i in self.recent() if i["src"] != src]
        items.insert(0, {"src": src, "name": display_name(src), "kind": kind, "ts": time.time()})
        self.settings.setValue("recent", json.dumps(items[:8]))

    def clear_recent(self):
        self.settings.setValue("recent", "[]")
        self.refresh_recent()

    def refresh_recent(self):
        while self.recent_box.count():
            w = self.recent_box.takeAt(0).widget()
            if w:
                w.deleteLater()
        items = self.recent()
        for item in items:
            self.recent_box.addWidget(RecentRow(item, self.play))
        if not items:
            empty = QLabel(f"Nothing played yet. Drop a disc image or video anywhere in this window, "
                           f"or copy a link and press {MOD}V.", objectName="empty", wordWrap=True)
            empty.setContentsMargins(10, 6, 10, 6)
            self.recent_box.addWidget(empty)
        self.clear_btn.setVisible(bool(items))

    # --- opening -----------------------------------------------------------------------------------

    def browse(self):
        f, _ = QFileDialog.getOpenFileName(
            self, "Open File", self.settings.value("dir", ""),
            "Disc images and video (*.iso *.img *.mkv *.mp4 *.m4v *.m2ts *.ts *.avi *.mov *.webm *.vob);;"
            "All files (*)")
        if f:
            self.settings.setValue("dir", str(Path(f).parent))
            self.play(f)

    def ask_url(self):
        clip = QApplication.clipboard().text().strip()
        url, ok = QInputDialog.getText(self, "Open Location", "Link to a disc image or video file:",
                                       text=clip if is_url(clip) else "")
        if ok and url.strip():
            self.play(url.strip())

    def paste(self):
        text = QApplication.clipboard().text().strip()
        if is_url(text) or (text and Path(text).exists()):
            self.play(text)
        else:
            self.flash("The clipboard has no link or file path")

    def menus_changed(self):
        self.settings.setValue("menus", self.act_menus.isChecked())

    def replay_main(self):
        if self.current:
            self.play(self.current, menus=False, disc_root=self.disc_root)

    def switch_disc(self, root):
        if self.current and root != self.disc_root:
            self.play(self.current, menus=self.use_menus, disc_root=root)

    def close_video(self):
        self.token += 1
        self.mp.stop()
        if self.proxy:
            self.proxy.close()
            self.proxy = None
        self.current = self.disc = None
        self.disc_root = None
        self.setWindowTitle("isoplay")
        self.refresh_recent()
        self.stack.setCurrentWidget(self.welcome)
        self.osd_until = 0

    def play(self, src, menus=None, disc_root=None):
        if not src:
            return
        self.close_video()
        self.current = src
        self.use_menus = self.act_menus.isChecked() if menus is None else menus
        self.setWindowTitle(display_name(src))
        self.stack.setCurrentWidget(self.video)
        self.flash("Opening", sticky=True, busy=True)
        token = self.token

        def work():
            try:
                result = prepare(src, disc_root=disc_root)
            except Exception as e:  # noqa: BLE001 - shown to the user
                result = e
            self.bridge.prepared.emit(token, result)

        threading.Thread(target=work, daemon=True).start()

    def start(self, token, result):
        if token != self.token:  # a newer play() superseded this one
            if isinstance(result, tuple) and result[2]:
                result[2].close()
            return
        if isinstance(result, Exception):
            self.flash(f"Couldn't open this. {result}", seconds=10)
            return
        mrl, self.disc, self.proxy = result
        self.disc_root = self.proxy.view.disc_root if self.proxy and self.proxy.view else None
        self.remember(self.current, self.disc)
        media = self.vlc.media_new(mrl) if "://" in mrl else self.vlc.media_new_path(mrl)
        media.add_option(f":http-user-agent={UA}")
        if self.disc == "dvd" and self.use_menus and not mrl.startswith("dvd:"):
            media.add_option(":demux=dvdnav")  # auto-probe picks raw PS over HTTP and loses menus
        if self.disc == "bluray" and not self.use_menus:
            media.add_option(":no-bluray-menu")
        self.mp.set_media(media)
        self.mp.play()
        kind = KIND.get(self.disc)
        self.flash(f"{kind}: loading {'disc menu' if self.use_menus else 'main title'}" if kind else "Loading",
                   sticky=True, busy=True)
        self.menu_btn.setVisible(bool(self.disc) and self.use_menus)
        self.act_discmenu.setEnabled(bool(self.disc) and self.use_menus)

    def on_event(self, name):
        if name.startswith("buffer:"):
            pct = int(name[7:])
            started = self.mp.get_time() > 0
            if pct < 100 and started and (self.buffering or self.osd_until < time.monotonic()):
                self.flash(f"Buffering {pct}%", sticky=True, busy=True)
                self.buffering = True
            elif pct >= 100 and self.buffering:
                self.flash("")
        elif name == "vout":
            self.flash("")
        elif name == "end" and not self.disc:
            self.flash("Finished", seconds=3)
        elif name == "error":
            if self.disc and self.use_menus:
                self.flash("The disc menu didn't load. Choose Play Main Title Without Menus from the track button.", seconds=12)
            else:
                self.flash("Couldn't play this. The link may have expired or the format isn't supported.",
                           seconds=12)

    # --- transport ---------------------------------------------------------------------------------

    def toggle_pause(self):
        if self.mp.get_media():
            self.mp.pause()
            QTimer.singleShot(120, lambda: self.flash("Paused" if not self.mp.is_playing() else "Playing", 1.2))

    def toggle_mute(self):
        self.mp.audio_toggle_mute()
        muted = bool(self.mp.audio_get_mute())
        self.mute_btn.setIcon(svg_icon("muted" if muted else "volume"))
        self.flash("Muted" if muted else f"Volume {self.vol.value()}%", 1.2)

    def nudge_volume(self, d):
        self.vol.setValue(self.vol.value() + d)
        self.flash(f"Volume {self.vol.value()}%", 1.2)

    def seek_by(self, ms):
        self.mp.set_time(max(0, self.mp.get_time() + ms))
        self.flash(f"{fmt(self.mp.get_time() + ms)} / {fmt(self.mp.get_length())}", 1.2)

    def step(self, d):
        if self.mp.get_chapter_count() > 1:
            self.mp.next_chapter() if d > 0 else self.mp.previous_chapter()
            QTimer.singleShot(150, lambda: self.flash(f"Chapter {self.mp.get_chapter() + 1}", 1.2))
        else:
            self.seek_by(d * 10000)

    def tracks_menu(self):
        menu = QMenu(self)
        for title, describe, current, select in (
                ("Audio", self.mp.audio_get_track_description, self.mp.audio_get_track, self.mp.audio_set_track),
                ("Subtitles", self.mp.video_get_spu_description, self.mp.video_get_spu, self.mp.video_set_spu)):
            if not menu.isEmpty():
                menu.addSeparator()
            menu.addAction(title).setEnabled(False)
            cur = current()
            tracks = describe() or []
            for tid, name in tracks:
                label = name.decode(errors="replace") if isinstance(name, bytes) else str(name)
                if tid == -1:
                    label = "Off"
                a = menu.addAction(label)
                a.setCheckable(True)
                a.setChecked(tid == cur)
                a.triggered.connect(lambda _=False, t=tid, s=select, n=title, lb=label: (s(t), self.flash(f"{n}: {lb}", 1.5)))
            if not tracks:
                menu.addAction("None").setEnabled(False)
        if self.disc:
            menu.addSeparator()
            menu.addAction("Disc").setEnabled(False)
            self.add_disc_choices(menu)
            menu.addAction(self.act_discmenu)
            menu.addAction(self.act_main)
            menu.addAction(self.act_menus)
        menu.exec(self.tracks_btn.mapToGlobal(QPoint(0, -menu.sizeHint().height() - 6)))

    def add_disc_choices(self, menu):
        view = self.proxy.view if self.proxy else None
        if not view or len(view.discs) < 2:
            return
        choices = menu.addMenu("Discs in This Image")
        for root in view.discs:
            label = root.strip("/").replace("_", " ").title() or "Disc"
            action = choices.addAction(label)
            action.setCheckable(True)
            action.setChecked(root == self.disc_root)
            action.triggered.connect(lambda _=False, selected=root: self.switch_disc(selected))

    # --- overlay -----------------------------------------------------------------------------------

    def flash(self, text, seconds=3.0, sticky=False, busy=False):
        self.buffering = False
        if not text:
            self.osd_until = 0
            return
        self.spinner.setVisible(busy)
        self.osd_label.setText(text)
        self.osd.adjustSize()
        self.osd_until = float("inf") if sticky else time.monotonic() + seconds
        self.place()

    def place(self):
        area = QRect(self.stack.mapToGlobal(QPoint(0, 0)), self.stack.size())
        w = min(520, area.width() - 40)
        self.controls.setGeometry(area.center().x() - w // 2, area.bottom() - 22 - 84, w, 84)
        self.osd.move(area.left() + 18, area.top() + 18)

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
        hovering = self.controls.isVisible() and self.controls.geometry().contains(pos)
        show = active and on_video and (not playing or now - self.last_move < 2.2 or hovering)
        self.controls.fade(show)
        self.osd.fade(active and on_video and now < self.osd_until)
        hide_cursor = on_video and active and playing and not show and self.frameGeometry().contains(pos)
        if hide_cursor != self.cursor_hidden:
            self.cursor_hidden = hide_cursor
            QApplication.setOverrideCursor(Qt.BlankCursor) if hide_cursor else QApplication.restoreOverrideCursor()
        if self.controls.isVisible():
            icon = self.play_icons[bool(playing)]
            if self.play_btn.icon().cacheKey() != icon.cacheKey():
                self.play_btn.setIcon(icon)
            length, t = self.mp.get_length(), self.mp.get_time()
            if not self.seek.isSliderDown():
                self.seek.setValue(int(self.mp.get_position() * 10000))
                self.pos_lbl.setText(fmt(t))
            self.len_btn.setText(f"-{fmt(length - t)}" if self.remaining and length > 0 else fmt(length))

    def moveEvent(self, e):
        self.place()
        super().moveEvent(e)

    def resizeEvent(self, e):
        self.place()
        super().resizeEvent(e)

    def changeEvent(self, e):
        if e.type() == QEvent.WindowStateChange:
            full = self.isFullScreen()
            self.full_btn.setIcon(svg_icon("unfull" if full else "full"))
            self.act_full.setText("Exit Full Screen" if full else "Enter Full Screen")
            QTimer.singleShot(250, self.place)
        super().changeEvent(e)

    def toggle_full(self):
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    # --- input -------------------------------------------------------------------------------------

    def keyPressEvent(self, e):
        k, shift = e.key(), e.modifiers() & Qt.ShiftModifier
        disc_nav = self.disc and self.use_menus
        if k in (Qt.Key_Left, Qt.Key_Right) and (shift or not disc_nav):
            self.seek_by(10000 if k == Qt.Key_Right else -10000)
        elif k in (Qt.Key_Up, Qt.Key_Down) and not disc_nav:
            self.nudge_volume(5 if k == Qt.Key_Up else -5)
        elif k in self.NAV and disc_nav:
            self.mp.navigate(self.NAV[k])
        elif k == Qt.Key_Space:
            self.toggle_pause()
        elif k == Qt.Key_M:
            self.mp.navigate(vlc.NavigateMode.popup)
        elif k == Qt.Key_F:
            self.toggle_full()
        elif k in (Qt.Key_PageUp, Qt.Key_PageDown):
            self.step(1 if k == Qt.Key_PageDown else -1)
        elif k == Qt.Key_Escape and self.isFullScreen():
            self.toggle_full()
        else:
            super().keyPressEvent(e)
        self.last_move = time.monotonic()

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() or e.mimeData().hasText():
            e.acceptProposedAction()
            self.set_dropping(True)

    def dragLeaveEvent(self, e):
        self.set_dropping(False)

    def set_dropping(self, on):
        self.welcome.setProperty("dropping", on)
        self.welcome.style().unpolish(self.welcome)
        self.welcome.style().polish(self.welcome)

    def dropEvent(self, e):
        self.set_dropping(False)
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
