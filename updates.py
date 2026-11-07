"""GitHub release updates, verified staging, and a detached transactional installer.

The staged app runs --apply-update before importing Qt or VLC. It waits for the
old app to exit, swaps the installation, and restores it if the new app cannot
finish starting. No shell, elevated permissions, or external installer is used.
"""
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.request import Request, urlopen

REPO = "hackerslash/isoplay"
LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"


class UpdateError(Exception):
    pass


class UpdateCancelled(UpdateError):
    pass


def version(value):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", value)
    if not match:
        raise UpdateError("Unsupported release version")
    return tuple(map(int, match.groups()))


def asset_name(system=None, machine=None):
    system, machine = system or sys.platform, (machine or platform.machine()).lower()
    if system == "darwin" and machine in ("arm64", "aarch64"):
        return "isoplay-macos-arm64.zip"
    if system == "win32" and machine in ("amd64", "x86_64"):
        return "isoplay-windows-amd64.zip"
    if system.startswith("linux") and machine in ("amd64", "x86_64"):
        return "isoplay-linux-x86_64.tar.gz"
    raise UpdateError("There is no automatic update for this operating system and processor")


@dataclass(frozen=True)
class Release:
    tag: str
    notes: str
    url: str
    asset: str
    download: str
    size: int
    digest: str


def check_for_update(current, system=None, machine=None):
    request = Request(LATEST, headers={"User-Agent": "isoplay-updater", "Accept": "application/vnd.github+json"})
    with urlopen(request, timeout=20) as response:
        data = json.loads(response.read(2 << 20))
    if data.get("draft") or data.get("prerelease") or version(data["tag_name"]) <= version(current):
        return None
    name = asset_name(system, machine)
    for asset in data.get("assets", []):
        if asset["name"] == name:
            download = f"https://github.com/{REPO}/releases/download/{data['tag_name']}/{name}"
            digest = asset.get("digest") or ""
            if asset["browser_download_url"] != download or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise UpdateError("The release download could not be verified")
            size = asset["size"]
            if not isinstance(size, int) or not 0 < size <= 2 << 30:
                raise UpdateError("Invalid release download size")
            return Release(data["tag_name"], data.get("body") or "", data["html_url"], name,
                           download, size, digest.split(":", 1)[1])
    raise UpdateError("The new version is still being built for this platform. Please try again later.")


def installation_target(executable=None, system=None):
    executable = Path(executable or sys.executable).resolve()
    system = system or sys.platform
    if system == "darwin":
        target = next((p for p in executable.parents if p.suffix == ".app"), None)
        if target is None or not (target / "Contents/MacOS/isoplay").is_file():
            raise UpdateError("Open the installed IsoPlay app to use automatic updates")
        if target.parts[1:2] == ("Volumes",):
            raise UpdateError("Move IsoPlay to Applications before installing an update")
    else:
        target = executable.parent
        if not (target / "_internal").is_dir() or executable.name not in ("isoplay", "isoplay.exe"):
            raise UpdateError("Open the installed IsoPlay app to use automatic updates")
    if not os.access(target.parent, os.W_OK):
        raise UpdateError("This app folder is read-only. Download the update and install it in a folder you can write to.")
    return target


def _executable(root, system):
    return root / ("Contents/MacOS/isoplay" if system == "darwin" else "isoplay.exe" if system == "win32" else "isoplay")


def _extract_zip(archive, destination, root):
    links = []
    with zipfile.ZipFile(archive) as bundle:
        if sum(entry.file_size for entry in bundle.infolist()) > 4 << 30:
            raise UpdateError("The update archive is too large")
        for entry in bundle.infolist():
            path = PurePosixPath(entry.filename)
            if path.parts and path.parts[0] == "__MACOSX":
                continue  # AppleDouble metadata is not executable bundle content.
            if path.is_absolute() or ".." in path.parts or "\\" in entry.filename or not path.parts or path.parts[0] != root:
                raise UpdateError("Unsafe path in update archive")
            target = destination.joinpath(*path.parts)
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode):
                links.append((target, bundle.read(entry).decode("utf-8")))
            elif entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(entry) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                if mode & 0o777:
                    target.chmod(mode & 0o777)
        # Extract regular files before links so writes never traverse a link.
        for target, link in links:
            if Path(link).is_absolute() or not (target.parent / link).resolve().is_relative_to(destination / root):
                raise UpdateError("Unsafe link in update archive")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(link)
        for target, _ in links:
            if not target.resolve().is_relative_to(destination / root):
                raise UpdateError("Unsafe link in update archive")


