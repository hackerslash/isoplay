#!/usr/bin/env python3
"""Build the patched libmpv that can open a disc image over a stream, and the BD-J jar that
matches the system's libbluray.

    python tools/build_mpv.py

Why a patch: mpv 0.42 and master open Blu-ray/DVD images only through local device paths
(bd_open_disc/dvdnav_open), so isoplay's loopback URL falls through to FFmpeg and fails. VLC
plays the same remote images by using libbluray/libdvdnav's block-reader callbacks
(bd_open_stream/dvdnav_open_stream); tools/mpv-stream-discs.patch adds exactly that to
stream_bluray.c and stream_dvdnav.c.

libmpv lands in build/mpv, which dev.py and build.py pick up. The BD-J jars land in
build/libbluray, built from the libbluray mpv links against: a native 1.5.0 with a 1.4.1 jar
makes libbluray disable BD-J and play the main title.

Needs: a C compiler, meson, ninja, pkg-config, mpv's dependencies (FFmpeg, libplacebo, libass,
libdvdnav, libbluray), and ant plus a JDK for the jar. .github/workflows/build.yml installs them
on each platform.
"""
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
SRC = BUILD / "mpv-src"
MPV_BUILD = BUILD / "mpv-build"
PREFIX = BUILD / "mpv"
PATCH = ROOT / "tools" / "mpv-stream-discs.patch"
VERSION = "v0.42.0"


def run(*cmd, **kw):
    print("+", " ".join(map(str, cmd)), flush=True)
    subprocess.run(cmd, check=True, **kw)


def mpv():
    if not (SRC / ".git").is_dir():
        shutil.rmtree(SRC, ignore_errors=True)
        run("git", "clone", "-c", "core.autocrlf=false", "--depth", "1", "--branch", VERSION,
            "https://github.com/mpv-player/mpv.git", SRC)
    check = subprocess.run(["git", "-C", str(SRC), "apply", "--reverse", "--check", str(PATCH)],
                           capture_output=True)
    if check.returncode == 0:
        print("patch already applied", flush=True)
    else:
        run("git", "-C", str(SRC), "apply", str(PATCH))
    if not (MPV_BUILD / "build.ninja").exists():
        run("meson", "setup", MPV_BUILD, SRC, "--buildtype=release", f"--prefix={PREFIX}", "--libdir=lib",
            "-Dlibmpv=true", "-Dcplayer=false", "-Ddvdnav=enabled",
            "-Dlibbluray=enabled", "-Dlua=disabled", "-Djavascript=disabled", "-Dvapoursynth=disabled",
            "-Dlibarchive=disabled", "-Drubberband=disabled", "-Dmanpage-build=disabled")
    run("ninja", "-C", MPV_BUILD)
    run("ninja", "-C", MPV_BUILD, "install")


def bdj_jar():
    """libbluray wants a jar built from its own version; Homebrew installs none at all."""
    version = subprocess.run(["pkg-config", "--modversion", "libbluray"],
                             capture_output=True, text=True, check=True).stdout.strip()
    src = BUILD / f"libbluray-{version}"
    dist = BUILD / "libbluray"
    if not (src / "src/libbluray/bdj/build.xml").exists():
        archive = BUILD / f"libbluray-{version}.tar.xz"
        url = f"https://download.videolan.org/pub/videolan/libbluray/{version}/libbluray-{version}.tar.xz"
        print("+ download", url, flush=True)
        urllib.request.urlretrieve(url, archive)
        with tarfile.open(archive) as tar:
            tar.extractall(BUILD, filter="data")
    shutil.rmtree(dist, ignore_errors=True)
    run("ant", "-f", src / "src/libbluray/bdj/build.xml",
        f"-Dbuild={BUILD / 'bdj-build'}", f"-Ddist={dist}", "-Dbootclasspath=",
        "-Dsrc_awt=:java-j2se:java-build-support", f"-Djavac_path={shutil.which('javac')}",
        "-Djavac_arg=-Xlint:-deprecation", f"-Dversion=j2se-{version}",
        "-Djava_version_asm=1.8", "-Djava_version_bdj=1.8")  # JDK 17 refuses target 7
    print(f"jar for libbluray {version} -> {dist}", flush=True)


def main():
    BUILD.mkdir(exist_ok=True)
    if sys.platform == "darwin":
        os.environ.setdefault("PKG_CONFIG_PATH", "/opt/homebrew/lib/pkgconfig")
    mpv()
    bdj_jar()


if __name__ == "__main__":
    main()
