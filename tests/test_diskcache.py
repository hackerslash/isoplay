"""Blocks kept on disk between plays."""
import os
import tempfile
import time
import unittest

from isoplay import media
from isoplay.diskcache import DiskCache


class Upstream:
    throttles, closed = 0, False

    def __init__(self):
        self.calls = 0

    def get(self, start, end):
        self.calls += 1
        return media._Body(206, {}, bytes([start % 251]) * (end - start + 1))

    def close(self):
        pass


K = DiskCache.key("http://host/disc.iso", 100, 4)


class DiskCacheTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def test_blocks_round_trip_and_the_oldest_go_first_past_the_limit(self):
        cache = DiskCache(self.root, limit=10)
        cache.put(K, 0, b"aaaa")
        cache.put(K, 1, b"bbbb")
        old = time.time() - 60
        os.utime(cache._path(K, 0), (old, old))
        cache.put(K, 2, b"cccc")  # 12 bytes: over, so down to 90%
        self.assertIsNone(cache.get(K, 0))
        self.assertEqual(cache.get(K, 2), b"cccc")
        self.assertLessEqual(cache.used(), 10)

    def test_shrinking_before_any_write_still_frees_the_disk(self):
        DiskCache(self.root, limit=1 << 20).put(K, 0, b"x" * 1000)
        fresh = DiskCache(self.root, limit=1 << 20)  # a new launch: nothing counted yet
        fresh.set_limit(0)
        self.assertEqual(fresh.used(), 0)

    def test_off_and_clear_leave_nothing(self):
        cache = DiskCache(self.root, limit=1 << 20)
        cache.put(K, 0, b"data")
        cache.clear()
        self.assertEqual(cache.used(), 0)
        cache.set_limit(0)
        cache.put(K, 0, b"data")
        self.assertIsNone(cache.get(K, 0))

    def test_a_reopened_file_reads_menu_runs_but_not_a_film(self):
        media.DISK = DiskCache(self.root, limit=1 << 30)
        self.addCleanup(setattr, media, "DISK", None)
        mib = 1 << 20
        blocks = media.Blocks("http://host/disc.iso", size=256 * mib, upstream=Upstream(), bs=mib, ahead=64)
        data = blocks.read(0, mib)
        for i in range(1, 48):  # a reader streaming on in order, read-ahead behind it
            blocks.prefetch(i * mib)
            blocks.block(i).result()
        blocks.pool.shutdown(wait=True)
        blocks.close()
        again = Upstream()
        reopened = media.Blocks("http://host/disc.iso", size=256 * mib, upstream=again, bs=mib)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.read(0, mib), data)
        self.assertEqual(again.calls, 0)
        key = reopened.disk_key
        self.assertTrue(all(media.DISK.has(key, i) for i in range(media.KEEP_RUN // mib)))  # a menu clip's run
        self.assertFalse(any(media.DISK.has(key, i) for i in range(48, 64)))  # a film's read-ahead stays in RAM


if __name__ == "__main__":
    unittest.main()
