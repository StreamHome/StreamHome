import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from lifecycle.control import root_files
from lifecycle.control.rpc import ControlError
from lifecycle.control.units import unit_files


class RootPublicationBoundaryTests(unittest.TestCase):
    def test_every_publisher_owned_unit_can_be_published_through_the_protected_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def parent(path):
                return os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            for name, raw in unit_files().items():
                with self.subTest(name=name), patch.object(root_files.os, 'geteuid', return_value=0), patch.object(root_files, 'root_directory', side_effect=parent), patch.object(root_files.os, 'fchown'):
                    root_files.atomic_root_bytes(Path(name), raw)
                self.assertEqual((root / Path(name).name).read_bytes(), raw)

    def test_only_the_exact_preparation_request_path_is_writable(self):
        operation = str(uuid.uuid4())
        accepted = Path('/etc/streamhome/control/preparations') / operation / 'request.json'
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(root_files.os, 'geteuid', return_value=0), patch.object(root_files, 'root_directory', side_effect=lambda _: os.open(directory, os.O_RDONLY | os.O_DIRECTORY)), patch.object(root_files.os, 'fchown'):
                root_files.atomic_root_json(accepted, {'schema_version': 1}, gid=999, mode=0o640)
            self.assertEqual((Path(directory) / 'request.json').stat().st_mode & 0o777, 0o640)
        for name in ('/etc/systemd/system/unrelated.service', '/etc/systemd/system/streamhome-prepare@other.service',
                     '/etc/streamhome/control/preparations/unreviewed/request.json', str(accepted.parent / 'arbitrary.json'),
                     str(accepted.parent / '../request.json'), '/tmp/streamhome/control/request.json'):
            with self.subTest(name=name), patch.object(root_files.os, 'geteuid', return_value=0), patch.object(root_files, 'root_directory') as parent:
                with self.assertRaisesRegex(ControlError, 'control_root_destination_invalid'):
                    root_files.atomic_root_json(Path(name), {'schema_version': 1})
                parent.assert_not_called()

    def test_exact_first_start_review_plan_path_is_writable(self):
        review = str(uuid.uuid4())
        accepted = Path('/etc/streamhome/control/first-start-attempts') / (review + '.json')
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(root_files.os, 'geteuid', return_value=0), patch.object(root_files, 'root_directory', side_effect=lambda _: os.open(directory, os.O_RDONLY | os.O_DIRECTORY)), patch.object(root_files.os, 'fchown'):
                root_files.atomic_root_json(accepted, {'schema_version': 1}, gid=999, mode=0o640)
            self.assertEqual((Path(directory) / accepted.name).stat().st_mode & 0o777, 0o640)
        with patch.object(root_files.os, 'geteuid', return_value=0), patch.object(root_files, 'root_directory') as parent:
            with self.assertRaisesRegex(ControlError, 'control_root_destination_invalid'):
                root_files.atomic_root_json(accepted.parent / 'unreviewed.json', {'schema_version': 1})
        parent.assert_not_called()


if __name__ == '__main__':
    unittest.main()
