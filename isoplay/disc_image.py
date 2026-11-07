"""Present a disc buried in an ISO9660 image or a zip as something libVLC can open.

libbluray reads UDF, not ISO9660 or zip, and libdvdread wants VIDEO_TS at the root, so
DVDs and Blu-rays are republished as UDF. Formats libVLC plays but cannot navigate --
HD DVD, VCD, DVD-Audio, a plain rip of media files -- have their main title served as
itself instead. Either way only metadata is generated, in memory; the payload stays in
the upstream cache and is never copied.
"""
import bisect
import io
import re
import struct
import threading
import zipfile

import pycdlib


class CacheFile(io.RawIOBase):
    """Seekable file interface used while reading filesystem metadata."""

    def __init__(self, cache):
        self.cache, self.pos = cache, 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=0):
        pos = (0 if whence == 0 else self.pos if whence == 1 else self.cache.size) + offset
        if pos < 0 or whence not in (0, 1, 2):
            raise ValueError("invalid seek")
        self.pos = pos
        return pos

    def read(self, size=-1):
        if size < 0:
            size = self.cache.size - self.pos
        if size >= 1 << 20 and hasattr(self.cache, "prefetch"):
            self.cache.prefetch(self.pos)
        data = self.cache.read(self.pos, size)
        self.pos += len(data)
        return data


