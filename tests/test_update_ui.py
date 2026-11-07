import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QWidget

from isoplay.update_ui import UpdateUI
from isoplay import updates


class UpdateDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.window = QWidget()
        self.settings = QSettings(str(Path(self.directory.name) / "settings.ini"), QSettings.IniFormat)
        self.ui = UpdateUI(self.window, "0.1.4", self.settings, lambda: [])

    def tearDown(self):
        if self.ui.prompt:
            self.ui.prompt.reject()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def test_automatic_checks_respect_preference_and_packaged_app(self):
        with patch.object(self.ui, "check") as check:
            with patch("isoplay.update_ui.sys.frozen", False, create=True):
                self.ui.automatic_check()
                check.assert_not_called()
            with patch("isoplay.update_ui.sys.frozen", True, create=True):
                self.ui.automatic_check()
                check.assert_called_once_with()
                self.settings.setValue("updates/automatic", False)
                self.ui.automatic_check()
                self.assertEqual(check.call_count, 1)

    def test_background_failures_are_quiet_manual_failures_explain(self):
        with patch("isoplay.update_ui.QMessageBox.warning") as warning:
            self.ui.on_checked(False, OSError("offline"))
            warning.assert_not_called()
            self.ui.on_checked(True, OSError("offline"))
            warning.assert_called_once()

    def test_available_release_popup_starts_installation_on_accept(self):
        release = updates.Release("v0.1.5", "Changes", "https://github.com/hackerslash/isoplay/releases/tag/v0.1.5",
                                  "isoplay.zip", "", 1, "0" * 64)
        target = Path(self.directory.name) / "isoplay"
        with patch("isoplay.update_ui.sys.frozen", True, create=True), patch.object(updates, "installation_target", return_value=target):
            self.ui.on_checked(False, release)
        self.assertIn("v0.1.5", self.ui.prompt.text())
        self.assertEqual(self.ui.prompt.buttons()[0].text(), "Download and Restart")
        with patch.object(self.ui, "download") as download:
            self.ui.prompt.buttons()[0].click()
            self.app.processEvents()
            download.assert_called_once_with(release, target)

    def test_cancelled_download_does_not_quit_or_show_failure(self):
        self.ui.cancel = threading.Event()
        self.ui.cancel.set()
        with patch("isoplay.update_ui.QApplication.instance") as application, patch("isoplay.update_ui.QMessageBox.warning") as warning:
            self.ui.on_downloaded(updates.UpdateCancelled("cancelled"))
            application.return_value.quit.assert_not_called()
            warning.assert_not_called()
        self.assertFalse(self.ui.busy)


if __name__ == "__main__":
    unittest.main()
