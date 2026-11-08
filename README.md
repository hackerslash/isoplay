<p align="center">
  <img src="isoplay/assets/icon.svg" width="96" alt="isoplay icon">
</p>

<h1 align="center">isoplay</h1>

<p align="center">
  Play Blu-ray and DVD discs from a link with their menus, including discs in WebDAV folders and
  zips that VLC can't open.
</p>

<p align="center">
  <a href="../../releases/latest"><b>Download</b></a>
</p>

![isoplay playing a Blu-ray disc menu](docs/screenshot.png)

## Why this exists

Full disc rips are big. A Blu-ray image is 25 to 100 GB, and more and more of them sit on remote
storage, a debrid service, a WebDAV share or a plain download link, rather than on a local disk.
Downloading the whole image to watch it, and extracting it first when it comes zipped, takes
longer than the film.

Playing straight from the link avoids that, but only if the player can open what the link points
at. Many of these links point at things a player can't open: a disc folder on a share, a disc
inside a zip, an image libbluray can't read. isoplay exists to play those discs the way they
were authored, menus included, from wherever they are stored, with nothing downloaded or mounted
first.

## Why not just VLC?

VLC already plays an ISO from a link, and isoplay is libVLC underneath, with the same libbluray
and libdvdnav. If VLC plays your disc smoothly, keep using it.

Where isoplay differs:

**How it reads.** VLC reads a network disc over one connection, one request at a time. isoplay
fetches several ranges in parallel and reads ahead of the player, which helps on hosts that
limit each connection. It works out how many connections a host tolerates as it goes, and backs
off when the host answers 429 or refuses connections. It can't add bandwidth the host or your
line doesn't have: a disc whose bitrate is above that will buffer here as it would in VLC.

**Disc menus over a network.** libbluray and libdvdnav jump around the image constantly while
a disc opens and its menus run, and over plain HTTP every jump is another round trip. isoplay
keeps those blocks in memory, fetches a disc's small files in parallel, and keeps them in a
disk cache, so a menu that took 6 s to load the first time loads instantly the next.

**Sources VLC can't open from a link at all:**

- A disc folder on a **WebDAV share**. VLC opens single files, not a `BDMV` or `VIDEO_TS` tree;
  isoplay lists the share and presents the folder as a disc, nothing downloaded or mounted.
- A disc inside a **`.zip`**, stored uncompressed, wherever its disc folder sits in the archive,
  and an **`.iso` inside a `.zip`**. A 1000-file season zip opens in about 7 s.
- **ISO9660-only Blu-ray images**, which libbluray can't read. isoplay re-presents them as UDF on
  the fly.
- **HD DVD, Video CD, DVD-Audio** and plain rips of media files, which have no menus libVLC can
  run. isoplay serves the main title, with numbered `.EVO`, `.VOB` and `.DAT` parts joined back
  into one stream.
- **Images holding several discs** open a chooser first.

**Fixes for menus that don't run.** Java (BD-J) menus that draw text need fonts the JDK stopped
shipping, so isoplay bundles a Java runtime that has them. Some studios' menus still don't run
on libbluray, Disney's 3D discs among them; when a menu freezes like that, isoplay plays the main
title instead and says so. A menu waiting for a choice isn't shown as buffering.

## What a link has to be

A direct link to the file (an `.iso`, `.img` or `.zip`, or a download link with no extension:
isoplay reads the first bytes to tell), or a WebDAV folder ending in `/`. The host has to support
HTTP `Range` requests. A protected host works with the credentials in the link, as
`https://user:pass@host/disc.iso`.

Images must already be decrypted. There is no AACS, so retail UHD discs don't play as they are.

Plain video links (`.mkv`, `.mp4` and so on) work too, but those go straight to libVLC, the same
as in VLC.

## Install

| OS | File | First launch |
|---|---|---|
| macOS (Apple silicon) | `isoplay-macos-arm64.zip` | Unsigned: right-click the app, then Open |
| Windows x64 | `isoplay-windows-amd64.zip` | SmartScreen: More info, then Run anyway |
| Linux x64 | `isoplay-linux-x86_64.tar.gz` | Ubuntu 24.04+ or equivalent, X11 or XWayland |

libVLC, libbluray, libdvdnav and a trimmed Java 17 runtime are bundled; there is nothing else to
install. The app checks for new releases on startup and can install them itself, keeping the old
version until the new one starts. Turn that off in **Options** on the start screen.

## Use

Paste a link with ⌘V / Ctrl+V, press ⌘L / Ctrl+L to type one, drop a file or link on the window,
or run `isoplay <file-or-url>`. Local ISOs, zips, `VIDEO_TS` and `BDMV` folders and video files
open the same way.

| Key | Action |
|---|---|
| Arrows, Return | navigate disc menus (on macOS menus don't take clicks yet) |
| Space | play or pause |
| M | disc popup menu |
| ← → (Shift on discs) | seek 10 s |
| ↑ ↓ on videos | volume |
| Page Up / Down | previous or next chapter |
| F | full screen |

The track button holds audio and subtitle tracks, **Add Subtitle File…**, the fit modes (fit,
fill, stretch) and the disc options. If a disc menu misbehaves, choose
**Play Main Title Without Menus**.

**Options** on the start screen sets whether discs start at their menu and the size of the disk
cache (2 GB by default, up to 50 GB, or off). The cache keeps what discs were opened with, not
the film's own stream, so it fills slowly and reopening is what gets faster.

If buffering keeps interrupting, isoplay says **Slow connection** with the rate actually
arriving. That is the host's limit, and VLC would stall on it too.

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
isoplay/        media.py streams and probes links, webdav.py lists a share, diskcache.py keeps
                blocks between plays, disc_image.py projects ISO9660, zip and remote discs as UDF
                or lifts a single title out, app.py is the window, widgets.py and theme.py the UI
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
libVLC log, the ranges the local server answers, and upstream retries, 429s and failures.

## License

MIT. Bundles include VLC (LGPL/GPL) and OpenJDK (GPLv2 with Classpath Exception).
