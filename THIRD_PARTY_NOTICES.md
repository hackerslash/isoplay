# Third-party software in isoplay bundles

isoplay itself is MIT licensed. Its release bundles also contain the software below, each under
its own licence. Because libmpv and FFmpeg are built with GPL components, the bundle as a whole
is distributed under the terms of the GNU GPL version 2 or later.

| Component | Licence | Source |
|---|---|---|
| mpv / libmpv 0.42.0, with `tools/mpv-stream-discs.patch` | GPL-2.0-or-later | https://github.com/mpv-player/mpv/tree/v0.42.0 and this repository |
| FFmpeg | GPL-2.0-or-later (built with x264/x265) | https://ffmpeg.org/download.html |
| libplacebo | LGPL-2.1-or-later | https://code.videolan.org/videolan/libplacebo |
| libass, FreeType, HarfBuzz, FriBidi, fontconfig | ISC, FTL, MIT, LGPL-2.1, MIT | their upstream projects |
| libbluray and its BD-J jars | LGPL-2.1-or-later | https://code.videolan.org/videolan/libbluray |
| libdvdnav, libdvdread | GPL-2.0-or-later | https://code.videolan.org/videolan/libdvdnav |
| libcurl | curl licence (MIT-style) | https://curl.se |
| OpenJDK runtime (jlink image) | GPL-2.0 with Classpath Exception | https://openjdk.org |
| DejaVu fonts (as Lucida replacements for BD-J) | Bitstream Vera / DejaVu licence | https://dejavu-fonts.github.io |
| Qt 6 / PySide6 | LGPL-3.0 | https://www.qt.io/download-open-source |
| Python | PSF-2.0 | https://www.python.org |
| python-mpv | GPL-2.0-or-later or LGPL-2.1-or-later | https://github.com/jaseg/python-mpv |
| urllib3 | MIT | https://github.com/urllib3/urllib3 |
| certifi | MPL-2.0 | https://github.com/certifi/python-certifi |
| pycdlib | LGPL-2.1 | https://github.com/clalancette/pycdlib |

The exact native libraries in a bundle are listed at the end of the bundled copy of this file.
To get the corresponding source for a release, follow the links above at the versions listed, or
open an issue and it will be provided.
