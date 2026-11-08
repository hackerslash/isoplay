#!/usr/bin/env python3
"""The futureplan item 1 spike: libmpv on one of the test ISOs, through isoplay's loopback server,
watching whether a disc menu renders, reports itself, and answers the mouse and the keyboard.

    python spike_mpv.py <file|link>            a disc image or video, local or a web link
    python spike_mpv.py --headless N <src>     run vo=null for N seconds, print observations

What it watches, so the open questions of futureplan #1 get answers:

- the `disc-menu-active` property while a synthetic Disc Menu edition is up;
- what `discnav up/select` and `discnav mouse-move`/`mouse-click` change on an HDMV disc
  (Eega), a BD-J disc (Shutter Island, Blade Runner) and a DVD; nav sends `mouse x y` first,
  because an argument-less discnav mouse-* maps `mouse-pos` itself (docs/spike-mpv.md);
- whether BD-J menus need a JVM and whether they run at all;
- rendering through MpvRenderContext into a QOpenGLWidget, as the real integration will.

The streaming layer is exactly the app's: prepare() builds the same loopback proxy, and libmpv
reads the same http://127.0.0.1:PORT/disc.iso it always did.

The mpv log is followed at `warn` level, so option and command mistakes (an old libmpv without
discnav, say) show up instead of silently doing nothing.
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from isoplay import media  # noqa: E402 - the runtime VLC env is set anyway; libmpv doesn't need it

USAGE = __doc__


def log(level, prefix, text):
    print(f"[mpv {level} {prefix}] {text.strip()}")


def observe(engine, note=""):
    player = engine.player
    try:
        state = (f"core-idle={player.core_idle} menu-active={engine.menu_active()} "
                 f"edition={player.edition or '-'} title={player.media_title or '-'} "
                 f"pos={engine.position():.3f}")
    except Exception as e:  # noqa: BLE001 - property names shift between mpv versions
        state = f"properties unavailable ({e})"
    print(f"[observe {note}] {state}")


def nav(engine, action, logline=True):
    if logline:
        print(f"[discnav] {action}")
    engine.navigate(action)
    time.sleep(0.3)


def headless(seconds, sources):
    """No window: does the disc open, do its menus report active, do nav commands change
    something observable. vo=null stands in for a real video output."""
    from isoplay.engine import create_engine

    engine = create_engine("mpv")
    engine.set_bridge(lambda name: print(f"[event] {name}"))
    engine.player.set_loglevel("warn")  # mistakes (an old libmpv, a wrong command) print, not hide
    engine.player._log_handler = log
    engine.player["vo"] = "null"
    for mrl, kind in sources:
        print("[open]", mrl)
        engine.open(mrl, kind=kind, menus=True)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            observe(engine, f"{deadline - time.monotonic():4.0f}s left")
            for action in ("right", "select"):
                nav(engine, action)
            time.sleep(4)
        engine.close()
    engine.shutdown()


def windowed(sources, menus=True):
    """The real shape: MpvRenderContext drawing into a Qt window, input forwarded like the app
    will do it."""
    from PySide6.QtCore import QTimer, Qt
    from PySide6.QtGui import QKeySequence, QShortcut
    from PySide6.QtWidgets import QApplication, QMainWindow
    from isoplay.engine import create_engine

    app = QApplication.instance() or QApplication(sys.argv)
    engine = create_engine("mpv")
    window = QMainWindow()
    window.setWindowTitle("isoplay mpv spike")
    window.resize(1120, 630)
    window.setCentralWidget(engine.surface(window))
    engine.settle(window.centralWidget())
    engine.set_bridge(lambda name: print(f"[event] {name}"))

    for key, action in (("Up", "up"), ("Down", "down"), ("Left", "left"), ("Right", "right"),
                        ("Return", "select"), ("Escape", "prev")):
        QShortcut(QKeySequence(key), window, activated=lambda a=action: nav(engine, a))
    QShortcut(QKeySequence(Qt.Key_Space), window, activated=lambda: engine.pause())
    QShortcut(QKeySequence("D"), window, activated=lambda: observe(engine, "D pressed"))
    status = window.statusBar()
    status.showMessage("arrows/enter: discnav · space: pause · D: observe · click: menu click")

    mrl, kind = sources[0]
    print("[open]", mrl)
    engine.open(mrl, kind=kind, menus=menus)
    QTimer.singleShot(2000, lambda: observe(engine, "2s in"))
    app.aboutToQuit.connect(engine.shutdown)
    window.show()
    status_app = app.exec()
    return status_app


def cli(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    headless_for = None
    if argv[0] == "--headless":
        headless_for, argv = float(argv[1]), argv[2:]
    if not argv:
        print(USAGE)
        return 2

    sources = []
    for src in argv:
        given = src if "://" in src else str(Path(src).resolve())
        try:
            mrl, kind, proxy, _discs, _root = media.prepare(given)
        except Exception as e:  # noqa: BLE001 - a bad source ends the spike's work with it
            print(f"[prepare failed] {src}: {e}")
            continue
        sources.append((mrl, kind))
    if not sources:
        print("nothing opened; try an .iso file or a disc folder link")
        return 1

    if headless_for:
        headless(headless_for, sources)
        return 0
    return windowed(sources)


if __name__ == "__main__":
    if not os.environ.get("ISOPLAY_ENGINE"):
        os.environ["ISOPLAY_ENGINE"] = "mpv"
    raise SystemExit(cli(sys.argv[1:]))
