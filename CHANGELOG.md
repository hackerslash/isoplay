# Changelog

## Unreleased

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
