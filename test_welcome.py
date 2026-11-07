"""Resize regressions for a populated welcome screen, without starting VLC."""
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# CI can exercise this native Qt layout on every OS after the bundle is built,
# without requiring a separate system VLC installation on Windows or macOS.
bundle = Path(__file__).resolve().parent / "dist"
runtime = bundle / ("isoplay.app/Contents/Frameworks" if sys.platform == "darwin" else "isoplay/_internal")
library = runtime / "vlc" / ("lib/libvlc.dylib" if sys.platform == "darwin" else "libvlc.dll" if sys.platform == "win32" else "lib/libvlc.so.5")
if library.is_file():
    os.environ["PYTHON_VLC_LIB_PATH"] = str(library)
    if sys.platform == "win32":
        dll_directory = os.add_dll_directory(str(library.parent))
    elif sys.platform.startswith("linux"):
        import ctypes
        ctypes.CDLL(str(library.parent / "libvlccore.so.9"), mode=ctypes.RTLD_GLOBAL)
import isoplay


class WelcomeHarness:
    def __init__(self):
        self.act_open = isoplay.QAction("Open File…")
        self.act_url = isoplay.QAction("Open Location…")
        self.act_menus = isoplay.QAction("Start Discs at Their Menu")
        self.act_menus.setCheckable(True)
        self.act_auto_updates = isoplay.QAction("Check for Updates Automatically")
        self.act_auto_updates.setCheckable(True)
        self.act_updates = isoplay.QAction("Check for Updates…")
        self.settings = Mock()
        self.updater = Mock()
        self.browse = self.ask_url = self.play = self.clear_recent = self.menus_changed = Mock()
        self.items = [{"src": f"https://very-long-source-name-{i}.workers.dev/a/movie.iso",
                       "name": "A very long movie filename with resolution and audio details.iso",
                       "kind": "bluray", "ts": time.time() - i * 600} for i in range(8)]

    def recent(self):
        return self.items

    def refresh_recent(self):
        isoplay.Player.refresh_recent(self)


class WelcomeLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = isoplay.QApplication.instance() or isoplay.QApplication([])

    def test_recent_text_is_not_crushed_when_window_is_short(self):
        owner = WelcomeHarness()
        page = isoplay.Player.build_welcome(owner)
        page.setStyleSheet(isoplay.CSS.replace("CHECK", "") + isoplay.MENU_CSS)
        try:
            page.show()
            for width, height in ((1120, 630), (640, 400), (800, 500), (1280, 800), (640, 400)):
                page.setFixedSize(width, height)
                self.app.processEvents()
                scroll = page.findChild(isoplay.QScrollArea, "recentScroll")
                self.assertTrue(page.rect().contains(scroll.mapTo(page, isoplay.QPoint(0, 0))))
                self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
                rows = page.findChildren(isoplay.RecentRow)
                self.assertEqual(len(rows), 8)
                for row in rows:
                    for name in ("rowtitle", "rowmeta"):
                        label = row.findChild(isoplay.QLabel, name)
                        self.assertGreaterEqual(label.height(), label.fontMetrics().height(),
                                                f"{name} clipped at {width}x{height}: {label.height()}px")
                        self.assertTrue(row.rect().contains(label.geometry()))
                if height == 400:
                    self.assertGreater(scroll.verticalScrollBar().maximum(), 0)
                    scroll.ensureWidgetVisible(rows[-1])
                    self.app.processEvents()
                    bottom = rows[-1].mapTo(scroll.viewport(), rows[-1].rect().bottomLeft())
                    self.assertLessEqual(bottom.y(), scroll.viewport().height())
        finally:
            page.close()

    def test_preferences_are_in_one_menu_and_rows_elide_to_available_width(self):
        owner = WelcomeHarness()
        owner.items[0]["name"] = "Very long movie " + "edition-and-audio-details-" * 12 + ".iso"
        page = isoplay.Player.build_welcome(owner)
        page.setStyleSheet(isoplay.CSS.replace("CHECK", "") + isoplay.MENU_CSS)
        try:
            page.setFixedSize(640, 400)
            page.show()
            self.app.processEvents()
            options = page.findChild(isoplay.QToolButton)
            self.assertEqual(options.text(), "Options")
            actions = options.menu().actions()
            self.assertIn(owner.act_menus, actions)
            self.assertIn(owner.act_auto_updates, actions)
            self.assertIn(owner.act_updates, actions)
            self.assertEqual(page.findChildren(isoplay.QCheckBox), [])
            for label in page.findChildren(isoplay.ElidedLabel):
                self.assertLessEqual(label.fontMetrics().horizontalAdvance(label.text()), label.width())
            title = page.findChildren(isoplay.RecentRow)[0].findChild(isoplay.QLabel, "rowtitle")
            self.assertIn("…", title.text())
            self.assertTrue(title.text().endswith(".iso"))
        finally:
            page.close()

    def test_empty_recent_list_stays_within_short_window(self):
        owner = WelcomeHarness()
        owner.items = []
        page = isoplay.Player.build_welcome(owner)
        page.setStyleSheet(isoplay.CSS.replace("CHECK", "") + isoplay.MENU_CSS)
        try:
            page.setFixedSize(640, 400)
            page.show()
            self.app.processEvents()
            self.assertFalse(owner.clear_btn.isVisible())
            self.assertEqual(len(page.findChildren(isoplay.RecentRow)), 0)
            scroll = page.findChild(isoplay.QScrollArea, "recentScroll")
            self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
        finally:
            page.close()


if __name__ == "__main__":
    unittest.main()
