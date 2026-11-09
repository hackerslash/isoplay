#!/usr/bin/env python3
"""Build a self-contained isoplay bundle for the current OS into dist/.

Needs: Python with requirements.txt installed, a JDK 17+ (JAVA_HOME or on PATH) for jlink and
what tools/build_mpv.py needs to build the patched libmpv (see .github/workflows/build.yml).
"""
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILD, DIST = ROOT / "build", ROOT / "dist"
MPV = BUILD / "mpv"  # tools/build_mpv.py installs here
STAGE = BUILD / "mpv-stage"
JRE_MODULES = "java.base,java.desktop,java.rmi"  # jdeps on the libbluray jars
DEJAVU_URL = ("https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/"
              "dejavu-fonts-ttf-2.37.tar.bz2")
# libbluray maps BD-J's default faces to the Lucida files the JDK stopped shipping in 11, and has no
# fontconfig on macOS or Windows to fall back on, so a menu that draws text dies on a black screen.
LUCIDA = {"LucidaSansRegular": "DejaVuSans", "LucidaSansDemiBold": "DejaVuSans-Bold",
          "LucidaSansOblique": "DejaVuSans-Oblique", "LucidaSansDemiOblique": "DejaVuSans-BoldOblique",
          "LucidaBrightRegular": "DejaVuSerif", "LucidaBrightDemiBold": "DejaVuSerif-Bold",
          "LucidaBrightItalic": "DejaVuSerif-Italic", "LucidaBrightDemiItalic": "DejaVuSerif-BoldItalic",
          "LucidaTypewriterRegular": "DejaVuSansMono", "LucidaTypewriterBold": "DejaVuSansMono-Bold",
          "LucidaTypewriterOblique": "DejaVuSansMono-Oblique",
          "LucidaTypewriterBoldOblique": "DejaVuSansMono-BoldOblique"}
# Qt the player never loads, ~20 MB pulled in by the PDF image and virtual keyboard plugins.
# The names match on every platform, give or take a lib prefix, a 6 and a framework.
QT_UNUSED = re.compile(r"(?i)^(lib)?qt6?(pdf|quick|qml|virtualkeyboard)|^(lib)?q(pdf|tvirtualkeyboardplugin)\.")

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


def libmpv():
    run(sys.executable, ROOT / "tools/build_mpv.py")
    STAGE.mkdir(parents=True)
    for jar in (BUILD / "libbluray").glob("*.jar"):
        shutil.copy2(jar, STAGE)
    {"darwin": mpv_macos, "win32": mpv_windows}.get(sys.platform, mpv_linux)()


def mpv_macos():
    """libmpv and every non-system dylib under it, relinked to load from beside each other."""
    queue, seen = [str(MPV / "lib/libmpv.2.dylib")], set()
    while queue:
        src = Path(queue.pop())
        if src.name in seen:
            continue
        seen.add(src.name)
        dst = STAGE / src.name
        shutil.copy2(src.resolve(), dst)
        dst.chmod(0o755)
        changes = ["-id", f"@loader_path/{src.name}"]
        for line in subprocess.check_output(["otool", "-L", dst], text=True).splitlines()[1:]:
            dep = line.strip().split(" (")[0]
            if dep.startswith(("/usr/lib/", "/System/")) or Path(dep).name == src.name:
                continue
            for prefix in ("@rpath/", "@loader_path/"):
                if dep.startswith(prefix):
                    found = [d / dep[len(prefix):] for d in (src.resolve().parent, Path("/opt/homebrew/lib"))]
                    dep = str(next(f for f in found if f.exists()))
            changes += ["-change", line.strip().split(" (")[0], f"@loader_path/{Path(dep).name}"]
            queue.append(dep)
        subprocess.run(["install_name_tool", *changes, dst], check=True, capture_output=True)
    print(f"bundled {len(seen)} dylibs", flush=True)


def mpv_windows():
    """libmpv-2.dll and the MinGW DLLs it imports, from the toolchain that built it."""
    bindir = Path(shutil.which("gcc")).parent
    queue, seen = [MPV / "bin/libmpv-2.dll"], set()
    while queue:
        src = queue.pop()
        if src.name.lower() in seen:
            continue
        seen.add(src.name.lower())
        shutil.copy2(src, STAGE / src.name)
        for line in subprocess.check_output(["objdump", "-p", src], text=True).splitlines():
            name = line.split(":", 1)[1].strip() if "DLL Name:" in line else None
            found = next((d / name for d in (MPV / "bin", bindir) if name and (d / name).is_file()), None)
            if found:
                queue.append(found)
    print(f"bundled {len(seen)} DLLs", flush=True)


def mpv_linux():
    shutil.copy2((MPV / "lib/libmpv.so.2").resolve(), STAGE / "libmpv.so.2")


def linux_deps(dest):
    """Copy shared-library dependencies of the staged libmpv into dest (PyInstaller adds it to LD_LIBRARY_PATH)."""
    seen = set()
    for so in [*STAGE.rglob("*.so*")]:
        out = subprocess.run(["ldd", so], capture_output=True, text=True).stdout
        for line in out.splitlines():
            parts = line.split("=>")
            if len(parts) != 2 or not parts[1].strip().startswith("/"):
                continue
            path = parts[1].split("(")[0].strip()
            name = Path(path).name
            if name in seen or name.startswith(LINUX_SKIP) or name.startswith("libmpv."):
                continue
            seen.add(name)
            shutil.copy2(path, dest / name)
    print(f"bundled {len(seen)} shared libraries", flush=True)


