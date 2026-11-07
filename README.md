<p align="center">
  <img src="assets/icon.svg" width="96" alt="isoplay icon">
</p>

<h1 align="center">isoplay</h1>

<p align="center">
  Play Blu-ray and DVD ISOs with their disc menus, from a file or straight from an HTTP link.<br>
  One self-contained app for macOS, Windows and Linux. Nothing else to install.
</p>

<p align="center">
  <a href="../../releases/latest"><b>Download</b></a>
</p>

![isoplay playing Sintel](docs/screenshot.png)

## Features

- **Disc menus**, including Java (BD-J) menus, for DVD5/DVD9, BD25/BD50 and UHD BD66/BD100.
- **Streams remote images** through a local block cache with parallel read-ahead, so menus stay
  responsive on slow hosts. Links don't need `.iso` in them; the disc type comes from the contents.
- **Plays ordinary video** too, local or remote.
- **Handles ISO9660 Blu-ray containers over HTTP**, including images with multiple disc folders.
  Starts with the disc with the most video data and streams it with menus, without extracting the image.
  Use **Discs in This Image** in the track menu to switch to a bonus or special-features disc.
- **Everything bundled:** libVLC with libbluray and libdvdnav, plus a trimmed Java 17 runtime.

## Install

| OS | File | First launch |
|---|---|---|
| macOS (Apple silicon) | `isoplay-macos-arm64.zip` | Unsigned: right-click the app, then Open |
| Windows x64 | `isoplay-windows-amd64.zip` | SmartScreen: More info, then Run anyway |
| Linux x64 | `isoplay-linux-x86_64.tar.gz` | Ubuntu 24.04+ or equivalent, X11 or XWayland |

## Use

Drop a file or link on the window, press ⌘V / Ctrl+V with a link copied, use ⌘O / ⌘L, or run
`isoplay <file-or-url>`. Recent items wait on the empty window.

| Key | Action |
|---|---|
| Arrows, Return | navigate disc menus (clicks work too) |
| Space | play or pause |
| M | disc popup menu |
| ← → (Shift on discs) | seek 10 s |
| ↑ ↓ on videos | volume |
| Page Up / Down | previous or next chapter |
| F | full screen |

If a disc menu fails, choose **Play Main Title Without Menus** from the track button.

## Limits

- Images must already be decrypted (no AACS, so no retail UHD as-is).
- The host has to support HTTP `Range` requests.
- macOS builds are Apple silicon only and not notarized.

## Build

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python build.py    # needs a JDK 17+; output in dist/
```

Linux builds need the distro VLC (`libvlc5 vlc-plugin-base vlc-plugin-video-output libbluray-bdj`).
Pushing a `v*` tag builds every platform and publishes a release. `ISOPLAY_DEBUG=1` prints the
libVLC log.

## License

MIT. Bundles include VLC (LGPL/GPL) and OpenJDK (GPLv2 with Classpath Exception). Screenshot
from [Sintel](https://durian.blender.org/) © Blender Foundation, CC BY 3.0.
