# The mpv engine spike: what is known, what the spike must prove

Branch `mpv-engine`, written 2026-11-08. Companion to futureplan.md item 1 and
`spike_mpv.py`, which turns the questions below into observations with a real disc.

## What mpv 0.42.0 (2026-11-05) actually added

From the [release notes](https://github.com/mpv-player/mpv/releases/tag/v0.42.0) and
[DOCS/interface-changes.rst](https://github.com/mpv-player/mpv/blob/v0.42.0/DOCS/interface-changes.rst),
verified against `DOCS/man/input.rst` at v0.42.0:

- **Disc menus back.** `stream_dvdnav` implements DVD menu navigation and still frames;
  `stream_bluray` implements HDMV menu navigation, BD-J menu support and still frames;
  `demux_disc` adds a synthetic "Disc Menu" edition; a `--disc-menu` option turns menu playback
  on; the `disc-menu-active` property reports a menu waiting for a choice.
- **Disc input.** The `discnav` command takes actions `up down left right select menu
  title-menu popup audio-menu sub-menu angle-menu up-menu prev red green yellow blue
  mouse-move mouse-click`. Its `mouse-move`/`mouse-click` actions take an optional `<x> <y>` in
  **normalized 0..1 coordinates, top-left 0,0**; with no arguments the position is read from the
  `mouse-pos` property and mpv maps window-derived coordinates into source video
  coordinates itself. mpv's manual: between the synthesised mouse command (`mouse <x> <y>
  [<button>] [<single|double>]`, button omitted = position only) and a menu being up,
  "the arrow keys, ENTER, ESC/BACKSPACE and the mouse control the menu".
- **Direct iso playback.** `.iso` images of DVD-Video, DVD-Audio and Blu-ray are "opened with
  the matching disc backend instead of falling back to FFmpeg" — but only when the image is a
  **local path**: `bluray_stream_open_internal()` needs `mp_file_get_path()` plus a regular
  file for `bd_open_disc()`, and the same holds for `stream_dvdnav`. isoplay's loopback
  `http://127.0.0.1:PORT/disc.iso` therefore fell through to FFmpeg ("Failed to recognize file
  format") on the first real remote disc test. VLC plays the same images because it calls
  libbluray's `bd_open_stream()` and libdvdnav's `dvdnav_open_stream()` with its own block
  reader; `tools/mpv-stream-discs.patch` adds that to mpv (see "The stream patch" below).
- **Network layer.** libcurl-based stream backend: HTTP/2, HTTP/3, connection reuse; options
  `--curl-*`. A debrid bridge's per-request handshake cost partly moves into one connection.
- **libmpv render API.** D3D11 render API backend for Windows landed; OpenGL is everywhere
  already; software fallback in the render API remains.

## Open questions the docs settle, and what the spike adds

| Question | Docs' answer | Spike still has to |
| --- | --- | --- |
| exact `mouse`/`discnav` syntax | settled above | forward real Qt events; verify our *FBO-pixel* coordinate story maps to the video, and that a still menu's highlight follows |
| do synthetic mouse commands reach disc menus? | discnav `mouse-*` are engine-level commands, and the manual says the mouse controls a menu | confirm a click selects a button on an HDMV disc (Eega) |
| BD-J and the JVM | mpv 0.42 "adds BD-J menu support"; the JVM question is libbluray's, as with VLC | test Shutter Island and Blade Runner: does it need `JAVA_HOME`/a dlopen-able `libjvm`, do fonts draw, or does a menu famously without one hang |
| does python-mpv expose the render API? | **yes**: python-mpv 1.0.8 wraps `mpv_render_context_create/render/set_update_callback` (`MpvRenderContext`, `MpvOpenGLFBO`, `MpvGlGetProcAddressFn`; ctypes structs included) | none left for discovery; the spike's window exercises the actual path |
| menüs on macOS specifically | no platform difference documented; render API draws into the app's own surface | confirm the click lands on a menu button while the window is Qt's |

mpv's manual also documents `--tls-verify` as **default yes** since 0.42 — isoplay's loopback
traffic is plain `http`, so that does not touch us; upstream https will now verify, which is
what we want.

python-mpv quirks to respect (`site-packages/mpv.py` 1.0.8):

- options are set by **option name** (`config=no`), not CLI spelling (`no-config` errors with
  "mpv option does not exist");
- no file loaded plus `idle=no` shuts the core down instantly: libmpv players run `idle=yes`
  between sources here;
- the render context is created on an initialized player and needs `vo=libmpv`.

## The stream patch (`tools/mpv-stream-discs.patch`)

mpv's disc backends open a device path: `bd_open_disc()` for Blu-ray, `dvdnav_open()` for DVD.
libbluray and libdvdnav also expose block-callback entry points (`bd_open_stream`,
`dvdnav_open_stream`) which VLC uses for network inputs; the patch adds both to mpv:

- `stream_bluray.c` and `stream_dvdnav.c` create a nested `stream_create()` for a device URL,
  keep it in the stream's private state, and let libbluray/libdvdnav read 2048-byte blocks
  through it. `STREAM_ORIGIN_DIRECT` is required: a nested open with no origin resolves as
  `STREAM_UNSAFE`, which surfaces as "No protocol handler found";
- Blu-ray reads are serialized by a mutex (libbluray can read from more than one thread) and
  freed with the stream;
- `tools/build_mpv.py` clones v0.42.0, applies the patch, and installs to `build/mpv`;
  `dev.py --mpv` prefers that build automatically through `LIBMPV_PATH`.

## BD-J jars

BD-J needs a Java VM and a `libbluray-j2se-<native version>.jar`; Homebrew installs no jar where
libbluray looks. `tools/build_mpv.py` also builds the matching main and awt jars into
`build/libbluray/` when ant and javac are present, and `dev.py --mpv` points `LIBBLURAY_CP` at
that directory. A 1.4.1 jar under a 1.5.0 native library makes libbluray disable BD-J: mpv then
says so and plays the main title instead.

What the real-disc runs proved (2026-11-08): a remote DVD and a BD-J Blu-ray both played through
isoplay's loopback with the patched build. The Blu-ray booted in menu mode (`cfg_title=-1`,
`hdmv_mode=1`) and `disc-menu-active` became true after the JVM boot and first-play logos
(~55 s cold; the disk cache shortens later opens). The main feature playlist was 2:33:29.

## Where libmpv comes from on each platform

- **macOS**: `tools/build_mpv.py` builds the patched libmpv into `build/mpv` (meson, ninja,
  libdvdnav and ant needed). Without it, `binding()` falls back to Homebrew's
  `libmpv.2.dylib`, which plays local discs only and never sees isoplay's loopback URL.
  **mpv 0.42.0 or newer is required**; older libmpv fails at engine creation with a message
  naming why.
- **Windows**: community `libmpv` builds (mpv-player-windows/libmpv) drop `libmpv-2.dll` next
  to the app; python-mpv searches `%PATH%` and its own module dir.
- **Linux**: distro `libmpv2`/`mpv-libs`.

Bundling libmpv + its FFmpeg in `build.py` for three platforms is still future work (futureplan
item 1, "Risks"); the spike runs on system libraries and the bundling question is answered
after the menus pass.

## How to run it

```sh
python tools/build_mpv.py                    # once: patched libmpv + matching BD-J jar
python dev.py --mpv Eega.iso                 # the app itself, on the mpv engine
python spike_mpv.py Eega.iso                 # only the menu questions, in a window
python spike_mpv.py --headless 20 Eega.iso   # the same, without a window, printed
python dev.py                                # libVLC, exactly as before
```

`ISOPLAY_ENGINE=mpv` (or `dev.py --mpv`) selects the engine; without it isoplay stays on libVLC
no matter what is on the system.
