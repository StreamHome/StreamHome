import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lifecycle.control import progress


class DisplayProgressTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'preparation').mkdir(mode=0o700)
        self.path = self.root / 'preparation/progress.json'
        self.path.write_text(json.dumps({'stage': progress.PREPARATION_STAGES[0], 'status': 'running'}))
        self.path.chmod(0o600)
        self.fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.fd)

    def read(self):
        return progress.read_at(self.fd, os.geteuid(), ('preparation', 'progress.json'))

    def test_only_fixed_display_labels_are_returned(self):
        self.assertEqual(self.read(), {'stage': progress.PREPARATION_STAGES[0], 'status': 'running'})
        for value in ({'stage': '\x1b[2Jprivate arbitrary text', 'status': 'done'},
                      {'stage': progress.PREPARATION_STAGES[0], 'status': 'running', 'accepted': True},
                      {'stage': progress.PREPARATION_STAGES[0], 'status': ['done']}):
            with self.subTest(value=value):
                self.path.write_text(json.dumps(value))
                self.assertIsNone(self.read())

    def test_file_and_parent_aliases_never_expose_other_contents(self):
        outside = self.root / 'private-unrelated'
        outside.write_text('unrelated content must not be read')
        self.path.unlink()
        self.path.symlink_to(outside)
        with patch.object(progress.os, 'read') as read:
            self.assertIsNone(self.read())
            read.assert_not_called()
        self.path.unlink()
        (self.root / 'preparation').rmdir()
        (self.root / 'preparation').symlink_to(self.root, target_is_directory=True)
        with patch.object(progress.os, 'read') as read:
            self.assertIsNone(self.read())
            read.assert_not_called()

    def test_wrong_owner_hardlink_fifo_or_oversize_is_rejected_before_read(self):
        with patch.object(progress.os, 'read') as read:
            self.assertIsNone(progress.read_at(self.fd, os.geteuid() + 1, ('preparation', 'progress.json')))
            read.assert_not_called()
        os.link(self.path, self.root / 'extra-link')
        with patch.object(progress.os, 'read') as read:
            self.assertIsNone(self.read())
            read.assert_not_called()
        self.path.unlink()
        os.mkfifo(self.path, mode=0o600)
        with patch.object(progress.os, 'read') as read:
            self.assertIsNone(self.read())
            read.assert_not_called()
        self.path.unlink()
        self.path.write_bytes(b'x' * 513)
        self.path.chmod(0o600)
        with patch.object(progress.os, 'read') as read:
            self.assertIsNone(self.read())
            read.assert_not_called()

    def test_duplicate_fields_and_file_replacement_cannot_supply_progress(self):
        self.path.write_bytes(b'{"stage":"first","stage":"second","status":"done"}')
        self.assertIsNone(self.read())
        value = json.dumps({'stage': progress.PREPARATION_STAGES[0], 'status': 'done'}).encode()
        original_read = os.read
        def replaced(fd, length):
            raw = original_read(fd, length)
            next_path = self.path.with_name('next.json')
            next_path.write_bytes(value)
            next_path.chmod(0o600)
            next_path.replace(self.path)
            return raw
        self.path.write_bytes(value)
        with patch.object(progress.os, 'read', side_effect=replaced):
            self.assertIsNone(self.read())

    def test_unknown_operation_does_not_open_any_system_path(self):
        for value in (None, '../unrelated', '00000000-0000-0000-0000-000000000000'):
            with self.subTest(value=value), patch.object(progress, 'root_directory') as directory:
                self.assertIsNone(progress.read_display_progress('repair', os.geteuid(), value))
                directory.assert_not_called()


if __name__ == '__main__':
    unittest.main()
