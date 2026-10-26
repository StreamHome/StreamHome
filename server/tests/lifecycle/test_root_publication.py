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
    def test_cloudflare_publication_has_exact_namespace_and_secret_permissions(self):
        base=Path('/etc/streamhome/control/cloudflare/pending')/str(uuid.uuid4())
        valid=((base/'token',0o600),(base/'metadata.json',0o644),
            (Path('/etc/streamhome/control/cloudflare/active.json'),0o644))
        with tempfile.TemporaryDirectory() as directory:
            for target,mode in valid:
                with self.subTest(target=target),patch.object(root_files.os,'geteuid',return_value=0), \
                        patch.object(root_files,'root_directory',side_effect=lambda _:os.open(directory,os.O_RDONLY|os.O_DIRECTORY)), \
                        patch.object(root_files.os,'fchown'):
                    root_files.atomic_root_bytes(target,b'Synthetic fixture',mode=mode)
                self.assertEqual((Path(directory)/target.name).stat().st_mode&0o777,mode)
        for target,gid,mode in ((base/'token',0,0o644),(base/'token',999,0o600),(base/'metadata.json',0,0o600),
                (base/'command.json',0,0o644),(base/'../token',0,0o600),
                (Path('/etc/streamhome/control/cloudflare/arbitrary.json'),0,0o644),
                (Path('/etc/streamhome/control/cloudflare/active.json'),999,0o644)):
            with self.subTest(target=target,gid=gid,mode=mode),patch.object(root_files.os,'geteuid',return_value=0), \
                    patch.object(root_files,'root_directory') as parent,self.assertRaises(ControlError):
                root_files.atomic_root_bytes(target,b'Synthetic fixture',gid=gid,mode=mode)
            parent.assert_not_called()

    def test_resumption_publication_is_limited_to_one_plan_and_two_named_stages(self):
        base = Path('/etc/streamhome/control/resumption') / str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as directory:
            for name in ('plan.json', 'candidate_start.json', 'previous_start.json', 'resumption-start.json'):
                with self.subTest(name=name), patch.object(root_files.os, 'geteuid', return_value=0), patch.object(root_files, 'root_directory', side_effect=lambda _: os.open(directory, os.O_RDONLY | os.O_DIRECTORY)), patch.object(root_files.os, 'fchown'):
                    target = Path('/etc/streamhome/control/resumption-start.json') if name == 'resumption-start.json' else base / name
                    root_files.atomic_root_json(target, {'schema_version': 1}, gid=999, mode=0o640)
                self.assertEqual((Path(directory) / name).stat().st_mode & 0o777, 0o640)
        for name in ('retry_start.json', 'candidate_start-2.json', 'command.json', '../plan.json'):
            with self.subTest(name=name), patch.object(root_files.os, 'geteuid', return_value=0), patch.object(root_files, 'root_directory') as parent:
                with self.assertRaises(ControlError):root_files.atomic_root_json(base / name, {})
                parent.assert_not_called()

    def test_generation_bound_recovery_files_are_publishable_and_paths_stay_fixed(self):
        operation, review = str(uuid.uuid4()), str(uuid.uuid4())
        base = Path('/etc/streamhome/control/operations') / operation
        with tempfile.TemporaryDirectory() as directory:
            for name in ('recovery-2.json', 'bridge-2.json', 'os-receipt-2.json', 'recovery-review-' + review + '.json',
                    'update-recovery-2.json', 'update-review-'+review+'.json', 'reader.json',
                    'control-artifact.json','control-permission.json','control-stop.json','control-cutover-2.json',
                    'control-repair-reader.json','control-repair-effect-2.json'):
                with self.subTest(name=name), patch.object(root_files.os, 'geteuid', return_value=0), patch.object(root_files, 'root_directory', side_effect=lambda _: os.open(directory, os.O_RDONLY | os.O_DIRECTORY)), patch.object(root_files.os, 'fchown'):
                    root_files.atomic_root_json(base / name, {'schema_version': 1}, gid=999, mode=0o640)
                self.assertEqual((Path(directory) / name).stat().st_mode & 0o777, 0o640)
        for name in ('recovery-0.json', 'recovery-02.json', 'recovery-../../other.json', 'arbitrary-2.json', 'recovery-review-unbound.json',
                'update-recovery-0.json', 'update-recovery-02.json', 'update-review-unbound.json',
                'control-cutover-0.json','control-cutover-02.json','control-cutover-unbound.json','reader-2.json','control-stop-2.json',
                'control-repair-effect-0.json','control-repair-effect-02.json','control-repair-effect-unbound.json',
                'control-repair-reader-2.json','control-repair-command.json'):
            with self.subTest(name=name), patch.object(root_files.os, 'geteuid', return_value=0), patch.object(root_files, 'root_directory') as parent:
                with self.assertRaises(ControlError):
                    root_files.atomic_root_json(base / name, {})
                parent.assert_not_called()

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
