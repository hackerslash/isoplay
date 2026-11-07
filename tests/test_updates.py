import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import zipfile

from isoplay import updates


def archive(system="linux", unsafe=False):
    output = io.BytesIO()
    root = "isoplay.app" if system == "darwin" else "isoplay"
    executable = "Contents/MacOS/isoplay" if system == "darwin" else "isoplay.exe" if system == "win32" else "isoplay"
    files = {f"{root}/{executable}": b"new app", f"{root}/_internal/version": b"new"}
    if unsafe:
        files[f"{root}/../../escape"] = b"bad"
    if system == "linux":
        with tarfile.open(fileobj=output, mode="w:gz") as bundle:
            for name, data in files.items():
                item = tarfile.TarInfo(name)
                item.size, item.mode = len(data), 0o755
                bundle.addfile(item, io.BytesIO(data))
    else:
        with zipfile.ZipFile(output, "w") as bundle:
            for name, data in files.items():
                item = zipfile.ZipInfo(name)
                item.external_attr = (stat.S_IFREG | 0o755) << 16
                bundle.writestr(item, data)
            if system == "darwin":
                item = zipfile.ZipInfo(f"{root}/Contents/link")
                item.external_attr = (stat.S_IFLNK | 0o777) << 16
                bundle.writestr(item, "MacOS/isoplay")
    return output.getvalue()


