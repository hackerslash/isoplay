#!/usr/bin/env python3
"""Run isoplay from this checkout, without installing or building anything.

    python dev.py              start the app
    python dev.py <file|url>   start it on something
    python dev.py --mpv [...]  start it under the mpv engine (futureplan item 1's prototype)
    python dev.py --tests      run the test suite

Creates .venv on first use and keeps it in step with requirements.txt. Neither libVLC nor
libmpv is a Python package, so the libraries the chosen engine needs come from the system;
--mpv wants mpv 0.42.0 or newer, the first to bring disc menus back."""
import os
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
PYTHON = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
INSTALLED = VENV / "requirements.installed"
VLC_HELP = """isoplay needs VLC installed, which provides libVLC.
  macOS    brew install --cask vlc
  Windows  winget install VideoLAN.VLC
  Linux    sudo apt install vlc        (or your distribution's package)"""
MPV_HELP = """the mpv engine needs libmpv, mpv 0.42.0 or newer for its disc menus.
  macOS    brew upgrade mpv | brew install mpv
  Windows  put libmpv-2.dll next to run.py (mpv-player-windows/libmpv)
  Linux    sudo apt install libmpv2     (or install mpv)
If more than one libmpv is installed, point LIBMPV_PATH at the exact dylib/dll."""


def main():
    if not PYTHON.exists():
        print(f"creating {VENV.name}", flush=True)
        venv.EnvBuilder(with_pip=True, upgrade_deps=True).create(VENV)
    wanted = (ROOT / "requirements.txt").read_text()
    if not INSTALLED.exists() or INSTALLED.read_text() != wanted:
        subprocess.run([PYTHON, "-m", "pip", "install", "-q", "-r", "requirements.txt"], cwd=ROOT, check=True)
        INSTALLED.write_text(wanted)
    if sys.argv[1:2] == ["--tests"]:
        return subprocess.run([PYTHON, "-m", "unittest", "discover", "-s", "tests"], cwd=ROOT).returncode

    env = java_env()
    args = list(sys.argv[1:])
    if "--mpv" in args:
        args.remove("--mpv")
        if subprocess.run([PYTHON, "-c", "from isoplay.mpv import binding; binding()"], cwd=ROOT).returncode:
            sys.exit(MPV_HELP)
        env = {**(env or os.environ), "ISOPLAY_ENGINE": "mpv"}
    elif subprocess.run([PYTHON, "-c", "import vlc"], cwd=ROOT, capture_output=True).returncode:
        sys.exit(VLC_HELP)
    return subprocess.run([PYTHON, "run.py", *args], cwd=ROOT, env=env).returncode


def java_env():
    """The bundle's Java runtime, fonts included, so BD-J menus behave as they will once built."""
    import build
    if not (build.BUILD / "jre/lib/fonts/LucidaSansRegular.ttf").exists():
        build.BUILD.mkdir(exist_ok=True)
        try:
            build.jre()
        except (OSError, subprocess.CalledProcessError):
            print("no JDK found: Java disc menus that draw text will stay black", flush=True)
            return None
    return {**os.environ, "JAVA_HOME": str(build.BUILD / "jre")}


if __name__ == "__main__":
    raise SystemExit(main())
