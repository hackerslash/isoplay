#!/usr/bin/env python3
"""Build a self-contained isoplay bundle for the current OS into dist/.

Needs: Python with requirements.txt installed, a JDK 17+ (JAVA_HOME or on PATH) for jlink,
and on Linux the distro VLC packages (see .github/workflows/build.yml).
"""
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

VLC_VERSION = "3.0.24"
VLC_URL = "https://download.videolan.org/pub/videolan/vlc/{v}/{path}"
ROOT = Path(__file__).resolve().parent
BUILD, DIST = ROOT / "build", ROOT / "dist"
STAGE = BUILD / "vlc"
JRE_MODULES = "java.base,java.desktop,java.rmi"  # jdeps on the libbluray jars

# Libraries every Linux desktop already has, or that must match the host (GPU/audio/glibc).
LINUX_SKIP = ("linux-vdso", "ld-linux", "libc.so", "libm.so", "libdl.so", "libpthread", "librt.so",
              "libstdc++", "libgcc_s", "libGL", "libEGL", "libGLX", "libGLdispatch", "libdrm",
              "libX", "libxcb", "libwayland", "libasound", "libpulse", "libdbus", "libsystemd",
              "libudev", "libz.so", "libresolv", "libgbm", "libvulkan", "libva")


def run(*cmd):
    print("+", " ".join(map(str, cmd)), flush=True)
    subprocess.run(cmd, check=True)


def download(url, dest):
    if not dest.exists():
        print("downloading", url, flush=True)
        with urllib.request.urlopen(url) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
    return dest


def vlc_macos():
    arch = "arm64" if platform.machine() == "arm64" else "intel64"
    dmg = download(VLC_URL.format(v=VLC_VERSION, path=f"macosx/vlc-{VLC_VERSION}-{arch}.dmg"),
                   BUILD / f"vlc-{arch}.dmg")
    out = subprocess.check_output(["hdiutil", "attach", "-nobrowse", "-readonly", "-plist", dmg])
    mnt = Path(next(e["mount-point"] for e in plistlib.loads(out)["system-entities"] if "mount-point" in e))
    try:
        src = mnt / "VLC.app/Contents/MacOS"
        for d in ("lib", "plugins"):
            shutil.copytree(src / d, STAGE / d, symlinks=True)
    finally:
        run("hdiutil", "detach", mnt)
    for ui in ("libmacosx_plugin.dylib", "libosx_notifications_plugin.dylib"):  # VLC.app UI; need Sparkle/Growl
        (STAGE / "plugins" / ui).unlink()
    # VLC's own executable supplies @rpath; without it libvlc cannot find libvlccore
    run("install_name_tool", "-add_rpath", "@loader_path", STAGE / "lib/libvlc.5.dylib")


def vlc_windows():
    z = download(VLC_URL.format(v=VLC_VERSION, path=f"win64/vlc-{VLC_VERSION}-win64.zip"), BUILD / "vlc-win64.zip")
    top = f"vlc-{VLC_VERSION}/"
    with zipfile.ZipFile(z) as zf:
        for name in zf.namelist():
            rel = name[len(top):]
            if name.endswith("/") or not (rel in ("libvlc.dll", "libvlccore.dll") or rel.startswith("plugins/")):
                continue
            out = STAGE / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(zf.read(name))


def vlc_linux():
    libdir = Path(subprocess.check_output(["dpkg-architecture", "-qDEB_HOST_MULTIARCH"], text=True).strip())
    libdir = Path("/usr/lib") / libdir
    (STAGE / "lib").mkdir(parents=True)
    for pat in ("libvlc.so.5*", "libvlccore.so.9*"):
        for f in libdir.glob(pat):
            shutil.copy2(f, STAGE / "lib", follow_symlinks=False)
    shutil.copytree(libdir / "vlc/plugins", STAGE / "plugins", symlinks=False)
    for jar in Path("/usr/share/java").glob("libbluray*j2se-*.jar"):
        shutil.copy2(jar.resolve(), STAGE / "plugins" / jar.name)


def linux_deps(dest):
    """Copy shared-library dependencies of the staged libVLC into dest (PyInstaller adds it to LD_LIBRARY_PATH)."""
    seen = set()
    for so in [*STAGE.rglob("*.so*")]:
        out = subprocess.run(["ldd", so], capture_output=True, text=True).stdout
        for line in out.splitlines():
            parts = line.split("=>")
            if len(parts) != 2 or not parts[1].strip().startswith("/"):
                continue
            path = parts[1].split("(")[0].strip()
            name = Path(path).name
            if name in seen or name.startswith(LINUX_SKIP) or name.startswith(("libvlc.", "libvlccore.")):
                continue
            seen.add(name)
            shutil.copy2(path, dest / name)
    print(f"bundled {len(seen)} shared libraries", flush=True)


def jre():
    java_home = os.environ.get("JAVA_HOME")
    jlink = str(Path(java_home) / "bin" / "jlink") if java_home else "jlink"
    run(jlink, "--add-modules", JRE_MODULES, "--strip-debug", "--no-man-pages", "--no-header-files",
        "--compress=2", "--output", BUILD / "jre")


def main():
    shutil.rmtree(DIST, ignore_errors=True)
    for d in (STAGE, BUILD / "jre", BUILD / "pyinstaller"):
        shutil.rmtree(d, ignore_errors=True)
    BUILD.mkdir(exist_ok=True)
    {"darwin": vlc_macos, "win32": vlc_windows}.get(sys.platform, vlc_linux)()
    jre()

    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--windowed", "--name", "isoplay",
        "--workpath", BUILD / "pyinstaller", "--distpath", DIST, ROOT / "isoplay.py")

    if sys.platform == "darwin":
        app = DIST / "isoplay.app"
        internal = app / "Contents/Frameworks"
    else:
        internal = DIST / "isoplay/_internal"
    for name, src in (("vlc", STAGE), ("jre", BUILD / "jre")):
        if sys.platform == "darwin":  # codesign only accepts code in Frameworks; data lives in Resources
            shutil.copytree(src, app / "Contents/Resources" / name, symlinks=True)
            (internal / name).symlink_to(Path("../Resources") / name)
        else:
            shutil.copytree(src, internal / name, symlinks=True)

    tag = f"{ {'darwin': 'macos', 'win32': 'windows'}.get(sys.platform, 'linux') }-{platform.machine().lower()}"
    if sys.platform == "darwin":
        # --deep rejects the JRE/VLC data files; sign each Mach-O, then the bundle
        macho = (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe")
        for f in sorted((app / "Contents/Resources").rglob("*")):
            if f.is_file() and not f.is_symlink() and f.open("rb").read(4) in macho:
                subprocess.run(["codesign", "--force", "--sign", "-", f], check=True, capture_output=True)
        run("codesign", "--force", "--sign", "-", app)
        run(app / "Contents/MacOS/isoplay", "--check")
        run("ditto", "-c", "-k", "--keepParent", app, DIST / f"isoplay-{tag}.zip")
    elif sys.platform == "win32":
        run(DIST / "isoplay/isoplay.exe", "--check")
        shutil.make_archive(str(DIST / f"isoplay-{tag}"), "zip", DIST, "isoplay")
    else:
        linux_deps(internal)
        run(DIST / "isoplay/isoplay", "--check")
        with tarfile.open(DIST / f"isoplay-{tag}.tar.gz", "w:gz") as t:
            t.add(DIST / "isoplay", "isoplay")
    print("built", *DIST.glob("isoplay-*"))


if __name__ == "__main__":
    main()
