#!/usr/bin/env python3
"""Build the patched libmpv that can open a disc image over a stream, and the BD-J jar that
matches the system's libbluray.

    python tools/build_mpv.py

Why a patch: mpv 0.42 and master open Blu-ray/DVD images only through local device paths
(bd_open_disc/dvdnav_open), so isoplay's loopback URL falls through to FFmpeg and fails. VLC
plays the same remote images by using libbluray/libdvdnav's block-reader callbacks
(bd_open_stream/dvdnav_open_stream); tools/mpv-stream-discs.patch adds exactly that to
stream_bluray.c and stream_dvdnav.c.

libdvdnav is built too, with a fix for streamed discs (see dvdnav()). libmpv lands in build/mpv,
which dev.py and build.py pick up. The BD-J jars land in
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
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
SRC = BUILD / "mpv-src"
MPV_BUILD = BUILD / "mpv-build"
PREFIX = BUILD / "mpv"
PATCH = ROOT / "tools" / "mpv-stream-discs.patch"
VERSION = "v0.42.0"
DVDNAV = "7.0.0"


def run(*cmd, **kw):
    print("+", " ".join(map(str, cmd)), flush=True)
    subprocess.run(cmd, check=True, **kw)


def fetch(name, version):
    """VideoLAN's release tarball of name-version, unpacked into build/."""
    src = BUILD / f"{name}-{version}"
    if src.is_dir():
        return src
    for ext in ("xz", "bz2"):  # releases before 1.3.4 or so ship only .tar.bz2
        archive = BUILD / f"{name}-{version}.tar.{ext}"
        url = f"https://download.videolan.org/pub/videolan/{name}/{version}/{archive.name}"
        print("+ download", url, flush=True)
        try:
            urllib.request.urlretrieve(url, archive)
            break
        except urllib.error.HTTPError as e:
            if e.code != 404 or ext == "bz2":
                raise
    with tarfile.open(archive) as tar:
        tar.extractall(BUILD, filter="data")
    return src


def dvdnav():
    """libdvdnav into build/mpv, fixed: restarting a stopped VM (a title's end, a menu's Stop)
    wipes the read callbacks of a disc opened on a stream, and the next read seeks through
    NULL. A crash with libdvdread's own reader, a stream of seek errors with libdvdcss's."""
    src = fetch("libdvdnav", DVDNAV)
    vm = src / "src/vm/vm.c"
    text = vm.read_text()
    wipe = "  else\n      vm->streamcb = (dvdnav_stream_cb) { NULL, NULL, NULL };"
    keep = "  else if(dvdroot)  /* a restart keeps the stream it was opened on */\n" \
           "      vm->streamcb = (dvdnav_stream_cb) { NULL, NULL, NULL };"
    if wipe in text:
        vm.write_text(text.replace(wipe, keep))
    elif keep not in text:
        raise SystemExit(f"libdvdnav {DVDNAV} no longer matches the stream-callback fix; recheck vm_reset")
    build = BUILD / "dvdnav-build"
    if not (build / "build.ninja").exists():
        run("meson", "setup", build, src, "--buildtype=release", f"--prefix={PREFIX}", "--libdir=lib")
    run("ninja", "-C", build, "install")


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
        # rpath: the libdvdnav beside it, for a build used in place (dev.py, LIBMPV_PATH, CI tests)
        rpath = [] if sys.platform == "win32" else [f"-Dc_link_args=-Wl,-rpath,{PREFIX / 'lib'}"]
        run("meson", "setup", MPV_BUILD, SRC, "--buildtype=release", f"--prefix={PREFIX}", "--libdir=lib", *rpath,
            "-Dlibmpv=true", "-Dcplayer=false", "-Ddvdnav=enabled",
            "-Dlibbluray=enabled", "-Dlibcurl=enabled", "-Dlua=disabled", "-Djavascript=disabled", "-Dvapoursynth=disabled",
            "-Dlibarchive=disabled", "-Drubberband=disabled", "-Dmanpage-build=disabled")
    run("ninja", "-C", MPV_BUILD)
    run("ninja", "-C", MPV_BUILD, "install")


def bdj_jar():
    """libbluray wants a jar built from its own version; Homebrew installs none at all."""
    version = subprocess.run(["pkg-config", "--modversion", "libbluray"],
                             capture_output=True, text=True, check=True).stdout.strip()
    src = fetch("libbluray", version)
    dist = BUILD / "libbluray"
    shutil.rmtree(dist, ignore_errors=True)
    # A file, not -D arguments: Windows' ant.bat splits "-Dsrc_awt=:java-j2se:..." into a target
    props = {"build": (BUILD / "bdj-build").as_posix(), "dist": dist.as_posix(), "bootclasspath": "",
             "src_awt": ":java-j2se:java-build-support", "javac_path": Path(shutil.which("javac")).as_posix(),
             "javac_arg": "-Xlint:-deprecation", "version": f"j2se-{version}",
             "java_version_asm": "1.8", "java_version_bdj": "1.8"}  # JDK 17 refuses target 7
    props_file = BUILD / "bdj.properties"
    props_file.write_text("".join(f"{k}={v}\n" for k, v in props.items()))
    run("ant", "-f", src / "src/libbluray/bdj/build.xml", "-propertyfile", props_file)
    print(f"jar for libbluray {version} -> {dist}", flush=True)


def main():
    BUILD.mkdir(exist_ok=True)
    if sys.platform == "darwin":
        os.environ.setdefault("PKG_CONFIG_PATH", "/opt/homebrew/lib/pkgconfig")
    os.environ["PKG_CONFIG_PATH"] = os.pathsep.join(filter(None, (str(PREFIX / "lib/pkgconfig"),
                                                                  os.environ.get("PKG_CONFIG_PATH"))))
    dvdnav()
    mpv()
    bdj_jar()


if __name__ == "__main__":
    main()
