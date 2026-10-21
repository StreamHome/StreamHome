import hashlib
import fcntl
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from lifecycle.artifact_cache import ArtifactCacheError, cached_artifact


class ArtifactCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cache = self.root / 'artifacts'
        self.payload = b'fixed public archive bytes' * 10000
        self.digest = hashlib.sha256(self.payload).hexdigest()

    def fetch(self, destination):
        with destination.open('xb') as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(self.payload)

    def prepare(self, name, **kwargs):
        destination = self.root / name
        cached_artifact(destination, self.digest, cache_root=self.cache, **kwargs)
        return destination

    def test_verified_hit_works_without_network_and_rechecks_actual_bytes(self):
        first = self.prepare('first', fetch=self.fetch)
        unavailable_network = Mock(side_effect=AssertionError('A verified cache hit must not need the network'))
        second = self.prepare('second', fetch=unavailable_network)
        self.assertEqual(first.read_bytes(), self.payload)
        self.assertEqual(second.read_bytes(), self.payload)
        self.assertEqual(second.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.cache / self.digest).read_bytes(), self.payload)
        unavailable_network.assert_not_called()

    def test_corruption_is_retained_before_exact_pinned_redownload(self):
        self.prepare('first', fetch=self.fetch)
        (self.cache / self.digest).write_bytes(b'corrupted old archive')
        fetch = Mock(side_effect=self.fetch)
        restored = self.prepare('restored', fetch=fetch)
        self.assertEqual(restored.read_bytes(), self.payload)
        self.assertEqual(fetch.call_count, 1)
        evidence = list(self.cache.glob(self.digest + '.corrupt-*'))
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].read_bytes(), b'corrupted old archive')
        self.assertEqual((self.cache / self.digest).read_bytes(), self.payload)

    def test_wrong_download_never_becomes_cached_or_an_executable_input(self):
        def incorrect(path):
            path.write_bytes(b'wrong public archive')
            path.chmod(0o600)
        with self.assertRaises(ArtifactCacheError):
            self.prepare('wrong', fetch=incorrect)
        self.assertFalse((self.root / 'wrong').exists())
        self.assertFalse((self.cache / self.digest).exists())
        self.assertFalse(list(self.cache.glob('.fetch-*')))

    def test_unsafe_aliases_and_hardlinks_are_preserved_and_never_refetched(self):
        self.cache.mkdir(mode=0o700)
        external = self.root / 'external'
        external.write_bytes(self.payload)
        external.chmod(0o600)
        entry = self.cache / self.digest
        fetch = Mock()
        entry.symlink_to(external)
        with self.assertRaises((OSError, ArtifactCacheError)):
            self.prepare('linked', fetch=fetch)
        self.assertTrue(entry.is_symlink())
        self.assertFalse((self.root / 'linked').exists())
        entry.unlink()
        os.link(external, entry)
        with self.assertRaises(ArtifactCacheError):
            self.prepare('hardlinked', fetch=fetch)
        self.assertEqual(entry.stat().st_nlink, 2)
        self.assertEqual(external.read_bytes(), self.payload)
        fetch.assert_not_called()

    def test_stop_during_copy_discards_only_its_partial_destination(self):
        self.prepare('first', fetch=self.fetch)
        destination = self.root / 'cancelled'
        fetch = Mock()
        def cancelled():
            if destination.exists():
                raise RuntimeError('operator stopped')
        with self.assertRaisesRegex(RuntimeError, 'operator stopped'):
            self.prepare('cancelled', fetch=fetch, checkpoint=cancelled)
        self.assertFalse(destination.exists())
        self.assertEqual((self.cache / self.digest).read_bytes(), self.payload)
        fetch.assert_not_called()
        self.assertEqual(self.prepare('after-stop', fetch=fetch).read_bytes(), self.payload)

    def test_existing_destination_is_not_overwritten(self):
        destination = self.root / 'existing'
        destination.write_bytes(b'preserve unrelated file')
        fetch = Mock()
        with self.assertRaises(ArtifactCacheError):
            self.prepare('existing', fetch=fetch)
        self.assertEqual(destination.read_bytes(), b'preserve unrelated file')
        self.assertFalse(self.cache.exists())
        fetch.assert_not_called()

    def test_stop_while_another_builder_owns_hash_lock_does_not_fetch_or_copy(self):
        self.prepare('first', fetch=self.fetch)
        lock = os.open(self.cache / (self.digest + '.lock'), os.O_RDWR)
        calls = []
        fetch = Mock()
        def cancelled():
            calls.append(True)
            if len(calls) >= 4:
                raise RuntimeError('operator stopped')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, 'operator stopped'):
                self.prepare('waiting', fetch=fetch, checkpoint=cancelled)
        finally:
            os.close(lock)
        self.assertFalse((self.root / 'waiting').exists())
        self.assertEqual((self.cache / self.digest).read_bytes(), self.payload)
        fetch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
