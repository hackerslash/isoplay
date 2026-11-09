# Changelog

## Unreleased

- **libmpv replaces libVLC.** Disc menus take the mouse on every platform, video is drawn into
  the window by mpv's render API, and disc images stream to a patched libmpv
  (`tools/mpv-stream-discs.patch`, built by `tools/build_mpv.py`) that reads them through
  libbluray's and libdvdnav's block callbacks. Bundles ship that libmpv with its libraries and
  BD-J jars built for the bundled libbluray
- mpv's cache, readahead and FFmpeg reconnects are tuned for network play, and seeks show the
  buffering spinner while the picture is frozen

## 0.3.7

- A disc's top menu no longer hides behind a "Buffering 0%" spinner: libVLC reports a menu waiting
  for a choice as buffering, so a disc like Eega looked stuck after its intro. That wait no longer
  counts toward the slow-connection warning either
- A slow host no longer gets an open called off: the 45 s "Nothing played from this" limit now
  only applies while no data is arriving, so a disc that takes a minute to show its first frame
  plays instead of being abandoned

## 0.3.6

- Reopening a disc skips most of its round trips: what a disc was opened with is kept in a disk
  cache, 2 GB by default and sized (or turned off and cleared) under **Options → Disk Cache**.
  Read-ahead of the film itself isn't kept, so it can't flush out the menus
- Read-ahead finds how many connections a host takes, as TCP finds its window: one more per round
  while there is more to fetch, halved on a 429 or a failure, up to 10 for a link
- A Blu-ray whose BD-J menu libbluray can't run, which froze at the end of a feature-length title
  the moment it opened, now plays its main title instead, and says so
- Playback that keeps stalling (three pauses of 3 s or more within 3 minutes) says the connection
  is too slow for the video, and how fast it is arriving; the brief buffering after a seek or a
  menu doesn't count
- Quitting no longer waits up to 30 s for a download in flight; closing wakes it straight away
- Opening a link costs one request fewer: the probe's answer already carries the file's size
- A share's files share one 256 MiB memory budget, instead of each holding up to 192 MiB
- Bundles are ~20 MB smaller: QtPdf, QtQuick, QtQml and the virtual keyboard are left out, and the
  build now starts the window offscreen to prove what's left still draws it

## 0.3.2

- HD DVD, Video CD, DVD-Audio and plain rips of media files now play from an ISO or a `.zip`:
  with no menu format libVLC can navigate, the main title is served as itself, numbered
  `.EVO`/`.VOB`/`.DAT` parts joined back into one stream
- Local `.zip` discs no longer have to be extracted first; they open like a linked one
- A disc image stored inside a `.zip` is opened in turn, so a release that ships `movie.iso`
  in an archive keeps its menus instead of being refused
- Local ISO9660 Blu-rays get their menus too: they are republished as UDF like a linked one
  rather than handed to libbluray, which cannot read ISO9660
- **WebDAV shares play like a folder.** Paste a share URL and the disc folder on it is listed
  with PROPFIND and projected as UDF, so a `BDMV` or `VIDEO_TS` tree streams with its menus
  without being downloaded or mounted. A share with no disc on it plays its largest media file
- Links carrying credentials (`https://user:pass@host/disc.iso`) now authenticate, so WebDAV
  shares and other protected hosts work. The user:pass was previously handed to DNS as part of
  the hostname, so the link failed to resolve at all
- A host that advertises `Accept-Ranges` and then answers with the whole file is now refused
  in seconds. The body read was unbounded, so such a link tried to buffer the entire file into
  memory before anything noticed the range had been ignored
- `ISOPLAY_DEBUG=1` now also reports upstream retries and errors the local server hits, which
  were being swallowed silently
- Transport controls no longer stay pinned over a disc menu: a still menu reports "not playing",
  which the auto-hide rule read as "keep the controls up"
- Java (BD-J) menus that draw text no longer stay black: libbluray looks for the Lucida fonts the
  JDK stopped shipping in 11, so the bundled runtime now carries DejaVu under those names
- Streaming stays within what hosts allow: read-ahead goes out as a few 4 MiB requests, about
  20 s ahead of the player, and backs off on a 429, where a dozen small requests at once got a
  CDN to refuse the connection outright. Discs inside a zip or ISO get read-ahead again
- A multi-disc zip opens in seconds: only the chosen disc's headers are read, merged and in
  parallel, instead of every entry's one round trip at a time (~17 min for a 1007-file season)
- WebDAV discs start about twice as fast: folders are listed in parallel, the disc's small files
  are fetched ahead, and the player never queues behind that speculative traffic
- Known: some studios' BD-J menus (Disney 3D among them) don't run on libbluray at all; use
  **Play Main Title Without Menus** for those

Known: disc menus cannot be clicked on macOS. libVLC embeds through `caopengllayer`, whose view
gets no mouse-moved events from a Qt-owned window, so nothing highlights under the pointer.
`--vout`/`:vout` are ignored when embedding, so the NSView-based output can't be selected.
Arrow keys and Return navigate menus as before.

## 0.3.0

- Discs stored uncompressed in a `.zip` now stream in place, without unpacking
- Links are identified by their first bytes, so extensionless download links work
- Open Disc Folder…, with a chooser when a folder holds several rips
- Fit, fill and stretch modes, and external subtitle files
- Remove a single item from Recent by hovering its row
- A source that won't play returns to the start screen with the reason, instead of a black screen
- Recent only records what actually played
