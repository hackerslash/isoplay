<p align="center">
  <img src="isoplay/assets/icon.svg" width="96" alt="isoplay icon">
</p>

<h1 align="center">isoplay</h1>

<p align="center">
  Play Blu-ray and DVD discs straight from a link, menus and all: ISOs on a web server, disc
  folders on a WebDAV share, discs inside a zip.
</p>

<p align="center">
  <a href="../../releases/latest"><b>Download</b></a>
</p>

![isoplay playing a Blu-ray disc menu](docs/screenshot.png)

## Why this exists

A Blu-ray image is 25 to 100 GB, and more and more of them live on a debrid service, a WebDAV
share or a download link rather than a local disk. Downloading one to watch it, or extracting it
first when it comes zipped, takes longer than the film. isoplay plays those discs as they were
authored, menus included, without downloading or mounting anything.

## What it does

isoplay plays through libmpv with libbluray and libdvdnav, patched so a disc image can be read
over a stream instead of only from a local path.

- **It opens discs where they are.** An `.iso` or `.img` behind a link; a `BDMV` or `VIDEO_TS`
  folder on a WebDAV share; a disc, or an `.iso`, inside an uncompressed `.zip`; ISO9660-only
  Blu-ray images, which libbluray can't read and isoplay re-presents as UDF; HD DVD, Video CD
  and DVD-Audio, whose main title it plays since their menus can't run. Images holding several
  discs open a chooser.
- **It reads in parallel.** isoplay fetches several ranges at once and reads ahead, learning how
  many connections and how large a request a host tolerates, backing off when it pushes back
  and retrying dropped or throttled requests. It can't add bandwidth the host or your line
  doesn't have.
- **It keeps what menus need.** Opening a disc and running its menus jumps all over the image,
  and over a network every jump is a round trip. isoplay holds those blocks in memory and in a
  disk cache, so a disc reopens without them.
- **Menus take the mouse.** Hover and click disc menus, or use the keyboard. It bundles the
  fonts BD-J menus need, and when a Java menu freezes, as some studios' do on libbluray, it
  plays the main title instead.

## Links

A direct link to an `.iso`, `.img` or `.zip` (extensionless download links work too), or a WebDAV
folder ending in `/`. The host must support HTTP `Range` requests; credentials go in the link,
as `https://user:pass@host/disc.iso`. Images must already be decrypted: there is no AACS, so
retail UHD discs won't play as they are. Plain video links play as they are.

## Install

| OS | File | First launch |
|---|---|---|
| macOS (Apple silicon) | `isoplay-macos-arm64.zip` | Unsigned: right-click the app, then Open |
| Windows x64 | `isoplay-windows-amd64.zip` | SmartScreen: More info, then Run anyway |
| Linux x64 | `isoplay-linux-x86_64.tar.gz` | Ubuntu 24.04+ or equivalent |

libmpv and a trimmed Java runtime are bundled, so there is nothing else to install. The app
offers new releases and installs them, keeping the old version until the new one starts; turn
the check off in **Options**.

## Use

Paste a link (⌘V / Ctrl+V), type one (⌘L / Ctrl+L), drop a file or link on the window, or run
`isoplay <file-or-url>`. Local ISOs, zips and disc folders open the same way.

| Key / mouse | Action |
|---|---|
| Arrows, Return, mouse | navigate disc menus |
| Space, click | play or pause |
| Double click, F | full screen |
| Right click | tracks menu |
| Scroll wheel | volume |
| M | disc popup menu |
| ← → (Shift in disc menus) | seek 10 s |
| ↑ ↓ on videos | volume |
| Home | back to the start |
| Page Up / Down | previous or next chapter |

- Films **resume** where you left off; discs played with menus always start at the menu.
- The **track button** holds audio and subtitle tracks, subtitle files, fit modes and disc
  options, including **Play Main Title Without Menus**.
- **Options** (the Playback menu on macOS) sets whether discs start at their menu, preferred audio and subtitle languages,
  audio passthrough to a receiver, and the disk cache size: 2 GB by default, up to 50 GB, or
  off. It keeps what discs were opened with, not the film's stream.
- **Slow connection** appears when buffering keeps interrupting, with the rate actually
  arriving: the host's limit, not isoplay's.
- **Copy Diagnostics** (Options, or Help on macOS) puts versions, the source (password removed) and the recent mpv log on
  the clipboard for a bug report.

## Development

Running from source needs libmpv 0.42+. `tools/build_mpv.py` builds the patched one the bundle
ships (stock mpv opens disc images only from local paths, so remote discs need the patch) and
the BD-J jar matching libbluray; with a JDK 17+ present, `dev.py` also builds the Java runtime,
so BD-J menus behave the same.

```sh
python3 tools/build_mpv.py      # patched libmpv into build/mpv, BD-J jars into build/libbluray
python3 dev.py                  # creates .venv, installs deps, starts the app
python3 dev.py path/or/url      # start on a file or link
python3 dev.py --tests          # run the test suite
.venv/bin/pip install -r requirements-build.txt && .venv/bin/python build.py   # bundle in dist/
```

`media.py` streams and probes links, `webdav.py` lists shares, `diskcache.py` keeps blocks
between plays, `disc_image.py` presents ISO9660, zip and remote discs as UDF, `mpv.py` is the
playback engine, and `app.py`, `widgets.py` and `theme.py` are the interface.
.github/workflows/build.yml lists each platform's build dependencies. Pushing a `v*` tag builds
every platform and publishes a release. `ISOPLAY_DEBUG=1` logs libmpv and every retry, 429 and
failure upstream (`ISOPLAY_DEBUG=verbose` for libbluray's BD-J too).

## License

MIT. Bundles include mpv and FFmpeg (GPL) and OpenJDK (GPLv2 with Classpath Exception); see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
