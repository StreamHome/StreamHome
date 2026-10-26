from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import uuid

from lifecycle import runtime_recovery_cache as cache, runtime_build as runtime
from lifecycle.management_journal import parse_execution_tuple
from tests.lifecycle.test_management_journal import _execution_payload


class BoundRecoveryCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.execution = parse_execution_tuple(_execution_payload())
        self.cache = self.root / 'recovery'
        self.runtime = self.root / 'runtime';self.runtime.mkdir(mode=0o700);self.bundle = self.runtime / 'bundles' / self.execution.runtime_bundle_id
        self.bundle.mkdir(mode=0o700, parents=True)
        self.trees = {}
        for category, identity in (('python', self.execution.python_environment_id), ('node', self.execution.node_environment_id),
                                   ('web', self.execution.web_bundle_id), ('tools', None)):
            directory = self.bundle / 'tools' if identity is None else self.runtime / category / identity
            directory.mkdir(mode=0o700, parents=True)
            directory.parent.chmod(0o700)
            (directory / 'bin').mkdir(mode=0o700)
            # Repeated object bytes are deduplicated between independent trees.
            (directory / 'bin/program').write_bytes(b'exact immutable bytes')
            (directory / 'bin/program').chmod(0o700)
            (directory / 'bin/alias').symlink_to('program')
            self.trees[category] = directory
        self.manifest = {'schema_version': 1, 'bundle_id': self.execution.runtime_bundle_id,
            'python_id': self.execution.python_environment_id, 'node_id': self.execution.node_environment_id,
            'web_id': self.execution.web_bundle_id, 'application_commit': self.execution.application_commit,
            'application_build_id': self.execution.application_build_id,
            **{name: runtime.tree_inventory(directory) for name, directory in self.trees.items()}}
        raw = runtime.canonical_bytes(self.manifest)
        (self.bundle / 'manifest.json').write_bytes(raw)
        (self.bundle / 'manifest.json').chmod(0o600)
        self.execution = replace(self.execution, runtime_manifest_sha256=hashlib.sha256(raw).hexdigest())
        self.checkpoint = Mock()

    def capture(self):
        cache.capture(self.bundle, self.cache, self.execution.runtime_manifest_sha256, checkpoint=self.checkpoint)

    def test_exact_inputs_survive_original_corruption_and_rebuild_only_an_empty_permanent_path(self):
        self.capture()
        self.assertEqual(len(list((self.cache / 'objects').iterdir())), 1)
        self.assertEqual(cache.inspect(self.cache, self.execution, checkpoint=self.checkpoint), self.manifest)
        original = self.trees['python'];evidence = original.with_name('preserved-corrupt-evidence')
        (original / 'bin/program').write_bytes(b'corrupted')
        original.rename(evidence)
        cache.restore_empty_tree(self.cache, original, self.manifest['python'], checkpoint=self.checkpoint)
        self.assertEqual(runtime.tree_inventory(original), self.manifest['python'])
        self.assertEqual((evidence / 'bin/program').read_bytes(), b'corrupted')
        with self.assertRaises(FileExistsError):
            cache.restore_empty_tree(self.cache, original, self.manifest['python'], checkpoint=self.checkpoint)

    def test_manifest_and_cached_object_tampering_are_refused_without_repairing_the_source(self):
        self.capture();source = self.trees['python'] / 'bin/program'
        objects = list((self.cache / 'objects').iterdir());objects[0].write_bytes(b'bad object')
        with self.assertRaises(runtime.RuntimeBuildError):cache.inspect(self.cache, self.execution, checkpoint=self.checkpoint)
        self.assertEqual(source.read_bytes(), b'exact immutable bytes')
        with self.assertRaises(runtime.RuntimeBuildError):self.capture()
        with self.assertRaises(runtime.RuntimeBuildError):
            cache.inspect(self.cache, replace(self.execution, runtime_bundle_id=str(uuid.uuid4())), checkpoint=self.checkpoint)

    def test_symlinked_objects_and_noncanonical_inventory_cannot_escape_the_cache(self):
        self.capture();obj = next((self.cache / 'objects').iterdir());obj.unlink();obj.symlink_to(self.trees['python'] / 'bin/program')
        with self.assertRaises(OSError):cache.inspect(self.cache, self.execution, checkpoint=self.checkpoint)
        for entries in ({'../escape': 'a' * 64}, {'/escape': 'a' * 64}, {'a//b': 'a' * 64},
                        {'a': 'link:elsewhere', 'a/b': 'a' * 64}, {'a': True}):
            target = self.root / str(uuid.uuid4())
            with self.subTest(entries=entries), self.assertRaises(runtime.RuntimeBuildError):
                cache.restore_empty_tree(self.cache, target, entries, checkpoint=self.checkpoint)
            self.assertFalse(target.exists())

    def test_cancellation_retains_original_bytes_and_never_publishes_an_unchecked_object(self):
        self.checkpoint.side_effect = RuntimeError('explicit cancellation')
        with self.assertRaisesRegex(RuntimeError, 'explicit cancellation'):self.capture()
        self.assertEqual((self.trees['python'] / 'bin/program').read_bytes(), b'exact immutable bytes')
        self.assertFalse(any((self.cache / 'objects').iterdir()))

    def test_manifest_named_installed_hardlinks_are_captured_as_independent_single_link_objects(self):
        source = self.trees['web'] / 'bin/program'
        source.write_bytes(b'esbuild-specific platform binary bytes')
        alias = self.trees['web'] / 'bin/second-program';os.link(source, alias)
        self.manifest['web'] = runtime.tree_inventory(self.trees['web'])
        raw = runtime.canonical_bytes(self.manifest);(self.bundle / 'manifest.json').write_bytes(raw)
        self.execution = replace(self.execution, runtime_manifest_sha256=hashlib.sha256(raw).hexdigest())
        self.capture()
        obj = self.cache / 'objects' / hashlib.sha256(source.read_bytes()).hexdigest()
        self.assertEqual(source.stat().st_nlink, 2)
        self.assertEqual(obj.stat().st_nlink, 1)
        self.assertNotEqual(source.stat().st_ino, obj.stat().st_ino)
        illegal = obj.with_name('untrusted-hardlink');os.link(obj, illegal)
        with self.assertRaises(runtime.RuntimeBuildError):cache.inspect(self.cache, self.execution, checkpoint=self.checkpoint)


if __name__ == '__main__':unittest.main()
