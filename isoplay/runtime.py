"""Point libVLC, libbluray and OpenSSL at the bundled copies before anything loads them.

Importing this module has side effects and must happen before `import vlc`.
"""
import os
import sys
from pathlib import Path

BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
VLC_DIR = BASE / "vlc"
LIB = {"darwin": "lib/libvlc.dylib", "win32": "libvlc.dll"}.get(sys.platform, "lib/libvlc.so.5")
if VLC_DIR.is_dir():
    os.environ["PYTHON_VLC_LIB_PATH"] = str(VLC_DIR / LIB)
    os.environ["PYTHON_VLC_MODULE_PATH"] = os.environ["VLC_PLUGIN_PATH"] = str(VLC_DIR / "plugins")
    jars = sorted((VLC_DIR / "plugins").rglob("libbluray-j2se-*.jar"))
    if jars:
        os.environ["LIBBLURAY_CP"] = str(jars[-1])
    if sys.platform == "win32":
        os.add_dll_directory(str(VLC_DIR))
    elif sys.platform.startswith("linux"):  # libvlc's DT_NEEDED libvlccore is outside the loader path
        import ctypes
        ctypes.CDLL(str(VLC_DIR / "lib/libvlccore.so.9"), mode=ctypes.RTLD_GLOBAL)
if (BASE / "jre").is_dir():
    os.environ["JAVA_HOME"] = str(BASE / "jre")

import certifi  # noqa: E402
os.environ.setdefault("SSL_CERT_FILE", certifi.where())  # frozen builds inherit the build host's trust store

if sys.platform.startswith("linux"):
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")  # libVLC embeds into X11 windows only
