"""Native update dialogs; network and installation work stay off the GUI thread."""
import shutil
import sys
import threading

from PySide6.QtCore import QObject, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog

from isoplay import updates


class UpdateUI(QObject):
    checked = Signal(bool, object)
    progress = Signal(int)
    downloaded = Signal(object)
    finished = Signal(object)
    busy_changed = Signal(bool)

    def __init__(self, parent, version, settings, resume_args):
        super().__init__(parent)
        self.window, self.version, self.settings, self.resume_args = parent, version, settings, resume_args
        self.busy = False
        self.cancel = None
        self.dialog = self.prompt = None
        self.checked.connect(self.on_checked)
        self.progress.connect(self.on_progress)
        self.downloaded.connect(self.on_downloaded)
        self.finished.connect(self.on_finished)
        QApplication.instance().aboutToQuit.connect(self.cancel_download)

    def set_busy(self, value):
        self.busy = value
        self.busy_changed.emit(value)

    def automatic_check(self):
        if getattr(sys, "frozen", False) and self.settings.value("updates/automatic", True, type=bool):
            self.check()

    def check(self, manual=False):
        if self.busy or self.prompt:
            return
        self.set_busy(True)
        def work():
            try:
                result = updates.check_for_update(self.version)
            except Exception as error:
                result = error
            self.checked.emit(manual, result)
        threading.Thread(target=work, daemon=True).start()

    def on_checked(self, manual, result):
        self.set_busy(False)
        if isinstance(result, Exception):
            if manual:
                QMessageBox.warning(self.window, "Check for Updates", f"Couldn't check for updates.\n\n{result}")
            return
        if result is None:
            if manual:
                QMessageBox.information(self.window, "Check for Updates", "You're using the latest version of IsoPlay.")
            return
        target, reason = None, ""
        try:
            if not getattr(sys, "frozen", False):
                raise updates.UpdateError("Automatic installation is available in the downloaded app.")
            target = updates.installation_target()
        except updates.UpdateError as error:
            reason = str(error)
        prompt = QMessageBox(self.window)
        self.prompt = prompt
        prompt.setWindowTitle("IsoPlay Update")
        prompt.setTextFormat(Qt.PlainText)
        prompt.setText(f"IsoPlay {result.tag} is available")
        prompt.setInformativeText(reason or "Download the update and restart IsoPlay to install it.")
        prompt.setDetailedText(result.notes)
        install = prompt.addButton("Download and Restart" if target else "Open Downloads", QMessageBox.AcceptRole)
        prompt.addButton("Later", QMessageBox.RejectRole)
        prompt.setDefaultButton(install)
        def chosen(_):
            self.prompt = None
            if prompt.clickedButton() is install:
                if target:
                    self.download(result, target)
                else:
                    QDesktopServices.openUrl(QUrl(result.url))
            prompt.deleteLater()
        prompt.finished.connect(chosen)
        prompt.open()

    def cancel_download(self):
        if self.cancel:
            self.cancel.set()

    def download(self, release, target):
        self.set_busy(True)
        self.cancel = threading.Event()
        cancel = self.cancel
        resume_args = self.resume_args()
        self.dialog = QProgressDialog("Downloading the update…", "Cancel", 0, 100, self.window)
        self.dialog.setWindowTitle("Update IsoPlay")
        self.dialog.setWindowModality(Qt.WindowModal)
        self.dialog.setAutoClose(False)
        self.dialog.setAutoReset(False)
        self.dialog.setMinimumDuration(0)
        self.dialog.canceled.connect(self.cancel_download)
        self.dialog.show()
        def work():
            manifest = None
            try:
                manifest = updates.prepare_update(release, target, self.progress.emit, cancel, resume_args=resume_args)
                self.progress.emit(100)
                installer = updates.launch_installer(manifest, cancel)
                result = (manifest, installer)
            except Exception as error:
                if manifest:
                    shutil.rmtree(manifest.parent, ignore_errors=True)
                result = error
            self.downloaded.emit(result)
        threading.Thread(target=work, daemon=True).start()

    def on_progress(self, value):
        if not self.dialog:
            return
        if value == 100:
            self.dialog.setLabelText("Preparing to restart…")
            self.dialog.setRange(0, 0)
            self.dialog.setCancelButton(None)
        else:
            self.dialog.setValue(value)

    def on_downloaded(self, result):
        if not isinstance(result, Exception) and self.cancel.is_set():
            manifest, installer = result
            installer.terminate()
            installer.wait(timeout=10)
            shutil.rmtree(manifest.parent, ignore_errors=True)
            result = updates.UpdateCancelled("Update cancelled")
        self.cancel = None
        if self.dialog:
            self.dialog.close()
            self.dialog.deleteLater()
            self.dialog = None
        self.set_busy(False)
        if isinstance(result, updates.UpdateCancelled):
            return
        if isinstance(result, Exception):
            QMessageBox.warning(self.window, "Update IsoPlay", f"Couldn't install the update.\n\n{result}")
            return
        QApplication.instance().quit()

    def complete_restart(self, manifest):
        def work():
            try:
                result = updates.finish_update(manifest)
            except Exception as error:
                result = error
            self.finished.emit(result)
        threading.Thread(target=work, daemon=True).start()

    def on_finished(self, result):
        if isinstance(result, Exception):
            QMessageBox.warning(self.window, "Update IsoPlay", str(result))
        elif result["success"]:
            QMessageBox.information(self.window, "IsoPlay Updated", f"You're now using IsoPlay {result['tag']}.")
        else:
            QMessageBox.warning(self.window, "Update IsoPlay", "The update couldn't start. Your previous version was restored.\n\n"
                                + result.get("error", ""))