class _MetadataImage(io.RawIOBase):
    """Sparse output: metadata pages live in RAM; file contents remain references."""

    def __init__(self):
        self.pos = self.size = 0
        self.pages, self.files = {}, []
        self.lock = threading.Lock()

    def writable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=0):
        self.pos = (0 if whence == 0 else self.pos if whence == 1 else self.size) + offset
        return self.pos

    def write(self, data):
        n = len(data)
        while data:
            page, off = divmod(self.pos, 2048)
            count = min(len(data), 2048 - off)
            self.pages.setdefault(page, bytearray(2048))[off:off + count] = data[:count]
            if len(self.pages) > 8192:
                raise OSError("disc filesystem metadata exceeds 16 MiB")
            self.pos += count
            data = data[count:]
        self.size = max(self.size, self.pos)
        return n

    def read(self, pos, size):
        end = min(pos + size, self.size)
        out = bytearray(max(0, end - pos))
        # Files never overlap metadata pages, and pread keeps no cursor, so readers
        # do not have to queue behind each other's network fetches.
        i = max(0, bisect.bisect_right(self.starts, pos) - 1)
        spans = []
        for start, length, fp in self.files[i:]:
            if start >= end:
                break
            a, b = max(pos, start), min(end, start + length)
            if a < b:
                spans.append((start, a, b, fp))
                if hasattr(fp, "want"):
                    fp.want(a - start, b - a)  # a disc's small files sit together; fetch them at once
        for start, a, b, fp in spans:
            data = fp.pread(a - start, b - a)
            if len(data) != b - a:
                raise OSError("short read in disc file")
            out[a - pos:b - pos] = data
        for page in range(pos // 2048, (end + 2047) // 2048):
            data = self.pages.get(page)
            if data is not None:
                a, b = max(pos, page * 2048), min(end, (page + 1) * 2048)
                out[a - pos:b - pos] = data[a - page * 2048:b - page * 2048]
        return bytes(out)

    def close(self):
        with self.lock:
            for fp in getattr(self, "handles", ()):
                fp.close()
            if hasattr(self, "udf"):
                self.udf.close()
        super().close()


class _MappedUDF(pycdlib.PyCdlib):
    # This is the only private pycdlib hook. Pin its version and exercise the
    # resulting image with both pycdlib and libVLC when upgrading the dependency.
    def _output_file_data(self, outfp, blocksize, ino):
        start = ino.extent_location() * self.logical_block_size
        length = ino.get_data_length()
        outfp.files.append((start, length, ino.data_fp))
        outfp.seek(start + ((length + 2047) // 2048) * 2048)
        outfp.size = max(outfp.size, outfp.tell())
        yield length


class _ExtentFile(io.RawIOBase):
    """One file of the source image, as one or more (offset, length) spans of it."""

    def __init__(self, cache, spans):
        self.cache, self.pos = cache, 0
        self.parts, self.starts, self.size = [], [], 0
        for offset, length in spans:
            if offset < 0 or offset + length > cache.size:
                raise OSError("disc file extends beyond the image")
            self.starts.append(self.size)
            self.parts.append((offset, length))
            self.size += length

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=0):
        self.pos = (0 if whence == 0 else self.pos if whence == 1 else self.size) + offset
        return self.pos

    def pread(self, pos, size):
        """Read without touching the cursor, so concurrent readers need no lock."""
        size = min(size, self.size - pos)
        out = bytearray()
        while size > 0:
            i = bisect.bisect_right(self.starts, pos) - 1
            offset, length = self.parts[i]
            within = pos - self.starts[i]
            count = min(size, length - within)
            if count >= 1 << 20 and hasattr(self.cache, "prefetch"):
                self.cache.prefetch(offset + within)
            data = self.cache.read(offset + within, count)
            if len(data) != count:
                raise OSError("short read in disc extent")
            out += data
            pos += count
            size -= count
        return bytes(out)

    def read(self, size=-1):
        data = self.pread(self.pos, self.size - self.pos if size < 0 else size)
        self.pos += len(data)
        return data


def iso_signature(data):
    """ISO 9660 (CD001) or UDF (BEA01) volume descriptor at sector 16: DVD5/9, BD25/50/66/100."""
    return data[32769:32774] in (b"CD001", b"BEA01")


def has_udf(data):
    return any(data[s * 2048 + 1:s * 2048 + 6] in (b"BEA01", b"NSR02", b"NSR03") for s in range(16, 32))


def disc_kind(data):
    if b"BDMV" in data:
        return "bluray"
    if b"VIDEO_TS" in data:
        return "dvd"
    return None


class _FileView(io.RawIOBase):
    """One title lifted out of an image and served as itself, for what libVLC can't navigate."""

    def __init__(self, fp, name):
        self.fp, self.size, self.name = fp, fp.size, name
        self.discs, self.disc_root, self.kind = (), None, None

    def read(self, pos, size):
        return self.fp.pread(pos, size)

    def close(self):
        self.fp.close()
        super().close()


MEDIA_EXT = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm", ".ts", ".m2ts", ".mts", ".wmv", ".flv",
             ".mpg", ".mpeg", ".vob", ".evo", ".dat", ".aob", ".mka", ".mp3", ".flac", ".ogg", ".opus",
             ".wav", ".m4a", ".ac3", ".dts", ".aiff", ".wv", ".ape"}
NUMBERED = re.compile(r"(.*?)(\d+)(\.[^.]+)")


def _ext(path):
    stem, dot, ext = path.rpartition("/")[2].rpartition(".")
    return "." + ext.lower() if dot else ""


def _main_title(files):
    """(name, spans) of the largest playable file plus its numbered continuations.

    VOB, EVO and DAT sets split one title across siblings that differ only in a
    fixed-width trailing number, so the biggest file alone is never the title.
    """
    playable = {path: spans for path, spans in files.items() if _ext(path) in MEDIA_EXT}
    if not playable:
        return None, None
    size = lambda path: sum(length for _, length in playable[path])  # noqa: E731
    main = max(playable, key=lambda path: (size(path), path))
    parent, _, name = main.rpartition("/")
    match = NUMBERED.fullmatch(name)
    if not match:
        return name, playable[main]
    sequel = re.compile(re.escape(match[1]) + r"\d{%d}" % len(match[2]) + re.escape(match[3]), re.I)
    group = sorted(p for p in playable
                   if p.rpartition("/")[0] == parent and sequel.fullmatch(p.rpartition("/")[2]))
    spans = []
    for path in group:
        spans += playable[path]
    return group[0].rpartition("/")[2], spans


def _title_view(cache, files):
    name, spans = _main_title(files)
    return _FileView(_ExtentFile(cache, spans), name) if spans else None


def _walk_iso9660(cache, skip_udf=False):
    """({path: [(offset, length)]}, directories) for the ISO9660 tree, or (None, None) for UDF."""
    original = pycdlib.PyCdlib()
    original.open_fp(CacheFile(cache))
    try:
        if skip_udf and original.has_udf():
            return None, None
        directories, files = [], {}
        todo = [("", original.get_record(iso_path="/"))]
        while todo:
            path, directory = todo.pop()
            directories.append(path)
            for record in directory.children:
                if record.is_dot() or record.is_dotdot() or record.is_symlink():
                    continue
                name = record.file_identifier().decode("ascii", "replace").split(";", 1)[0]
                if record.rock_ridge is not None:
                    name = record.rock_ridge.name().decode("utf-8")
                elif not record.is_dir():
                    stem, dot, ext = name.rpartition(".")
                    name = stem + dot + ext.lower() if dot else name
                    name = {"INDEX.bdmv": "index.bdmv", "MOVIEOBJ.bdmv": "MovieObject.bdmv",
                            "MOVIEOBJECT.bdmv": "MovieObject.bdmv"}.get(name, name)
                child = path + "/" + name
                if record.is_dir():
                    todo.append((child, record))
                else:
                    files.setdefault(child, []).append((record.extent_location() * 2048, record.data_length))
        return files, directories
    finally:
        original.close()


IMAGE_EXT = {".iso", ".img", ".udf"}


def _nested_image(cache, files):
    """The biggest disc image stored inside the archive, routed to the engine that can navigate it.

    An image served as itself keeps its menus: libdvdread reads ISO9660 and libbluray reads
    UDF straight off the stream. Only what neither can navigate is rewritten.
    """
    images = {path: spans for path, spans in files.items() if _ext(path) in IMAGE_EXT}
    if not images:
        return None
    path = max(images, key=lambda p: (sum(length for _, length in images[p]), p))
    inner = _FileView(_ExtentFile(cache, images[path]), path.rpartition("/")[2])
    head = inner.read(0, 4 << 20)
    if not iso_signature(head):
        return None
    inner.kind = disc_kind(head)
    if inner.kind == "bluray" and not has_udf(head):
        return project_iso9660(inner)  # libbluray can't read ISO9660; republish it as UDF
    return project_media(inner) if inner.kind is None else inner


def project_media(cache):
    """The main title of an image libVLC plays but can't navigate: HD DVD, VCD, DVD-Audio, data rips."""
    try:
        files, _ = _walk_iso9660(cache)
    except Exception:
        return None  # not a tree pycdlib can read; let libVLC try the image itself
    return _title_view(cache, files)


def project_iso9660(cache, root=None):
    """Return a UDF view for an ISO9660-only Blu-ray, or None for other images.

    Default to the disc with the most stream data; root selects a different
    disc folder. Expose all choices on the view. No media payload is fetched.
    """
    files, directories = _walk_iso9660(cache, skip_udf=True)
    if files is None:
        return None
    streams = {path: _ExtentFile(cache, spans) for path, spans in files.items()}
    candidates = {}
    for path, fp in streams.items():
        parent, _, name = path.rpartition("/")
        if parent.upper().endswith("/BDMV/STREAM") and name.upper().endswith(".M2TS"):
            disc_root = parent[:-len("/BDMV/STREAM")]
            candidates[disc_root] = candidates.get(disc_root, 0) + fp.size
    if not candidates:
        return None
    discs = tuple(sorted(candidates, key=lambda path: (-candidates[path], path)))
    if root is None:
        root = discs[0]
    elif root not in candidates:
        raise ValueError("The selected disc folder is not in this image")
    inside = {path[len(root):]: fp for path, fp in streams.items() if path.startswith(root + "/")}
    return _build_udf(inside, [p[len(root):] for p in directories if p.startswith(root + "/")],
                      discs, root, "bluray")


def _build_udf(streams, directories, discs, root, kind):
    udf = _MappedUDF()
    udf.new(interchange_level=3, udf="2.60", vol_ident="ISOPLAY")
    for path in sorted(directories, key=lambda p: (p.count("/"), p)):
        udf.add_directory(udf_path=path)
    handles = []
    for path, fp in sorted(streams.items()):
        handles.append(fp)
        udf.add_fp(fp, fp.size, udf_path=path)
    view = _MetadataImage()
    udf.write_fp(view)
    view.files.sort(key=lambda entry: entry[0])
    view.starts = [entry[0] for entry in view.files]
    view.udf, view.handles = udf, handles
    view.discs, view.disc_root, view.kind = discs, root, kind
    return view


DVD_EXT = (".IFO", ".BUP", ".VOB")


def _stored_spans(cache, archive):
    """(offset, length) of each uncompressed entry's payload, from its local header."""
    spans = {}
    for info in archive.infolist():
        if info.is_dir() or info.compress_type != zipfile.ZIP_STORED or info.flag_bits & 0x1:
            continue
        head = cache.read(info.header_offset, 30)
        if head[:4] != b"PK\x03\x04":
            continue
        name_len, extra_len = struct.unpack("<HH", head[26:30])
        spans[info.filename] = (info.header_offset + 30 + name_len + extra_len, info.file_size)
    return spans


def _zip_discs(names):
    """{root: (kind, {path in the image: entry name})}. DVDs move to /VIDEO_TS, the only place
    libdvdread looks, whatever folder they were zipped in."""
    found = {}
    for name in names:
        low = name.lower()
        if low.endswith("bdmv/index.bdmv"):
            root = name[:-len("BDMV/index.bdmv")].rstrip("/")
            prefix = root + "/" if root else ""
            found[root] = ("bluray", {"/" + n[len(prefix):]: n for n in names
                                      if n.startswith(prefix) and not n.endswith("/")})
        elif low.endswith("video_ts.ifo"):
            root = name.rpartition("/")[0]
            prefix = root + "/" if root else ""
            found[root] = ("dvd", {"/VIDEO_TS/" + n[len(prefix):].upper(): n for n in names
                                   if n.startswith(prefix) and "/" not in n[len(prefix):]
                                   and n.upper().endswith(DVD_EXT)})
    return found


def _assemble(found, root, discs, open_stream):
    """Project one disc of {root: (kind, {path in the image: source})} as a UDF view."""
    kind, layout = found[root]
    streams = {path: open_stream(source) for path, source in layout.items()}
    directories = set()
    for path in streams:
        parent = path.rpartition("/")[0]
        while parent:
            directories.add(parent)
            parent = parent.rpartition("/")[0]
    return _build_udf(streams, sorted(directories), discs, root, kind)


def project_remote(files, root=None):
    """A disc folder on a remote share, as a UDF view; its files stay where they are.

    files is {path on the share: an open seekable reader}. With no disc folder on it,
    the largest playable file is served as itself, as for a zip.
    """
    found = _zip_discs(files)
    if not found:
        name = max((p for p in files if _ext(p) in MEDIA_EXT),
                   key=lambda p: (files[p].size, p), default=None)
        if name is None:
            raise OSError("No playable media on that share")
        return _FileView(files[name], name.rpartition("/")[2])
    discs = tuple(sorted(found))
    if root is None:
        root = max(discs, key=lambda d: sum(files[n].size for n in found[d][1].values()))
    elif root not in found:
        raise ValueError("The selected disc folder is not on this share")
    return _assemble(found, root, discs, files.__getitem__)


def project_zip(cache, root=None):
    """Present a disc folder stored inside a zip as a UDF image, without unpacking it.

    A zip holding no navigable disc falls back to its main title, served as itself.
    """
    with zipfile.ZipFile(CacheFile(cache)) as archive:
        spans = _stored_spans(cache, archive)
        names = archive.namelist()
    found = _zip_discs(names)
    if not found:
        inside = {name: [span] for name, span in spans.items()}
        view = _nested_image(cache, inside) or _title_view(cache, inside)
        if view:
            return view
        if any(_ext(name) in MEDIA_EXT for name in names):
            raise OSError("That zip is compressed, so it can't be streamed. Extract it first")
        raise OSError("No playable media in that zip")
    discs = tuple(sorted(found))
    if root is None:
        root = max(discs, key=lambda d: sum(spans.get(n, (0, 0))[1] for n in found[d][1].values()))
    elif root not in found:
        raise ValueError("The selected disc folder is not in this image")
    if any(name not in spans for name in found[root][1].values()):
        raise OSError("That zip is compressed, so the disc can't be streamed. Extract it first")
    return _assemble(found, root, discs, lambda name: _ExtentFile(cache, [spans[name]]))