def prepare_update(release, target, progress=None, cancel=None, system=None, resume_args=()):
    """Download and stage without changing the running installation."""
    system = system or sys.platform
    cancel = cancel or threading.Event()
    target = Path(target).resolve()
    work = Path(tempfile.mkdtemp(prefix="isoplay-update-")).resolve()
    try:
        archive = work / release.asset
        digest, received = hashlib.sha256(), 0
        request = Request(release.download, headers={"User-Agent": "isoplay-updater"})
        with urlopen(request, timeout=30) as response, archive.open("wb") as output:
            while True:
                if cancel.is_set():
                    raise UpdateCancelled("Update cancelled")
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                received += len(chunk)
                if received > release.size:
                    raise UpdateError("The update download has an unexpected size")
                digest.update(chunk)
                output.write(chunk)
                if progress:
                    progress(min(99, received * 100 // release.size))
        if received != release.size or digest.hexdigest() != release.digest:
            raise UpdateError("The update download failed its integrity check. Your app was not changed.")
        if cancel.is_set():
            raise UpdateCancelled("Update cancelled")
        extracted = work / "extracted"
        extracted.mkdir()
        root = "isoplay.app" if system == "darwin" else "isoplay"
        if release.asset.endswith(".zip"):
            _extract_zip(archive, extracted, root)
        else:
            with tarfile.open(archive) as bundle:
                members = bundle.getmembers()
                if sum(member.size for member in members) > 4 << 30:
                    raise UpdateError("The update archive is too large")
                if any(not PurePosixPath(m.name).parts or PurePosixPath(m.name).parts[0] != root for m in members):
                    raise UpdateError("Unexpected files in update archive")
                bundle.extractall(extracted, filter="data")
        stage = extracted / root
        executable = _executable(stage, system)
        if not executable.is_file() or (system != "win32" and not os.access(executable, os.X_OK)):
            raise UpdateError("The update does not contain a runnable IsoPlay app")
        archive.unlink()
        manifest = work / "update.json"
        manifest.write_text(json.dumps({"target": str(target), "stage": str(stage), "system": system,
                                       "tag": release.tag, "parent_pid": os.getpid(), "resume_args": list(resume_args),
                                       "backup": str(target.parent / (".isoplay-backup-" + work.name)),
                                       "replacement": str(target.parent / (".isoplay-new-" + work.name))}))
        return manifest
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise


def _manifest(path):
    path = Path(path).resolve()
    data = json.loads(path.read_text())
    work = path.parent
    target, stage = Path(data["target"]), Path(data["stage"])
    if (not work.name.startswith("isoplay-update-") or path.name != "update.json"
            or stage.parent != work / "extracted" or target == target.parent
            or Path(data["backup"]) != target.parent / (".isoplay-backup-" + work.name)
            or Path(data["replacement"]) != target.parent / (".isoplay-new-" + work.name)):
        raise UpdateError("Invalid update installation plan")
    return path, data


def _environment():
    env = os.environ.copy()
    if "LD_LIBRARY_PATH_ORIG" in env:
        env["LD_LIBRARY_PATH"] = env.pop("LD_LIBRARY_PATH_ORIG")
    else:
        env.pop("LD_LIBRARY_PATH", None)
    for key in list(env):
        if key.startswith("_PYI_") or key in ("PYTHON_VLC_LIB_PATH", "PYTHON_VLC_MODULE_PATH", "VLC_PLUGIN_PATH",
                                            "LIBBLURAY_CP", "JAVA_HOME"):
            env.pop(key, None)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def _launch(args, work):
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS if sys.platform == "win32" else 0
    # PyInstaller changes the process-wide Windows DLL search directory. A new
    # app must load its own DLLs, rather than the running version's copies.
    kernel, previous = None, None
    if sys.platform == "win32":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
        length = kernel.GetDllDirectoryW(0, None)
        buffer = ctypes.create_unicode_buffer(length + 1)
        kernel.GetDllDirectoryW(len(buffer), buffer)
        previous = buffer.value or None
        kernel.SetDllDirectoryW(None)
    try:
        # The relaunched GUI must not retain a log handle or cwd inside work:
        # Windows would then prevent cleanup for the lifetime of the app.
        log_path = work / "installer.log" if "--apply-update" in args else Path(os.devnull)
        with log_path.open("ab") as log:
            return subprocess.Popen([str(arg) for arg in args], cwd=str(Path(args[0]).parent), env=_environment(),
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                    creationflags=flags, start_new_session=sys.platform != "win32")
    finally:
        if kernel:
            kernel.SetDllDirectoryW(previous)


def launch_installer(manifest, cancel=None):
    manifest, data = _manifest(manifest)
    work = manifest.parent
    process = _launch([_executable(Path(data["stage"]), data["system"]), "--apply-update", manifest], work)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if cancel and cancel.is_set():
            process.terminate()
            process.wait(timeout=10)
            raise UpdateCancelled("Update cancelled")
        if (work / "ready").exists():
            return process
        if process.poll() is not None:
            break
        time.sleep(0.1)
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=10)
    raise UpdateError("The update could not start. Your current app is unchanged.")


def _wait_for_exit(pid, timeout=90):
    if not isinstance(pid, int) or pid < 1 or pid == os.getpid():
        raise UpdateError("Invalid app process")
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            return
        try:
            if kernel.WaitForSingleObject(handle, int(timeout * 1000)) != 0:
                raise UpdateError("IsoPlay did not close in time")
        finally:
            kernel.CloseHandle(handle)
    else:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        raise UpdateError("IsoPlay did not close in time")


def _wait_for_boot(process, work):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if (work / "boot-ok").exists():
            return True
        if process.poll() is not None:
            return False
        time.sleep(0.1)
    return False


def apply_update(manifest):
    """Detached helper entry point. Never removes the previous app before backup."""
    manifest, data = _manifest(manifest)
    work = manifest.parent
    target, stage, backup, replacement = (Path(data[key]) for key in ("target", "stage", "backup", "replacement"))
    (work / "ready").touch()
    parent_closed, moved_old, moved_new, process = False, False, False, None
    try:
        _wait_for_exit(data["parent_pid"])
        parent_closed = True
        shutil.copytree(stage, replacement, symlinks=True)
        os.replace(target, backup)
        moved_old = True
        os.replace(replacement, target)
        moved_new = True
        args = [_executable(target, data["system"]), *data["resume_args"], "--update-result", manifest]
        process = _launch(args, work)
        if not _wait_for_boot(process, work):
            raise UpdateError("The updated app could not start")
        (work / "result.json").write_text(json.dumps({"success": True, "tag": data["tag"]}))
        return 0
    except Exception as error:
        if process and process.poll() is None:
            process.terminate()
            process.wait(timeout=15)
        if moved_new:
            shutil.rmtree(target)
        if moved_old:
            os.replace(backup, target)
        shutil.rmtree(replacement, ignore_errors=True)
        result = {"success": False, "error": str(error)}
        (work / "result.json").write_text(json.dumps(result))
        if parent_closed:
            _launch([_executable(target, data["system"]), *data["resume_args"], "--update-result", manifest], work)
        return 1


def finish_update(manifest, executable=None):
    """Called after the relaunched app has initialized its UI and playback engine."""
    manifest, data = _manifest(manifest)
    if installation_target(executable, data["system"]) != Path(data["target"]):
        raise UpdateError("The update result belongs to a different app")
    work = manifest.parent
    (work / "boot-ok").touch()
    deadline = time.monotonic() + 30
    while not (work / "result.json").exists():
        if time.monotonic() >= deadline:
            raise UpdateError("The update has not finished. The previous app backup has been kept.")
        time.sleep(0.1)
    result = json.loads((work / "result.json").read_text())
    if result["success"]:
        shutil.rmtree(data["backup"], ignore_errors=True)
    # The helper runs from this directory on Windows, so let it release its DLLs.
    for _ in range(50):
        try:
            shutil.rmtree(work)
            break
        except OSError:
            time.sleep(0.1)
    return result
