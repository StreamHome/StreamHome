from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import uuid


PROJECT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('streamhome_bootstrap_frontend_test', PROJECT/'scripts/bootstrap_install.py')
frontend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(frontend)


def release_payload():
    return {'schema_version': 1, 'format': 'streamhome-control-bootstrap-deb-v1',
            'bundle_id': str(uuid.uuid4()), 'manifest_sha256': 'a'*64,
            'artifact': 'streamhome-control-bootstrap.deb', 'artifact_sha256': 'b'*64,
            'publisher_sha256': frontend.PUBLISHER_SHA256, 'entry_sha256': 'c'*64}


class BootstrapFrontendTests(unittest.TestCase):
    def test_signed_metadata_requires_fixed_key_format_and_artifact_destination(self):
        value = release_payload()
        self.assertEqual(frontend.decode_release(json.dumps(value).encode()), value)
        for changes in ({'artifact': '/tmp/unreviewed.deb'}, {'schema_version': True}, {'publisher_sha256': 'f'*64},
                        {'bundle_id': 'main'}, {'artifact_sha256': 'bad'}, {'post_install_command': 'anything'}):
            with self.subTest(changes=changes), self.assertRaises(frontend.BootstrapError):
                frontend.decode_release(json.dumps({**value, **changes}).encode())
        for raw in (b'[]', b'{"a":1,"a":2}', b'{"a":NaN}', b' ' * 8193):
            with self.subTest(raw=raw[:30]), self.assertRaises(frontend.BootstrapError):
                frontend.decode_release(raw)

    def test_existing_installation_retry_does_not_download_install_or_auto_resume(self):
        with patch.object(frontend.os, 'geteuid', return_value=1000), patch.object(frontend, 'require_platform'), patch.object(frontend, 'existing_evidence', return_value=True), patch.object(frontend, 'protected_entry', return_value=True), patch.object(frontend, 'download') as download, patch.object(frontend, 'install_verified_control') as install, patch.object(frontend, 'run', return_value=b'') as run:
            self.assertEqual(frontend.main(['--request','/tmp/ignored-new-install-request.json']), 0)
        download.assert_not_called()
        install.assert_not_called()
        self.assertEqual(run.call_args.args[0], ['/usr/bin/python3','-I',str(frontend.ENTRY),'--install'])
        self.assertNotIn('--resume-install', run.call_args.args[0])

    def test_root_bootstrap_refuses_before_dependencies_artifacts_or_custom_root_code(self):
        with patch.object(frontend.os, 'geteuid', return_value=0), patch.object(frontend, 'require_platform') as platform_check, patch.object(frontend, 'run') as run:
            with self.assertRaises(frontend.BootstrapError):
                frontend.main([])
        platform_check.assert_not_called()
        run.assert_not_called()

    def test_legacy_discovery_only_reports_and_never_executes_checkout(self):
        with patch.object(frontend.os, 'geteuid', return_value=1000), patch.object(frontend, 'require_platform'), patch.object(frontend, 'existing_evidence', return_value=False), patch.object(frontend, 'protected_entry', return_value=False), patch.object(frontend, 'legacy_hints', return_value=(Path('/legacy/StreamHome'),)), patch.object(frontend, 'inspect_existing_legacy') as inspect, patch.object(frontend, 'run') as run, patch.object(frontend, 'download') as download:
            self.assertEqual(frontend.main([]), 0)
        inspect.assert_called_once_with((Path('/legacy/StreamHome'),))
        run.assert_not_called()
        download.assert_not_called()

    def test_existing_evidence_without_trusted_entry_never_enters_fresh_path(self):
        with patch.object(frontend.os, 'geteuid', return_value=1000), patch.object(frontend, 'require_platform'), patch.object(frontend, 'existing_evidence', return_value=True), patch.object(frontend, 'protected_entry', return_value=False), patch.object(frontend, 'download') as download:
            with self.assertRaises(frontend.BootstrapError):
                frontend.main([])
        download.assert_not_called()

    def test_control_package_without_registration_does_not_override_legacy_evidence(self):
        with patch.object(frontend.os, 'geteuid', return_value=1000), patch.object(frontend, 'require_platform'), patch.object(frontend, 'existing_evidence', return_value=False), patch.object(frontend, 'protected_entry', return_value=True), patch.object(frontend, 'legacy_hints', return_value=(Path('/legacy/StreamHome'),)), patch.object(frontend, 'inspect_existing_legacy'), patch.object(frontend, 'run') as run, patch.object(frontend, 'download') as download:
            self.assertEqual(frontend.main([]), 0)
        run.assert_not_called()
        download.assert_not_called()

    def test_legacy_release_check_executes_only_authenticated_unprivileged_reader(self):
        def run(arguments, **kwargs):
            self.assertFalse(kwargs.get('privileged', False))
            self.assertEqual(arguments[:3], ['/usr/bin/python3','-I','-c'])
            self.assertIn('lifecycle.control.legacy_inspection', arguments[3])
            self.assertEqual(arguments[-1], '/legacy/StreamHome')
            return b'{"status":"up_to_date","reason":"The installed commit matches the configured official release."}'
        with patch.object(frontend, 'download') as download, patch.object(frontend, 'verify_download', return_value=release_payload()), patch.object(frontend, 'extracted_library', return_value=Path('/authenticated/package/lib')), patch.object(frontend, 'run', side_effect=run), patch.object(frontend, 'install_verified_control') as install:
            frontend.inspect_existing_legacy((Path('/legacy/StreamHome'),))
        self.assertEqual(download.call_count, len(frontend.LIMITS))
        install.assert_not_called()

    def test_fresh_install_cancellation_is_before_any_privileged_program(self):
        with patch.object(frontend.os, 'geteuid', return_value=1000), patch.object(frontend, 'require_platform'), patch.object(frontend, 'existing_evidence', return_value=False), patch.object(frontend, 'protected_entry', return_value=False), patch.object(frontend, 'legacy_hints', return_value=()), patch.object(frontend, 'download'), patch.object(frontend, 'verify_download', return_value=release_payload()), patch.object(frontend, 'review_installation', return_value=None), patch.object(frontend, 'install_verified_control') as install, patch.object(frontend, 'run') as run:
            self.assertEqual(frontend.main([]), 0)
        install.assert_not_called()
        run.assert_not_called()

    def test_publisher_signature_and_downloaded_package_are_verified_with_real_openssl(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            key = directory/'fixture-only-private.pem'
            subprocess.run(['/usr/bin/openssl','genpkey','-algorithm','ED25519','-out',str(key)],check=True,capture_output=True)
            subprocess.run(['/usr/bin/openssl','pkey','-in',str(key),'-pubout','-out',str(directory/'publisher.pem')],check=True,capture_output=True)
            payload = release_payload()
            artifact = directory/payload['artifact']
            artifact.write_bytes(b'fixture-package-data-only')
            payload['publisher_sha256'] = hashlib.sha256((directory/'publisher.pem').read_bytes()).hexdigest()
            payload['artifact_sha256'] = hashlib.sha256(artifact.read_bytes()).hexdigest()
            (directory/'release.json').write_text(json.dumps(payload))
            subprocess.run(['/usr/bin/openssl','pkeyutl','-sign','-rawin','-inkey',str(key),'-in',str(directory/'release.json'),'-out',str(directory/'release.sig')],check=True,capture_output=True)
            with patch.object(frontend, 'PUBLISHER_SHA256', payload['publisher_sha256']):
                self.assertEqual(frontend.verify_download(directory), payload)
                artifact.write_bytes(b'changed-package')
                with self.assertRaises(frontend.BootstrapError):
                    frontend.verify_download(directory)
                artifact.write_bytes(b'fixture-package-data-only')
                (directory/'release.json').write_text(json.dumps({**payload,'entry_sha256':'d'*64}))
                with self.assertRaises(frontend.BootstrapError):
                    frontend.verify_download(directory)

    def test_privileged_install_rechecks_root_copies_before_dpkg_or_custom_code(self):
        payload = release_payload()
        calls = []
        def run(arguments, **kwargs):
            self.assertTrue(kwargs.get('privileged'))
            calls.append(arguments)
            if arguments[0] == '/usr/bin/sha256sum':
                return ((payload['publisher_sha256'] if arguments[1].endswith('/publisher.pem') else '0'*64)+'  file\n').encode()
            if arguments[0] == '/usr/bin/cat':
                return json.dumps(payload).encode()
            return b''
        with patch.object(frontend, 'run', side_effect=run), self.assertRaises(frontend.BootstrapError):
            frontend.install_verified_control(Path('/unprivileged/staging'), payload)
        programs = [call[0] for call in calls]
        self.assertIn('/usr/bin/openssl', programs)
        self.assertNotIn('/usr/bin/dpkg', programs)
        self.assertNotIn('/usr/bin/python3', programs)
        self.assertTrue(all(call[0] in {'/usr/bin/install','/usr/bin/sha256sum','/usr/bin/openssl','/usr/bin/cat'} for call in calls))

    def test_shell_frontend_pins_the_complete_python_frontend(self):
        digest = hashlib.sha256((PROJECT/'scripts/bootstrap_install.py').read_bytes()).hexdigest()
        wrapper = (PROJECT/'install.sh').read_text()
        self.assertIn("'"+digest+"'", wrapper)
        self.assertIn('/usr/bin/python3 -I', wrapper)
        self.assertNotIn('sudo bash', wrapper)
        result = subprocess.run(['/usr/bin/bash','-n',str(PROJECT/'install.sh')],capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr.decode())


if __name__ == '__main__':
    unittest.main()
