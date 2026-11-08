"""Blocks kept on disk between plays, so reopening a disc skips the round trips its menus cost.

One file per block. Least recently used goes first once past the limit; a read touches the file.
"""
import hashlib
import os
import threading
from pathlib import Path

GB = 1 << 30


class DiskCache:
    def __init__(self, root, limit=2 * GB):
        self.root, self.limit = Path(root), limit
        self.lock = threading.Lock()
        self.size = None  # counted on first write, so opening the app costs no scan

    @staticmethod
    def key(url, size, bs):
        return hashlib.sha1(f"{url}|{size}|{bs}".encode()).hexdigest()

    def _path(self, key, index):
        return self.root / key[:2] / f"{key}-{index}"

    def get(self, key, index):
        if not self.limit:
            return None
        path = self._path(key, index)
        try:
            data = path.read_bytes()
            os.utime(path)
            return data
        except OSError:
            return None

    def has(self, key, index):
        return bool(self.limit) and self._path(key, index).is_file()

    def put(self, key, index, data):
        if not self.limit or len(data) > self.limit:
            return
        path = self._path(key, index)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            part = path.with_name(f"{path.name}.{threading.get_ident()}.part")
            part.write_bytes(data)
            os.replace(part, path)
        except OSError:
            return  # a full or read-only disk only costs the cache
        with self.lock:
            self.size = self.used() if self.size is None else self.size + len(data)  # a scan counts it already
            if self.size > self.limit:
                self._evict()

    def _files(self):
        return [f for d in self.root.glob("??") for f in d.iterdir() if not f.name.endswith(".part")]

    def used(self):
        total = 0
        for path in self._files() if self.root.is_dir() else ():
            try:
                total += path.stat().st_size
            except OSError:
                pass
        return total

    def _evict(self):
        """Down to 90% of the limit, oldest first, so the next writes don't each pay for a scan."""
        files = []
        for path in self._files():
            try:
                stat = path.stat()
            except OSError:
                continue  # removed meanwhile
            files.append((stat.st_mtime, stat.st_size, path))
        files.sort(key=lambda f: f[0])
        self.size = sum(size for _, size, _ in files)
        for _, size, path in files:
            if self.size <= self.limit * 0.9:
                break
            path.unlink(missing_ok=True)
            self.size -= size

    def set_limit(self, limit):
        with self.lock:
            self.limit = limit
            if self.size is None:
                self.size = self.used()
            if self.size > limit:
                self._evict()

    def clear(self):
        with self.lock:
            for path in self._files() if self.root.is_dir() else ():
                path.unlink(missing_ok=True)
            self.size = 0
