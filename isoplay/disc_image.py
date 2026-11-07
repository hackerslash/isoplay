"""Present a disc buried in an ISO9660 image or a zip as a seekable UDF image.

libbluray reads UDF, not ISO9660 or zip, and libdvdread wants VIDEO_TS at the root.
Only the chosen disc's filesystem metadata is generated, in memory; the video stays
in the upstream cache and is never copied.
"""
import bisect
import io
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
        # Files never overlap metadata pages. Serialize seek+read because each
        # file reference keeps a cursor shared by concurrent HTTP readers.
        with self.lock:
            i = max(0, bisect.bisect_right(self.starts, pos) - 1)
            for start, length, fp in self.files[i:]:
                if start >= end:
                    break
                a, b = max(pos, start), min(end, start + length)
                if a < b:
                    fp.seek(a - start)
                    data = fp.read(b - a)
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

    def read(self, size=-1):
        if size < 0:
            size = self.size - self.pos
        size = min(size, self.size - self.pos)
        out = bytearray()
        while size > 0:
            i = bisect.bisect_right(self.starts, self.pos) - 1
            offset, length = self.parts[i]
            within = self.pos - self.starts[i]
            count = min(size, length - within)
            if count >= 1 << 20 and hasattr(self.cache, "prefetch"):
                self.cache.prefetch(offset + within)
            data = self.cache.read(offset + within, count)
            if len(data) != count:
                raise OSError("short read in disc extent")
            out += data
            self.pos += count
            size -= count
        return bytes(out)


def project_iso9660(cache, root=None):
    """Return a UDF view for an ISO9660-only Blu-ray, or None for other images.

    Default to the disc with the most stream data; root selects a different
    disc folder. Expose all choices on the view. No media payload is fetched.
    """
    original = pycdlib.PyCdlib()
    original.open_fp(CacheFile(cache))
    try:
        if original.has_udf():
            return None
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
                    files.setdefault(child, []).append(record)
        streams = {path: _ExtentFile(cache, [(r.extent_location() * 2048, r.data_length) for r in records])
                   for path, records in files.items()}
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
    finally:
        original.close()


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


def project_zip(cache, root=None):
    """Present a disc folder stored inside a zip as a UDF image, without unpacking it."""
    with zipfile.ZipFile(CacheFile(cache)) as archive:
        spans = _stored_spans(cache, archive)
        found = _zip_discs(archive.namelist())
    if not found:
        raise OSError("No DVD or Blu-ray in that zip")
    discs = tuple(sorted(found))
    if root is None:
        root = max(discs, key=lambda d: sum(spans.get(n, (0, 0))[1] for n in found[d][1].values()))
    elif root not in found:
        raise ValueError("The selected disc folder is not in this image")
    kind, layout = found[root]
    if any(name not in spans for name in layout.values()):
        raise OSError("That zip is compressed, so the disc can't be streamed. Extract it first")
    streams = {path: _ExtentFile(cache, [spans[name]]) for path, name in layout.items()}
    directories = set()
    for path in streams:
        parent = path.rpartition("/")[0]
        while parent:
            directories.add(parent)
            parent = parent.rpartition("/")[0]
    return _build_udf(streams, sorted(directories), discs, root, kind)
