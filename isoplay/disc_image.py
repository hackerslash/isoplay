"""Present ISO9660 Blu-ray folders as a seekable UDF image, without copying video.

libbluray reads UDF images and BDMV directories, not ISO9660 containers. Some
downloads also wrap multiple discs in folders. Keep their bytes in the upstream
cache and generate only the selected disc's filesystem metadata in memory.
"""
import bisect
import io
import threading

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
    """One ISO9660 file, including every Level 3 multi-extent record."""

    def __init__(self, cache, records):
        self.cache, self.pos = cache, 0
        self.parts, self.starts, self.size = [], [], 0
        for record in records:
            offset = record.extent_location() * 2048
            length = record.data_length
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
                name = record.file_identifier().decode("ascii").split(";", 1)[0]
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
        streams = {path: _ExtentFile(cache, records) for path, records in files.items()}
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
        udf = _MappedUDF()
        udf.new(interchange_level=3, udf="2.60", vol_ident="ISOPLAY")
        for path in sorted(directories, key=lambda p: (p.count("/"), p)):
            if path.startswith(root + "/"):
                udf.add_directory(udf_path=path[len(root):])
        handles = []
        for path, fp in sorted(streams.items()):
            if path.startswith(root + "/"):
                handles.append(fp)
                udf.add_fp(fp, fp.size, udf_path=path[len(root):])
        view = _MetadataImage()
        udf.write_fp(view)
        view.files.sort(key=lambda entry: entry[0])
        view.starts = [entry[0] for entry in view.files]
        view.udf, view.handles = udf, handles
        view.discs, view.disc_root = discs, root
        return view
    finally:
        original.close()
