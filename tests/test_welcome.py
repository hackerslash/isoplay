"""Resize regressions for a populated welcome screen."""
import os
import time
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QPoint, QSettings, Qt
from PySide6.QtGui import QAction, QImage, QRegion
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QMenu, QPushButton, QScrollArea, QToolButton, QWidget

from isoplay import theme
from isoplay.app import Player
from isoplay.media import origin
from isoplay.widgets import ElidedLabel, MediaBadge, OptionsMenu, PreferenceRow, RecentRow


class WelcomeHarness:
    def __init__(self):
        self.act_open = QAction("Open File…")
        self.act_url = QAction("Open Link…")
        self.act_menus = QAction("Start Discs at Their Menu")
        self.act_menus.setCheckable(True)
        self.act_auto_updates = QAction("Check for Updates Automatically")
        self.act_auto_updates.setCheckable(True)
        self.act_updates = QAction("Check for Updates…")
        self.cache_menu = QMenu("Disk Cache")
        self.audio_menu = QMenu("Audio Language")
        self.sub_menu = QMenu("Subtitle Language")
        self.act_passthrough = QAction("Pass Audio Through to Receiver")
        self.act_diagnostics = QAction("Copy Diagnostics")
        self.settings = Mock()
        self.updater = Mock()
        self.browse = self.ask_url = self.play = self.clear_recent = self.menus_changed = Mock()
        self.forget = Mock()
        self.items = [{"src": f"https://very-long-source-name-{i}.workers.dev/a/movie.iso",
                       "name": "A very long movie filename with resolution and audio details.iso",
                       "kind": "bluray", "ts": time.time() - i * 600} for i in range(8)]

    def recent(self):
        return self.items

    def refresh_recent(self):
        Player.refresh_recent(self)


class WelcomeLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_recent_text_is_not_crushed_when_window_is_short(self):
        owner = WelcomeHarness()
        page = Player.build_welcome(owner)
        page.setStyleSheet(theme.CSS + theme.MENU_CSS)
        try:
            page.show()
            for width, height in ((1120, 630), (640, 400), (800, 500), (1280, 800), (640, 400)):
                page.setFixedSize(width, height)
                self.app.processEvents()
                scroll = page.findChild(QScrollArea, "recentScroll")
                self.assertTrue(page.rect().contains(scroll.mapTo(page, QPoint(0, 0))))
                self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
                rows = page.findChildren(RecentRow)
                self.assertEqual(len(rows), 8)
                for row in rows:
                    for name in ("rowtitle", "rowmeta"):
                        label = row.findChild(QLabel, name)
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
        page = Player.build_welcome(owner)
        page.setStyleSheet(theme.CSS + theme.MENU_CSS)
        try:
            page.setFixedSize(640, 400)
            page.show()
            self.app.processEvents()
            options = page.findChild(QToolButton)
            self.assertEqual(options.accessibleName(), "Options")
            actions = options.menu().actions()
            rows = options.menu().findChildren(PreferenceRow)
            self.assertEqual(len(rows), 2)
            rows[0].click()
            self.assertTrue(owner.act_menus.isChecked())
            self.assertEqual(rows[0].accessibleDescription(), "On")
            rows[1].click()
            self.assertTrue(owner.act_auto_updates.isChecked())
            self.assertIn(owner.act_updates, actions)
            for label in page.findChildren(ElidedLabel):
                self.assertLessEqual(label.fontMetrics().horizontalAdvance(label.text()), label.width())
            title = page.findChildren(RecentRow)[0].findChild(QLabel, "rowtitle")
            self.assertIn("…", title.text())
            self.assertTrue(title.text().endswith(".iso"))
        finally:
            page.close()

    def test_recent_types_use_distinct_accessible_badges_not_metadata_text(self):
        for kind, source, expected in (("bluray", "film.iso", "Blu-ray disc"),
                                       ("dvd", "film.iso", "DVD disc"),
                                       (None, "film.mkv", "Media file"),
                                       (None, "film.iso", "Disc image")):
            with self.subTest(kind=kind, source=source):
                item = {"src": source, "name": "Film", "kind": kind, "ts": time.time()}
                row = RecentRow(item, Mock(), Mock())
                badge = row.findChild(MediaBadge)
                self.assertEqual(badge.accessibleName(), expected)
                self.assertIn(expected, row.toolTip())
                meta = row.findChild(ElidedLabel, "rowmeta")
                self.assertEqual(meta.full_text, origin(source))
                row.close()

    def test_malformed_recent_entries_are_ignored(self):
        owner = WelcomeHarness()
        owner.settings = QSettings("isoplay-tests", "recent")
        owner.settings.setValue("recent", '[{"src": "/a.iso", "name": "A", "ts": 1}, "junk", {"src": "/b.iso"}, 7]')
        self.addCleanup(owner.settings.clear)
        self.assertEqual([i["src"] for i in Player.recent(owner)], ["/a.iso"])
        owner.settings.setValue("recent", "not json")
        self.assertEqual(Player.recent(owner), [])

    def test_back_overlay_leaves_the_video(self):
        class Owner(QWidget):
            close_video = Mock()

        owner = Owner()
        overlay = Player.build_back(owner)
        button = overlay.findChild(QPushButton)
        self.assertEqual(button.accessibleName(), "Back to the start screen")
        button.click()
        self.assertTrue(Owner.close_video.called, "the back control does not leave the video")
        overlay.close()

    def test_preference_rows_toggle_from_the_keyboard(self):
        owner = WelcomeHarness()
        menu = OptionsMenu(None, owner.act_menus, owner.act_auto_updates, owner.act_updates)
        menu.show()
        self.app.processEvents()
        QTest.keyClick(menu, Qt.Key_Down)
        QTest.keyClick(menu, Qt.Key_Return)
        self.assertTrue(owner.act_menus.isChecked(), "menu rows are not keyboard reachable")
        menu.close()
        with_cache = OptionsMenu(None, owner.act_menus, owner.act_auto_updates, owner.act_updates, owner.cache_menu)
        self.assertIn(owner.cache_menu.menuAction(), with_cache.actions())

    def test_badges_fill_their_box_without_spilling_into_the_corners(self):
        host = QWidget()
        for kind, source in (("bluray", "f.iso"), ("dvd", "f.iso"), (None, "f.mkv"), (None, "f.img")):
            with self.subTest(kind=kind, source=source):
                badge = MediaBadge(kind, source)
                badge.setParent(host)
                box, name = badge.width(), badge.accessibleName()
                shot = QImage(badge.size(), QImage.Format_ARGB32)
                shot.fill(0)
                badge.render(shot, QPoint(), QRegion(), QWidget.RenderFlag.DrawChildren)
                painted = [x for y in range(box) for x in range(box) if shot.pixelColor(x, y).alpha() > 0]
                self.assertTrue(painted, f"{name} painted nothing")
                width = max(painted) - min(painted) + 1
                self.assertGreaterEqual(width, 0.75 * box, f"{name} renders at {width}px in a {box}px badge")
                for x, y in ((0, 0), (box - 1, 0), (0, box - 1), (box - 1, box - 1)):
                    self.assertEqual(shot.pixelColor(x, y).alpha(), 0, f"{name} spills to ({x}, {y})")
                badge.close()

    def test_empty_recent_list_stays_within_short_window(self):
        owner = WelcomeHarness()
        owner.items = []
        page = Player.build_welcome(owner)
        page.setStyleSheet(theme.CSS + theme.MENU_CSS)
        try:
            page.setFixedSize(640, 400)
            page.show()
            self.app.processEvents()
            self.assertFalse(owner.clear_btn.isVisible())
            self.assertEqual(len(page.findChildren(RecentRow)), 0)
            scroll = page.findChild(QScrollArea, "recentScroll")
            self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
        finally:
            page.close()


if __name__ == "__main__":
    unittest.main()
