#!/usr/bin/env python3
"""Run isoplay from this checkout, without installing or building anything.

    python dev.py              start the app
    python dev.py <file|url>   start it on something
    python dev.py --tests      run the test suite

Creates .venv on first use and keeps it in step with requirements.txt. libmpv 0.42+ comes from
tools/build_mpv.py (build/mpv, needed for remote discs) or else the system."""
import os
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
PYTHON = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
INSTALLED = VENV / "requirements.installed"
MPV_HELP = """isoplay needs libmpv 0.42.0 or newer: run python tools/build_mpv.py (remote discs
need its patch), or install mpv and point LIBMPV_PATH at the library if it isn't found."""


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

    env = java_env() or dict(os.environ)
    if (ROOT / "build/libbluray").is_dir():
        env.setdefault("LIBBLURAY_CP", (ROOT / "build/libbluray").as_posix() + "/")  # jars matching the system libbluray
    built = ROOT / "build/mpv" / {"darwin": "lib/libmpv.2.dylib", "win32": "bin/libmpv-2.dll"}.get(sys.platform, "lib/libmpv.so.2")
    if built.is_file():
        env.setdefault("LIBMPV_PATH", str(built))
    if subprocess.run([PYTHON, "-c", "from isoplay.mpv import binding; binding()"], cwd=ROOT, env=env).returncode:
        sys.exit(MPV_HELP)
    return subprocess.run([PYTHON, "run.py", *sys.argv[1:]], cwd=ROOT, env=env).returncode


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
