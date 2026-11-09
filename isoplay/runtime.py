"""Point libmpv, libbluray and OpenSSL at the bundled copies before anything loads them.

Importing this module has side effects and must happen before isoplay.mpv.binding() runs.
"""
import os
import sys
from pathlib import Path

BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
MPV_DIR = BASE / "mpv"
LIB = {"darwin": "libmpv.2.dylib", "win32": "libmpv-2.dll"}.get(sys.platform, "libmpv.so.2")
if (MPV_DIR / LIB).is_file():
    os.environ["LIBMPV_PATH"] = str(MPV_DIR / LIB)
    os.environ["LIBBLURAY_CP"] = MPV_DIR.as_posix() + "/"  # a directory: libbluray finds its jars by name
    if sys.platform == "win32":
        os.add_dll_directory(str(MPV_DIR))
if (BASE / "jre").is_dir():
    os.environ["JAVA_HOME"] = str(BASE / "jre")

# libcurl and urllib honour proxy variables; the loopback server must never go through one
os.environ["no_proxy"] = os.environ["NO_PROXY"] = ",".join(
    filter(None, (os.environ.get("NO_PROXY") or os.environ.get("no_proxy"), "127.0.0.1,localhost")))

import certifi  # noqa: E402
os.environ.setdefault("SSL_CERT_FILE", certifi.where())  # frozen builds inherit the build host's trust store
