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
- Transport controls no longer stay pinned over a disc menu: a still menu reports "not playing",
  which the auto-hide rule read as "keep the controls up"

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
