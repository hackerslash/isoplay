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

import vlc  # noqa: E402
from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QCheckBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QPushButton, QSlider, QVBoxLayout, QWidget,
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


def disc_kind(data):
    if b"BDMV" in data:
        return "bluray"
    if b"VIDEO_TS" in data:
        return "dvd"
    return None


def is_iso(src):
    path = urllib.parse.urlparse(src).path if is_url(src) else src
    return urllib.parse.unquote(path).lower().endswith(".iso")


def fmt(ms):
    s = max(ms, 0) // 1000
    return f"{s // 3600}:{s // 60 % 60:02}:{s % 60:02}"


class Player(QMainWindow):
    NAV = {Qt.Key_Up: vlc.NavigateMode.up, Qt.Key_Down: vlc.NavigateMode.down,
           Qt.Key_Left: vlc.NavigateMode.left, Qt.Key_Right: vlc.NavigateMode.right,
           Qt.Key_Return: vlc.NavigateMode.activate, Qt.Key_Enter: vlc.NavigateMode.activate}

    def __init__(self):
        super().__init__()
        self.setWindowTitle("isoplay")
        self.resize(1100, 680)
        self.setAcceptDrops(True)
        self.vlc = vlc.Instance(["--no-video-title-show", "--network-caching=3000"]
                                + (["-vv"] if os.environ.get("ISOPLAY_DEBUG") else []))
        self.mp = self.vlc.media_player_new()
        self.mp.video_set_key_input(False)
        self.mp.video_set_mouse_input(True)
        self.disc = None
        self.proxy = None

        self.src = QLineEdit(placeholderText="File path or URL — or drop a file here")
        self.src.returnPressed.connect(lambda: self.play(self.src.text().strip()))
        browse = QPushButton("Open…", clicked=self.browse)
        go = QPushButton("Play", clicked=lambda: self.play(self.src.text().strip()))
        self.menus = QCheckBox("Disc menus", checked=True,
                               toolTip="Off: skip menus and start the main title (for menus that fail)")
        self.top = QWidget()
        t = QHBoxLayout(self.top)
        t.setContentsMargins(6, 6, 6, 0)
        for w in (self.src, browse, go, self.menus):
            t.addWidget(w)

        self.video = QFrame()
        self.video.setStyleSheet("background:black")
        self.video.setAttribute(Qt.WA_NativeWindow)
        self.video.setFocusPolicy(Qt.StrongFocus)

        self.pause_btn = QPushButton("⏯", clicked=self.mp.pause)
        self.seek = QSlider(Qt.Horizontal, maximum=1000)
        self.seek.sliderMoved.connect(lambda v: self.mp.set_position(v / 1000))
        self.time = QLabel("0:00:00 / 0:00:00")
        self.menu_btn = QPushButton("Menu", clicked=lambda: self.mp.navigate(vlc.NavigateMode.popup))
        self.full_btn = QPushButton("⛶", clicked=self.toggle_full)
        self.bar = QWidget()
        b = QHBoxLayout(self.bar)
        b.setContentsMargins(6, 0, 6, 6)
        for w in (self.pause_btn, self.seek, self.time, self.menu_btn, self.full_btn):
            b.addWidget(w)

        root = QWidget()
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)
        for w in (self.top, self.video, self.bar):
            v.addWidget(w)
        v.setStretch(1, 1)
        self.setCentralWidget(root)

        wid = int(self.video.winId())
        if sys.platform == "darwin":
            self.mp.set_nsobject(wid)
        elif sys.platform == "win32":
            self.mp.set_hwnd(wid)
        else:
            self.mp.set_xwindow(wid)

        ev = self.mp.event_manager()
        ev.event_attach(vlc.EventType.MediaPlayerEncounteredError,
                        lambda e: QTimer.singleShot(0, lambda: self.status("Playback error — see terminal log")))
        self.timer = QTimer(self, interval=500, timeout=self.tick)
        self.timer.start()

    def status(self, msg):
        self.statusBar().showMessage(msg)

    def browse(self):
        f, _ = QFileDialog.getOpenFileName(self, "Open", "", "Discs and media (*.iso *.mkv *.mp4 *.m2ts *.ts *.avi *.mov *.webm);;All files (*)")
        if f:
            self.play(f)

    def play(self, src):
        if not src:
            return
        self.src.setText(src)
        self.mp.stop()
        if self.proxy:
            self.proxy.close()
            self.proxy = None
        self.disc = None
        mrl = src
        if is_iso(src):
            self.status("Reading disc …")
            QApplication.processEvents()
            try:
                if is_url(src):
                    self.proxy = RangeCache(src)
                    mrl = self.proxy.mrl
                    data = self.proxy.read(0, 1 << 20)
                else:
                    with open(src, "rb") as f:
                        data = f.read(1 << 20)
                self.disc = disc_kind(data)
            except Exception as e:
                self.status(f"Cannot read: {e}")
                return
        media = self.vlc.media_new(mrl) if is_url(mrl) else self.vlc.media_new_path(mrl)
        media.add_option(f":http-user-agent={UA}")
        if self.disc == "dvd" and self.menus.isChecked():
            media.add_option(":demux=dvdnav")  # auto-probe picks raw PS over HTTP and loses menus
        if self.disc == "bluray" and not self.menus.isChecked():
            media.add_option(":no-bluray-menu")
        self.mp.set_media(media)
        self.mp.play()
        menus = self.menus.isChecked()
        self.status({"bluray": "Blu-ray — " + ("menus on (arrows/Enter, M = popup menu)" if menus else "main title"),
                     "dvd": "DVD — " + ("menus on (arrows/Enter, M = menu)" if menus else "no menus")}
                    .get(self.disc, "Playing"))
        self.setWindowTitle(f"isoplay — {urllib.parse.unquote(src.rsplit('/', 1)[-1])}")
        self.video.setFocus()

    def tick(self):
        length = self.mp.get_length()
        if not self.seek.isSliderDown():
            self.seek.setValue(int(self.mp.get_position() * 1000))
        self.time.setText(f"{fmt(self.mp.get_time())} / {fmt(length)}")

    def toggle_full(self):
        full = not self.isFullScreen()
        self.top.setVisible(not full)
        self.bar.setVisible(not full)
        self.statusBar().setVisible(not full)
        self.showFullScreen() if full else self.showNormal()
        self.video.setFocus()

    def keyPressEvent(self, e):
        k, shift = e.key(), e.modifiers() & Qt.ShiftModifier
        if k in (Qt.Key_Left, Qt.Key_Right) and (shift or not self.disc):
            self.mp.set_time(max(0, self.mp.get_time() + (10000 if k == Qt.Key_Right else -10000)))
        elif k in self.NAV:
            self.mp.navigate(self.NAV[k])
        elif k == Qt.Key_Space:
            self.mp.pause()
        elif k == Qt.Key_M:
            self.mp.navigate(vlc.NavigateMode.popup)
        elif k == Qt.Key_F:
            self.toggle_full()
        elif k == Qt.Key_Escape and self.isFullScreen():
            self.toggle_full()
        else:
            super().keyPressEvent(e)

    def mouseDoubleClickEvent(self, e):
        self.toggle_full()

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
    w = Player()
    w.show()
    if len(sys.argv) > 1:
        w.play(sys.argv[1])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