def release(blob, system="linux"):
    name = updates.asset_name(system, "arm64" if system == "darwin" else "amd64")
    return updates.Release("v0.1.5", "Changes", "https://github.com/hackerslash/isoplay/releases/tag/v0.1.5",
                           name, "https://github.com/hackerslash/isoplay/releases/download/v0.1.5/" + name,
                           len(blob), hashlib.sha256(blob).hexdigest())


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.target = Path(self.directory.name) / "isoplay"
        (self.target / "_internal").mkdir(parents=True)
        (self.target / "isoplay").write_bytes(b"old app")
        self.plans = []
        self.addCleanup(lambda: [shutil.rmtree(p.parent, ignore_errors=True) for p in self.plans])

    def prepare(self, blob=None, system="linux"):
        blob = archive(system) if blob is None else blob
        with patch.object(updates, "urlopen", return_value=io.BytesIO(blob)):
            plan = updates.prepare_update(release(blob, system), self.target, system=system,
                                          resume_args=["https://example.com/movie.mkv"])
        self.plans.append(plan)
        return plan

    def test_platform_assets_and_stable_version_order(self):
        self.assertEqual(updates.asset_name("darwin", "arm64"), "isoplay-macos-arm64.zip")
        self.assertEqual(updates.asset_name("win32", "AMD64"), "isoplay-windows-amd64.zip")
        self.assertEqual(updates.asset_name("linux", "x86_64"), "isoplay-linux-x86_64.tar.gz")
        self.assertGreater(updates.version("v0.1.10"), updates.version("0.1.9"))
        with self.assertRaises(updates.UpdateError):
            updates.asset_name("darwin", "x86_64")

    def test_latest_release_requires_matching_asset_and_digest(self):
        blob = archive()
        info = release(blob)
        payload = {"tag_name": info.tag, "html_url": info.url, "body": info.notes,
                   "assets": [{"name": info.asset, "size": info.size, "digest": "sha256:" + info.digest,
                               "browser_download_url": info.download}]}
        def check(current="0.1.4"):
            with patch.object(updates, "urlopen", return_value=io.BytesIO(json.dumps(payload).encode())):
                return updates.check_for_update(current, "linux", "x86_64")
        self.assertEqual(check(), info)
        self.assertIsNone(check("0.1.5"))
        payload["assets"][0]["browser_download_url"] = "https://example.com/wrong-download"
        with self.assertRaises(updates.UpdateError):
            check()
        payload["prerelease"] = True
        self.assertIsNone(check())

    def test_stage_all_three_formats_without_touching_current_app(self):
        for system in ("linux", "win32", "darwin"):
            with self.subTest(system=system):
                plan = self.prepare(system=system)
                data = json.loads(plan.read_text())
                executable = updates._executable(Path(data["stage"]), system)
                self.assertEqual(executable.read_bytes(), b"new app")
                self.assertEqual((self.target / "isoplay").read_bytes(), b"old app")
                if system == "darwin":
                    self.assertTrue((Path(data["stage"]) / "Contents/link").is_symlink())

    def test_corrupt_download_and_cancel_leave_app_untouched(self):
        blob = archive()
        with patch.object(updates, "urlopen", return_value=io.BytesIO(blob[:-10])):
            with self.assertRaisesRegex(updates.UpdateError, "integrity"):
                updates.prepare_update(release(blob), self.target)
        cancel = threading.Event()
        cancel.set()
        with patch.object(updates, "urlopen", return_value=io.BytesIO(blob)):
            with self.assertRaises(updates.UpdateCancelled):
                updates.prepare_update(release(blob), self.target, cancel=cancel)
        self.assertEqual((self.target / "isoplay").read_bytes(), b"old app")

    def test_archive_traversal_rejected(self):
        for system in ("linux", "win32", "darwin"):
            with self.subTest(system=system):
                with self.assertRaises((updates.UpdateError, tarfile.FilterError)):
                    self.prepare(archive(system, unsafe=True), system)
        self.assertFalse((self.target.parent / "escape").exists())

    def test_successful_swap_waits_for_boot_then_cleans_backup(self):
        plan = self.prepare()
        process = Mock()
        with patch.object(updates, "_wait_for_exit") as wait, patch.object(updates, "_launch", return_value=process) as launch, \
                patch.object(updates, "_wait_for_boot", return_value=True):
            self.assertEqual(updates.apply_update(plan), 0)
        wait.assert_called_once_with(os.getpid())
        self.assertIn("https://example.com/movie.mkv", launch.call_args.args[0])
        self.assertEqual((self.target / "isoplay").read_bytes(), b"new app")
        data = json.loads(plan.read_text())
        backup = Path(data["backup"])
        self.assertEqual((backup / "isoplay").read_bytes(), b"old app")
        self.assertTrue(updates.finish_update(plan, self.target / "isoplay")["success"])
        self.assertFalse(backup.exists())
        self.assertFalse(plan.parent.exists())

    def test_new_app_failure_restores_previous_app_and_reopens_it(self):
        plan = self.prepare()
        process = Mock()
        process.poll.return_value = None
        with patch.object(updates, "_wait_for_exit"), patch.object(updates, "_launch", return_value=process) as launch, \
                patch.object(updates, "_wait_for_boot", return_value=False):
            self.assertEqual(updates.apply_update(plan), 1)
        process.terminate.assert_called_once()
        self.assertEqual(launch.call_count, 2)
        self.assertEqual((self.target / "isoplay").read_bytes(), b"old app")
        self.assertFalse(json.loads((plan.parent / "result.json").read_text())["success"])

    def test_copy_failure_preserves_previous_app(self):
        plan = self.prepare()
        with patch.object(updates, "_wait_for_exit"), patch.object(updates.shutil, "copytree", side_effect=OSError("disk full")), \
                patch.object(updates, "_launch"):
            self.assertEqual(updates.apply_update(plan), 1)
        self.assertEqual((self.target / "isoplay").read_bytes(), b"old app")

    def test_child_environment_does_not_reuse_running_bundle(self):
        with patch.dict(os.environ, {"_PYI_APPLICATION_HOME_DIR": "old", "LD_LIBRARY_PATH": "old-libs",
                                     "LD_LIBRARY_PATH_ORIG": "system-libs", "JAVA_HOME": "old-java"}):
            env = updates._environment()
        self.assertNotIn("_PYI_APPLICATION_HOME_DIR", env)
        self.assertNotIn("JAVA_HOME", env)
        self.assertEqual(env["LD_LIBRARY_PATH"], "system-libs")
        self.assertEqual(env["PYINSTALLER_RESET_ENVIRONMENT"], "1")


if __name__ == "__main__":
    unittest.main()