def jre():
    java_home = os.environ.get("JAVA_HOME")
    jlink = str(Path(java_home) / "bin" / "jlink") if java_home else "jlink"
    shutil.rmtree(BUILD / "jre", ignore_errors=True)
    run(jlink, "--add-modules", JRE_MODULES, "--strip-debug", "--no-man-pages", "--no-header-files",
        "--compress=2", "--output", BUILD / "jre")
    fonts = BUILD / "jre/lib/fonts"
    fonts.mkdir(exist_ok=True)
    with tarfile.open(download(DEJAVU_URL, BUILD / "dejavu.tar.bz2")) as tar:
        ttf = {Path(m.name).stem: m for m in tar.getmembers() if m.name.endswith(".ttf")}
        for lucida, dejavu in LUCIDA.items():
            (fonts / f"{lucida}.ttf").write_bytes(tar.extractfile(ttf[dejavu]).read())


def check(exe):
    print("+", exe, "--check", flush=True)
    subprocess.run([exe, "--check"], check=True, env={**os.environ, "ISOPLAY_CHECK_QT": "1"})


def prune_qt(root):
    """Delete QT_UNUSED from the frozen app, links and all, before anything is signed."""
    removed = 0
    for path in [p for p in root.rglob("*") if QT_UNUSED.match(p.name)]:
        if path.is_symlink() or path.is_file():
            removed += 0 if path.is_symlink() else path.stat().st_size
            path.unlink()
        elif path.is_dir():
            removed += sum(f.stat().st_size for f in path.rglob("*") if f.is_file() and not f.is_symlink())
            shutil.rmtree(path)
    print(f"pruned {removed >> 20} MiB of unused Qt", flush=True)


def icon_png():
    """Render assets/icon.svg to a 1024 px PNG; PyInstaller turns it into .icns/.ico via Pillow."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer
    app = QGuiApplication.instance() or QGuiApplication([])  # noqa: F841
    img = QImage(1024, 1024, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    QSvgRenderer(str(ROOT / "isoplay/assets/icon.svg")).render(p)
    p.end()
    out = BUILD / "icon.png"
    img.save(str(out))
    return out


def main():
    shutil.rmtree(DIST, ignore_errors=True)
    for d in (STAGE, BUILD / "pyinstaller"):
        shutil.rmtree(d, ignore_errors=True)
    BUILD.mkdir(exist_ok=True)
    libmpv()
    jre()

    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--windowed", "--name", "isoplay",
        "--icon", icon_png(), "--add-data", f"{ROOT / 'isoplay/assets'}{os.pathsep}assets",
        "--collect-data", "certifi",
        "--osx-bundle-identifier", "io.github.hackerslash.isoplay",
        "--workpath", BUILD / "pyinstaller", "--distpath", DIST, ROOT / "run.py")

    if sys.platform == "darwin":
        app = DIST / "isoplay.app"
        internal = app / "Contents/Frameworks"
    else:
        internal = DIST / "isoplay/_internal"
    prune_qt(app / "Contents" if sys.platform == "darwin" else internal)
    for name, src in (("mpv", STAGE), ("jre", BUILD / "jre")):
        if sys.platform == "darwin":  # codesign only accepts code in Frameworks; data lives in Resources
            shutil.copytree(src, app / "Contents/Resources" / name, symlinks=True)
            (internal / name).symlink_to(Path("../Resources") / name)
        else:
            shutil.copytree(src, internal / name, symlinks=True)

    if sys.platform not in ("darwin", "win32"):
        linux_deps(internal)
    shipped = app / "Contents/Resources" if sys.platform == "darwin" else DIST / "isoplay"
    bundle = app / "Contents" if sys.platform == "darwin" else shipped
    libs = sorted({f.name for f in bundle.rglob("*") if re.search(r"\.(dylib|dll|so)(\.|$)", f.name)})
    (shipped / "THIRD_PARTY_NOTICES.md").write_text((ROOT / "THIRD_PARTY_NOTICES.md").read_text()
                                                    + "\n## Native libraries in this bundle\n\n"
                                                    + "".join(f"- {name}\n" for name in libs))

    tag = f"{ {'darwin': 'macos', 'win32': 'windows'}.get(sys.platform, 'linux') }-{platform.machine().lower()}"
    if sys.platform == "darwin":
        # --deep rejects the JRE/libbluray data files; sign each Mach-O, then the bundle
        macho = (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe")
        for f in sorted((app / "Contents/Resources").rglob("*")):
            if f.is_file() and not f.is_symlink() and f.open("rb").read(4) in macho:
                subprocess.run(["codesign", "--force", "--sign", "-", f], check=True, capture_output=True)
        run("codesign", "--force", "--sign", "-", app)
        check(app / "Contents/MacOS/isoplay")
        run("ditto", "-c", "-k", "--keepParent", app, DIST / f"isoplay-{tag}.zip")
    elif sys.platform == "win32":
        check(DIST / "isoplay/isoplay.exe")
        shutil.make_archive(str(DIST / f"isoplay-{tag}"), "zip", DIST, "isoplay")
    else:
        check(DIST / "isoplay/isoplay")
        with tarfile.open(DIST / f"isoplay-{tag}.tar.gz", "w:gz") as t:
            t.add(DIST / "isoplay", "isoplay")
    print("built", *DIST.glob("isoplay-*"))


if __name__ == "__main__":
    main()
