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

A Blu-ray image is 25 to 100 GB, and more and more of them live on a debrid service, a WebDAV
share or a download link rather than a local disk. Downloading one to watch it, or extracting it
first when it comes zipped, takes longer than the film. Playing straight from the link only works
if the player can open what the link points at, and often it can't. isoplay plays those discs as
they were authored, menus included, without downloading or mounting anything.

## Why not just VLC?

VLC plays an ISO from a link, and isoplay is libVLC underneath, with the same libbluray and
libdvdnav. If VLC plays your disc well, keep using it. isoplay differs in four ways:

- **It opens what VLC can't.** A `BDMV` or `VIDEO_TS` folder on a WebDAV share; a disc, or an
  `.iso`, inside an uncompressed `.zip`; ISO9660-only Blu-ray images, which libbluray can't read
  and isoplay re-presents as UDF; HD DVD, Video CD and DVD-Audio, whose main title it plays since
  libVLC can't run their menus. Images holding several discs open a chooser.
- **It reads in parallel.** VLC uses one connection, one request at a time. isoplay fetches
  several ranges at once and reads ahead, finding how many connections a host tolerates and
  backing off when it pushes back. It can't add bandwidth the host or your line doesn't have.
- **It keeps what menus need.** Opening a disc and running its menus jumps all over the image,
  and over a network every jump is a round trip. isoplay holds those blocks in memory and in a
  disk cache, so a disc reopens without them.
- **It works around menus that don't run.** It bundles the fonts BD-J menus need, and when a
  Java menu freezes, as some studios' do on libbluray, it plays the main title instead.

## Links

A direct link to an `.iso`, `.img` or `.zip` (extensionless download links work too), or a WebDAV
folder ending in `/`. The host must support HTTP `Range` requests; credentials go in the link,
as `https://user:pass@host/disc.iso`. Images must already be decrypted: there is no AACS, so
retail UHD discs won't play as they are. Plain video links go straight to libVLC, as in VLC.

## Install

| OS | File | First launch |
|---|---|---|
| macOS (Apple silicon) | `isoplay-macos-arm64.zip` | Unsigned: right-click the app, then Open |
| Windows x64 | `isoplay-windows-amd64.zip` | SmartScreen: More info, then Run anyway |
| Linux x64 | `isoplay-linux-x86_64.tar.gz` | Ubuntu 24.04+ or equivalent, X11 or XWayland |

libVLC and a trimmed Java runtime are bundled, so there is nothing else to install. The app
offers new releases and installs them, keeping the old version until the new one starts; turn
the check off in **Options**.

## Use

Paste a link (⌘V / Ctrl+V), type one (⌘L / Ctrl+L), drop a file or link on the window, or run
`isoplay <file-or-url>`. Local ISOs, zips and disc folders open the same way.

| Key | Action |
|---|---|
| Arrows, Return | navigate disc menus (macOS menus don't take clicks yet) |
| Space | play or pause |
| M | disc popup menu |
| ← → (Shift on discs) | seek 10 s |
| ↑ ↓ on videos | volume |
| Page Up / Down | previous or next chapter |
| F | full screen |

- The **track button** holds audio and subtitle tracks, subtitle files, fit modes and disc
  options, including **Play Main Title Without Menus**.
- **Options** sets whether discs start at their menu and the disk cache size: 2 GB by default,
  up to 50 GB, or off. It keeps what discs were opened with, not the film's stream.
- **Slow connection** appears when buffering keeps interrupting, with the rate actually
  arriving: the host's limit, which VLC would hit too.

## Development

Running from source needs VLC installed; with a JDK 17+ present, `dev.py` also builds the Java
runtime the bundle ships, so BD-J menus behave the same.

```sh
python3 dev.py                  # creates .venv, installs deps, starts the app
python3 dev.py path/or/url      # start on a file or link
python3 dev.py --tests          # run the test suite
.venv/bin/pip install -r requirements-build.txt && .venv/bin/python build.py   # bundle in dist/
```

`media.py` streams and probes links, `webdav.py` lists shares, `diskcache.py` keeps blocks
between plays, `disc_image.py` presents ISO9660, zip and remote discs as UDF, and `app.py`,
`widgets.py` and `theme.py` are the interface. Linux builds need the distro VLC
(`libvlc5 vlc-plugin-base vlc-plugin-video-output libbluray-bdj`). Pushing a `v*` tag builds
every platform and publishes a release. `ISOPLAY_DEBUG=1` logs libVLC and every retry, 429 and
failure upstream.

## License

MIT. Bundles include VLC (LGPL/GPL) and OpenJDK (GPLv2 with Classpath Exception).
