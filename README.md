<p align="center">
  <img src="isoplay/assets/icon.svg" width="96" alt="isoplay icon">
</p>

<h1 align="center">isoplay</h1>

<p align="center">
  Stream Blu-ray and DVD ISOs straight from an HTTP link, with their disc menus.<br>
  Nothing to download first, nothing to extract. Local files work too.
</p>

<p align="center">
  <a href="../../releases/latest"><b>Download</b></a>
</p>

![isoplay playing a Blu-ray disc menu](docs/screenshot.png)

## Streaming

Paste a link and the disc opens where you left the index — isoplay fetches only the blocks
libbluray and libdvdnav actually read, through a local cache with parallel read-ahead. A 50 GB
Blu-ray starts in seconds and never lands on disk.

- **Any direct link.** No `.iso` needed in the URL. One ranged read decides what the link is
  from its first bytes, so extensionless download links work.
- **Zipped discs**, without unpacking. A disc stored uncompressed in a `.zip` is read in place,
  wherever its `VIDEO_TS` or `BDMV` folder sits inside the archive — including a whole `.iso`
  stored inside it, which is opened in turn with its menus intact.
- **ISO9660 containers**, which libbluray can't read, are republished as UDF on the fly.
- **Everything else playable.** HD DVD, Video CD, DVD-Audio and plain rips of media files have no
  menu format libVLC can navigate, so their main title is served as itself — numbered `.EVO`,
  `.VOB` and `.DAT` parts joined back into one stream.
- **WebDAV shares**, listed with PROPFIND and projected the same way: a `BDMV` or `VIDEO_TS`
  folder on a share streams with its menus, nothing downloaded and nothing mounted.
- **Multi-disc images** open a chooser; switch later from **Discs in This Image** in the track menu.

The host must support HTTP `Range` requests. A protected host works through a link that
carries its credentials, as `https://user:pass@host/disc.iso`. Images must already be decrypted (no AACS, so no
retail UHD as-is).

Menus come from the engine that can navigate the format: libdvdnav for DVD, libbluray for
Blu-ray. HD DVD Advanced Content and DVD-Audio have no open-source navigator in existence, so
those play their main title without menus.

## Also plays

Local ISOs and `.zip` discs, `VIDEO_TS` and `BDMV` folders, and ordinary video files and links.
Disc menus include Java (BD-J) menus, for DVD5/DVD9, BD25/BD50 and UHD BD66/BD100. libVLC with
libbluray and libdvdnav plus a trimmed Java 17 runtime are bundled, so there is nothing else to
install.

## Install

| OS | File | First launch |
|---|---|---|
| macOS (Apple silicon) | `isoplay-macos-arm64.zip` | Unsigned: right-click the app, then Open |
| Windows x64 | `isoplay-windows-amd64.zip` | SmartScreen: More info, then Run anyway |
| Linux x64 | `isoplay-linux-x86_64.tar.gz` | Ubuntu 24.04+ or equivalent, X11 or XWayland |

The app checks for new releases on startup and can install them itself, keeping the old version
until the new one starts successfully. Turn that off in **Options** on the welcome screen.

## Use

Paste a link with ⌘V / Ctrl+V, press ⌘L / Ctrl+L to type one, drop a file or link on the window,
or run `isoplay <file-or-url>`.

| Key | Action |
|---|---|
| Arrows, Return | navigate disc menus (clicks work too) |
| Space | play or pause |
| M | disc popup menu |
| ← → (Shift on discs) | seek 10 s |
| ↑ ↓ on videos | volume |
| Page Up / Down | previous or next chapter |
| F | full screen |

The track button holds audio and subtitle tracks, **Add Subtitle File…**, the fit modes
(fit, fill, stretch) and the disc options. If a disc menu fails, choose
**Play Main Title Without Menus**.

## Run from source

VLC must be installed on the system, since libVLC is not a Python package. With a JDK 17+ present,
`dev.py` also builds the same Java runtime the bundle ships, so BD-J menus behave as they will there.

```sh
python3 dev.py                  # creates .venv, installs deps, starts the app
python3 dev.py path/or/url      # start on a file or link
python3 dev.py --tests          # run the test suite
```

Nothing is installed outside `.venv` and no bundle is built.

```
isoplay/        media.py streams and probes links, webdav.py lists a share, disc_image.py
                projects ISO9660, zip and remote discs as UDF or lifts a single title out,
                app.py is the window, widgets.py and theme.py the interface
tests/          unittest suite; test_isoplay.py covers the media layer with no Qt
build.py        produces the self-contained bundle in dist/
```

## Build

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-build.txt
.venv/bin/python build.py    # needs a JDK 17+; output in dist/
```

Linux builds need the distro VLC (`libvlc5 vlc-plugin-base vlc-plugin-video-output libbluray-bdj`).
Pushing a `v*` tag builds every platform and publishes a release. `ISOPLAY_DEBUG=1` prints the
libVLC log.

## License

MIT. Bundles include VLC (LGPL/GPL) and OpenJDK (GPLv2 with Classpath Exception).
