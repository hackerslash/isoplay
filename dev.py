#!/usr/bin/env python3
"""Run isoplay from this checkout, without installing or building anything.

    python dev.py              start the app
    python dev.py <file|url>   start it on something
    python dev.py --tests      run the test suite

Creates .venv on first use and keeps it in step with requirements.txt. The patched libmpv (needed for
remote discs) comes from tools/build_mpv.py (build/mpv) or else the latest release (build/mpv-release)."""
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
import venv
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
PYTHON = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
INSTALLED = VENV / "requirements.installed"
RELEASE = ROOT / "build/mpv-release"
RELEASES = "https://github.com/hackerslash/isoplay/releases/latest/download"
BUNDLED = {"darwin": ("macos", "isoplay.app/Contents/Resources/mpv/", "libmpv.2.dylib"),
           "win32": ("windows", "isoplay/_internal/mpv/", "libmpv-2.dll")}
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
    elif released := release_mpv():
        env["LIBMPV_PATH"] = str(released)
        env["LIBBLURAY_CP"] = RELEASE.as_posix() + "/"  # the jars built for the libbluray beside it
    if subprocess.run([PYTHON, "-c", "from isoplay.mpv import binding; binding()"], cwd=ROOT, env=env).returncode:
        sys.exit(MPV_HELP)
    return subprocess.run([PYTHON, "run.py", *sys.argv[1:]], cwd=ROOT, env=env).returncode


def release_mpv():
    # Linux releases keep libmpv's dependencies outside the mpv folder, so there's nothing to reuse
    if sys.platform not in BUNDLED:
        return None
    system, folder, lib = BUNDLED[sys.platform]
    if not (RELEASE / lib).is_file():
        machine = platform.machine().lower().replace("x86_64", "amd64")
        url = f"{RELEASES}/isoplay-{system}-{machine}.zip"
        archive = ROOT / "build/release.zip"
        print(f"downloading libmpv from {url}", flush=True)
        try:
            archive.parent.mkdir(exist_ok=True)
            urllib.request.urlretrieve(url, archive)
            shutil.rmtree(RELEASE, ignore_errors=True)
            RELEASE.mkdir()
            with zipfile.ZipFile(archive) as z:
                for info in z.infolist():
                    if info.filename.startswith(folder) and not info.is_dir():
                        dest = RELEASE / info.filename[len(folder):]
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(z.read(info))
        except (OSError, zipfile.BadZipFile) as e:
            shutil.rmtree(RELEASE, ignore_errors=True)
            print(f"couldn't fetch the release libmpv ({e}): trying the system one", flush=True)
            return None
        finally:
            archive.unlink(missing_ok=True)
    return RELEASE / lib


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
