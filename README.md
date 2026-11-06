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

The empty window lists recent discs and videos; click one to play it again. Open something new
with **Open File… (⌘O / Ctrl+O)** or **Open Location… (⌘L / Ctrl+L)**, drop a file or link on the
window, press **⌘V / Ctrl+V** with a link copied, or run `isoplay <file-or-url>`.

While playing, the window is all picture. Move the mouse and a small control panel fades in at
the bottom; it fades out, along with the cursor, after two seconds of stillness. Status messages
(loading, volume, chapter, track changes) appear briefly in the top-left corner.

The disc type comes from the file's contents, so links without `.iso` in them, `.img` images
and `BDMV`/`VIDEO_TS` folders all work: DVD5/DVD9, BD25/BD50, and UHD BD66/BD100.

| Key | Action |
|---|---|
| ↑ ↓ ← → Return | move and select in disc menus (clicking works too) |
| M | disc popup menu |
| Space | play/pause |
| ← → on videos, Shift+← → on discs | seek 10 s |
| ↑ ↓ on videos | volume |
| Page Up / Page Down | previous or next chapter |
| F, ⌃⌘F / F11 | full screen (Esc leaves) |
| ⌘W / Ctrl+W | close the video and return to Recent |

The control panel's track button lists audio and subtitle tracks; the time on the right switches
between remaining and total. **Start discs at their menu** (on the empty window and in the
Playback menu) decides how discs open. When a disc's menu fails, **Playback → Play Main Title
Without Menus** goes straight to the movie.

## How it works

- **Disc detection.** Anything that isn't a known video extension is checked for an ISO 9660 or
  UDF volume descriptor at sector 16, then the first 4 MiB are searched for `BDMV` or `VIDEO_TS`.
  DVDs are forced onto VLC's `dvdnav` demuxer, because over HTTP VLC otherwise plays the raw
  MPEG-PS stream and the menus are lost. Blu-rays are picked up by libbluray on their own.
- **Range cache.** A remote ISO is served to libVLC from a local HTTP endpoint backed by a 4 MiB
  block cache. Menus seek constantly, and each upstream request can cost a second. During playback,
  several blocks are fetched in parallel because one connection to a slow host can't keep up with
  Blu-ray bitrates. The host must support `Range` requests.
- **BD-J.** BD-J discs don't need a specific Java version. libbluray 1.4 supports Java 8, 11 and 17
  (newer releases dropped the SecurityManager it relies on), so every build bundles Java 17 and
  never uses a system Java. `JAVA_HOME` points at the bundled jlink JRE (`java.base`, `java.desktop`, `java.rmi`),
  and `LIBBLURAY_CP` points at the libbluray jar shipped with that platform's VLC.

## Limits

- AACS-encrypted discs (including every retail UHD, AACS 2) won't play; the image has to be
  decrypted already.
- Some BD-J menus still fail in libbluray. Use **Play Main Title Without Menus** to go straight
  to the movie.
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
