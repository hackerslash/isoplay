"""Run the actual packaged updater on each CI OS, without requiring a GUI.

The relaunched bundle uses its existing --check smoke test, then acknowledges
startup through the same handshake as the regular GUI. Source unit tests cover
failures; this test catches missing files, permissions, DLL locks and symlinks.
"""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

import updates


def main():
    root = Path(__file__).resolve().parent
    name = updates.asset_name()
    archive = root / "dist" / name
    source = root / "dist" / ("isoplay.app" if sys.platform == "darwin" else "isoplay")
    with tempfile.TemporaryDirectory(prefix="isoplay-update-smoke-") as directory:
        target = Path(directory).resolve() / source.name
        shutil.copytree(source, target, symlinks=True)
        info = updates.Release("v0.1.4", "Smoke test", "", name, "https://example.invalid/test",
                               archive.stat().st_size, hashlib.sha256(archive.read_bytes()).hexdigest())
        with patch.object(updates, "urlopen", side_effect=lambda *a, **k: archive.open("rb")):
            manifest = updates.prepare_update(info, target, resume_args=["--check"])
        try:
            # The old process holds its app binary open until it exits. This
            # also tests waiting for Windows file locks before replacing it.
            executable = updates._executable(target, sys.platform)
            old = subprocess.Popen([sys.executable, "-c",
                                    "import sys,time; f=open(sys.argv[1], 'rb'); time.sleep(3)", str(executable)])
            threading.Thread(target=old.wait, daemon=True).start()
            data = json.loads(manifest.read_text())
            data["parent_pid"] = old.pid
            manifest.write_text(json.dumps(data))
            installer = updates.launch_installer(manifest)
            code = installer.wait(timeout=150)
            if code:
                raise RuntimeError((manifest.parent / "installer.log").read_text(errors="replace"))
            deadline = time.monotonic() + 30
            while manifest.parent.exists() and time.monotonic() < deadline:
                time.sleep(0.2)
            assert not Path(data["backup"]).exists(), "successful update did not clean its backup"
            assert not manifest.parent.exists(), "update helper files were not cleaned"
            subprocess.run([str(executable), "--check"], check=True, timeout=30, env=updates._environment())
        finally:
            shutil.rmtree(manifest.parent, ignore_errors=True)
    print("Packaged update, restart and cleanup passed:", sys.platform)


if __name__ == "__main__":
    main()
