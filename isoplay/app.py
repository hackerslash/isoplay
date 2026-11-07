"""The player window."""
import json
import os
import sys
import threading
import time
from pathlib import Path

from isoplay import runtime  # noqa: F401 - sets the VLC environment before vlc loads

import vlc  # noqa: E402
from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, QSettings, Qt, QTimer, Signal  # noqa: E402
from PySide6.QtGui import QAction, QCursor, QKeySequence  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QMainWindow,
    QMenu, QPushButton, QScrollArea, QSizePolicy, QSlider, QStackedWidget, QToolButton, QVBoxLayout, QWidget,
)

from isoplay import updates  # noqa: E402
from isoplay import __version__  # noqa: E402
from isoplay.media import UA, display_name, fmt, is_url, prepare  # noqa: E402
from isoplay.runtime import BASE, VLC_DIR  # noqa: E402
from isoplay.theme import CSS, INK, INK2, MAC, MENU_CSS, MOD, svg_icon, tabular  # noqa: E402
from isoplay.update_ui import UpdateUI  # noqa: E402
from isoplay.widgets import (  # noqa: E402
    DiscChooser, ElidedLabel, OptionsMenu, Overlay, RecentRow, Spinner, disc_label,
)

KIND = {"bluray": "Blu-ray", "dvd": "DVD"}


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
        self.setStyleSheet(CSS + ("" if MAC else MENU_CSS))
        self.settings = QSettings("isoplay", "isoplay")
        self.vlc = vlc.Instance(["--no-video-title-show", "--network-caching=3000"]
                                + (["-vv"] if os.environ.get("ISOPLAY_DEBUG") else []))
        self.mp = self.vlc.media_player_new()
        self.mp.video_set_key_input(False)
        self.mp.video_set_mouse_input(True)
        self.disc = self.proxy = self.current = None
        self.disc_root = None
        self.ask_disc = False
        self.pending_disc = None
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

        self.updater = UpdateUI(self, __version__, self.settings, lambda: [self.current] if self.current else [])
        self.build_actions()
        self.stack = QStackedWidget()
        self.welcome = self.build_welcome()
        self.stack.addWidget(self.welcome)
        self.video = QFrame()
        self.video.setStyleSheet("background:#000")
        self.video.setAttribute(Qt.WA_NativeWindow)
        self.stack.addWidget(self.video)
        self.chooser = DiscChooser()
        self.chooser.selected.connect(self.choose_disc)
        self.chooser.cancelled.connect(self.close_video)
        self.stack.addWidget(self.chooser)
        self.setCentralWidget(self.stack)
        wid = int(self.video.winId())
        {"darwin": self.mp.set_nsobject, "win32": self.mp.set_hwnd}.get(sys.platform, self.mp.set_xwindow)(wid)

        self.controls = Overlay(self, clickable=True)
        self.build_controls()
        self.back = self.build_back()
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
        QTimer.singleShot(3000, self.updater.automatic_check)

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
        self.act_updates = self.action("Check for Updates…", None, lambda: self.updater.check(manual=True))
        self.act_auto_updates = self.action("Check for Updates Automatically", None,
                                           lambda: self.settings.setValue("updates/automatic", self.act_auto_updates.isChecked()),
                                           checkable=True)
        self.act_auto_updates.setChecked(self.settings.value("updates/automatic", True, type=bool))
        self.updater.busy_changed.connect(lambda busy: self.act_updates.setEnabled(not busy))
        if MAC:
            bar = self.menuBar()
            for title, acts in (("File", (self.act_open, self.act_url, self.act_paste, None, self.act_close)),
                                ("Playback", (self.act_pause, None, self.act_prev, self.act_next, None,
                                              self.act_discmenu, self.act_main, None, self.act_menus)),
                                ("Video", (self.act_full,)),
                                ("Help", (self.act_updates, self.act_auto_updates))):
                m = bar.addMenu(title)
                for a in acts:
                    m.addAction(a) if a else m.addSeparator()

    def build_welcome(self):
        page = QWidget(objectName="welcome")
        outer = QHBoxLayout(page)
        outer.setContentsMargins(32, 28, 32, 24)
        col = QVBoxLayout()
        col.setSpacing(0)
        box = QWidget()
        box.setMaximumWidth(640)
        box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        box.setLayout(col)
        outer.addStretch()
        outer.addWidget(box, 1)
        outer.addStretch()

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
        options = QToolButton(objectName="link", text="Options", cursor=Qt.PointingHandCursor)
        options.setIcon(svg_icon("more", 28, INK2))
        options.setIconSize(QSize(14, 14))
        options.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        options.setToolTip("Playback and update preferences")
        options.setPopupMode(QToolButton.InstantPopup)
        preferences = OptionsMenu(options, self.act_menus, self.act_auto_updates, self.act_updates)
        options.setMenu(preferences)
        head.addWidget(options)
        col.addLayout(head)
        col.addSpacing(22)

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
        col.addSpacing(24)

        top = QHBoxLayout()
        top.setContentsMargins(10, 0, 10, 0)  # line up with the row text, whose hover fill is inset
        top.addWidget(QLabel("Recent", objectName="section"))
        top.addStretch()
        self.clear_btn = QPushButton("Clear", objectName="link", cursor=Qt.PointingHandCursor,
                                     clicked=self.clear_recent)
        top.addWidget(self.clear_btn)
        col.addLayout(top)
        col.addSpacing(6)
        scroll = QScrollArea(objectName="recentScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setMinimumHeight(80)
        scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        content = QWidget(objectName="recentContent")
        self.recent_box = QVBoxLayout(content)
        self.recent_box.setContentsMargins(0, 0, 8, 0)
        self.recent_box.setSpacing(0)
        self.recent_box.setAlignment(Qt.AlignTop)
        scroll.setWidget(content)
        col.addWidget(scroll, 1)
        self.refresh_recent()
        return page

    def build_back(self):
        """Leave the video and return to the start screen, for people who never find Close Video."""
        overlay = Overlay(self, clickable=True)
        button = QPushButton(objectName="back", toolTip=f"Back to the start screen ({MOD}W)",
                             focusPolicy=Qt.NoFocus, cursor=Qt.PointingHandCursor,
                             icon=svg_icon("back", 40), iconSize=QSize(20, 20), clicked=self.close_video)
        button.setAccessibleName("Back to the start screen")
        button.setFixedSize(40, 40)
        layout = QVBoxLayout(overlay)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(button)
        return overlay

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
            items = json.loads(self.settings.value("recent", "[]"))
        except ValueError:
            return []
        return [i for i in items if isinstance(i, dict) and isinstance(i.get("src"), str)
                and isinstance(i.get("name"), str) and isinstance(i.get("ts"), (int, float))]

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
        self.ask_disc = False
        self.pending_disc = None
        self.setWindowTitle("isoplay")
        self.refresh_recent()
        self.stack.setCurrentWidget(self.welcome)
        self.osd_until = 0

    def play(self, src, menus=None, disc_root=None):
        if not src:
            return
        self.close_video()
        self.current = src
        self.ask_disc = disc_root is None
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
        if self.ask_disc and self.proxy and self.proxy.view and len(self.proxy.view.discs) > 1:
            self.ask_disc = False
            self.pending_disc = (token, result)
            self.chooser.set_discs(self.proxy.view.discs)
            self.stack.setCurrentWidget(self.chooser)
            self.osd_until = 0
            return
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

    def choose_disc(self, root):
        if not self.pending_disc:
            return
        token, result = self.pending_disc
        self.pending_disc = None
        if root == self.disc_root:
            self.stack.setCurrentWidget(self.video)
            self.start(token, result)
        else:
            self.play(self.current, menus=self.use_menus, disc_root=root)

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
        menu.addSeparator()
        menu.addAction(self.act_updates)
        menu.exec(self.tracks_btn.mapToGlobal(QPoint(0, -menu.sizeHint().height() - 6)))
        menu.deleteLater()  # parented to the window, so it would otherwise live until the app quits

    def add_disc_choices(self, menu):
        view = self.proxy.view if self.proxy else None
        if not view or len(view.discs) < 2:
            return
        choices = menu.addMenu("Discs in This Image")
        for root in view.discs:
            label = disc_label(root)
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
        self.back.setGeometry(area.left() + 18, area.top() + 18, 40, 40)
        self.osd.move(area.left() + 70, area.top() + 18)

    def tick(self):
        if self.stack.currentWidget() is not self.video and not any(
                overlay.shown for overlay in (self.controls, self.back, self.osd)):
            return
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
        self.back.fade(show)
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
    args = sys.argv[1:]
    result_file = None
    if "--update-result" in args and args.index("--update-result") + 1 < len(args):
        index = args.index("--update-result")
        result_file = args[index + 1]
        del args[index:index + 2]
    if "--check" in sys.argv:
        check()
        if result_file:
            updates.finish_update(result_file)
        return
    app = QApplication(sys.argv)
    app.setApplicationName("isoplay")
    app.setWindowIcon(svg_icon((BASE / "assets/icon.svg").read_bytes(), 256))
    w = Player()
    w.show()
    if args:
        w.play(args[0])
    if result_file:
        w.raise_()
        w.activateWindow()
        w.updater.complete_restart(result_file)
    sys.exit(app.exec())
