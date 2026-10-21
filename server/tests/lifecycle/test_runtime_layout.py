from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from lifecycle import installation, layout, state
from lifecycle.restore_controller import restore_paths


class RuntimeLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name)
        self.root = self.parent / 'application'
        self.root.mkdir()
        (self.root / 'server').mkdir()
        for name in ('start.sh', 'update.sh'):
            (self.root / name).write_text('#!/bin/sh\n')
        self.paths = installation.ManagedPaths(
            application=self.root, configuration=self.parent/'etc', state=self.parent/'state',
            cache=self.parent/'cache', runtime=self.parent/'run',
        )
        self.descriptor = installation.InstallationDescriptor(
            str(uuid.uuid4()), installation.InstallationStatus.READY, os.getuid() or 1000, os.getgid() or 1000, self.paths,
        )
        self.snapshot = installation.DescriptorSnapshot(self.descriptor, 'a'*64)

    def test_legacy_checkout_does_not_consult_an_unrelated_global_descriptor(self):
        with patch.object(installation, 'inspect_installation', side_effect=AssertionError('must not read global descriptor')):
            selected = layout.layout_for_root(self.root)
        self.assertFalse(selected.managed)
        self.assertEqual(selected.database, self.root/'server/database.db')
        self.assertEqual(selected.lifecycle, self.root/'.run/lifecycle')

    def test_managed_runtime_and_offline_restore_share_durable_database_paths(self):
        with patch.object(installation, 'STANDARD_PATHS', self.paths), patch.object(installation, 'inspect_installation', return_value=self.snapshot):
            selected = layout.layout_for_root(self.root)
            canonical = state.lifecycle_paths(self.root)
            restore = restore_paths(self.root, str(uuid.uuid4()))
        self.assertTrue(selected.managed)
        self.assertEqual(selected.database, self.paths.state/'database.db')
        self.assertEqual(restore.database_path, selected.database)
        self.assertEqual(restore.backup_dir, selected.backup)
        self.assertEqual(canonical.lifecycle_dir, self.paths.state/'lifecycle')
        self.assertEqual(selected.runtime, self.paths.runtime)
        self.assertNotEqual(selected.runtime, canonical.run_dir)
        self.assertFalse(self.paths.state.exists())

    def test_managed_root_without_valid_descriptor_never_becomes_legacy(self):
        with patch.object(installation, 'STANDARD_PATHS', self.paths):
            for snapshot in (None, replace(self.snapshot, descriptor=replace(self.descriptor, paths=replace(self.paths, application=Path('/wrong'))))):
                with patch.object(installation, 'inspect_installation', return_value=snapshot), self.assertRaises(layout.LayoutError):
                    layout.layout_for_root(self.root)
            with patch.object(installation, 'inspect_installation', return_value=None), self.assertRaises(state.InvalidRootError):
                state.lifecycle_paths(self.root)

    def test_alias_root_is_rejected_without_creating_any_state(self):
        alias = self.parent/'alias'
        alias.symlink_to(self.root)
        with self.assertRaises(layout.LayoutError):
            layout.layout_for_root(alias)


if __name__ == '__main__':
    unittest.main()
