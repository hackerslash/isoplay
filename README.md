# isoplay

A desktop player for Blu-ray and DVD ISOs **with their disc menus**, from a local file or straight
from an HTTP(S) link. It plays ordinary video files and links too. One download per OS, with
everything inside: libVLC (libbluray, libdvdnav, all codecs) and a trimmed Java runtime for BD-J
menus. Nothing else to install.

## Get it

Download from [Releases](../../releases), or the latest build from [Actions](../../actions):

| OS | File | First launch |
|---|---|---|
| macOS (Apple silicon) | `isoplay-macos-arm64.zip` | unsigned: right-click → Open, or `xattr -dr com.apple.quarantine isoplay.app` |
| Windows x64 | `isoplay-windows-amd64.zip` | SmartScreen: More info → Run anyway |
| Linux x64 | `isoplay-linux-x86_64.tar.gz` | needs glibc ≥ 2.39 (Ubuntu 24.04+), X11 or XWayland, `libxcb-cursor0` |

## Use

Paste a path or URL and press Enter, use **Open…**, drop a file on the window, or pass it on the
command line: `isoplay <file-or-url>`.

| Key | Action |
|---|---|
| ↑ ↓ ← → Enter | move/select in disc menus; mouse clicks work too |
| M | disc popup / menu |
| Space | pause |
| ← → (normal files), Shift+← → (discs) | seek 10 s |
| F, double-click | fullscreen (Esc leaves) |

## How it works

- **Disc detection.** For `.iso`, the first MiB is read and checked for `BDMV` or `VIDEO_TS`.
  DVDs are forced onto VLC's `dvdnav` demuxer, because over HTTP VLC otherwise plays the raw
  MPEG-PS stream and the menus are lost. Blu-rays are picked up by libbluray on their own.
- **Range cache.** A remote ISO is served to libVLC from a local HTTP endpoint backed by a 4 MiB
  block cache. Menus seek constantly, and each upstream request can cost a second. During playback,
  several blocks are fetched in parallel because one connection to a slow host can't keep up with
  Blu-ray bitrates. The host must support `Range` requests.
- **BD-J.** `JAVA_HOME` points at the bundled jlink JRE (`java.base`, `java.desktop`, `java.rmi`),
  and `LIBBLURAY_CP` points at the libbluray jar shipped with that platform's VLC.

## Limits

- AACS-encrypted discs won't play; the image has to be decrypted already.
- Some BD-J menus still fail in libbluray. Untick **Disc menus** and press Play to go straight to
  the main title.
- The first BD-J menu takes a while over a slow link.
- macOS builds are arm64 only and not notarized.

## Build

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python build.py          # needs a JDK 17+ for jlink; output in dist/
```

On Linux, install the distro VLC first (`libvlc5 vlc-plugin-base vlc-plugin-video-output
libbluray-bdj`); see `.github/workflows/build.yml`. Pushing a `v*` tag publishes a release.

`ISOPLAY_DEBUG=1 isoplay <url>` prints the full libVLC log.

## License

The code is MIT. The bundles include VLC (LGPL/GPL) and OpenJDK (GPLv2 + Classpath Exception).
