# Product

<!-- impeccable:product-schema 1 -->

## Platform

Desktop app on macOS, Windows and Linux (PySide6 over libVLC). Not a web surface.

## Users

One viewer at a desk: laptop or monitor at arm's length, mouse or trackpad, windowed or fullscreen.
They play remote Blu-ray and DVD images from direct links and local video files.

## Product Purpose

Play Blu-ray (BD25/50, UHD BD66/100) and DVD (DVD5/9) images with their disc menus, local or over
HTTP, plus ordinary video. Success: paste a link, the disc menu appears, the movie plays.

## Positioning

Streams remote disc images with working menus, BD-J included, from a single self-contained
download. No mount, no install, no system Java.

## Capabilities and Constraints

- Disc type is detected from content (ISO 9660/UDF descriptor, then BDMV/VIDEO_TS).
- Remote images go through a local block cache with parallel read-ahead.
- Bundled Java 17 for BD-J; AACS-encrypted images do not play.
- The video surface is a native libVLC view; anything drawn over it is a separate floating window.
- Disc menus can fail on some BD-J discs; playing the main title without menus is the fallback.

## Brand Commitments

Name: isoplay (lowercase). App icon: gradient disc with a play glyph on a dark rounded square
(`assets/icon.svg`). Feel: native and invisible, in the company of IINA and QuickTime. The video is
the product; chrome stays quiet. No em dashes in UI copy.

## Product Principles

1. The picture comes first; controls appear on intent and leave without being asked.
2. Speak in plain player language: Open File, Open Location, Disc Menu, Main Title.
3. Every failure says what happened and what to try next.
4. Recent discs are one click from the empty window.
