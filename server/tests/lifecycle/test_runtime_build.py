from pathlib import Path
import hashlib
import io
import urllib.error
import os
import select
import sys
import tarfile
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import uuid

from lifecycle import runtime_build as runtime


class RuntimeBuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_download_identifies_client_and_verifies_bytes_without_inherited_proxy(self):
        payload = b'pinned runtime package'
        response = io.BytesIO(payload)
        response.url = 'https://downloads.example.invalid/package'
        with patch.object(runtime.urllib.request, 'build_opener') as build_opener:
            build_opener.return_value.open.return_value = response
            destination = self.root/'package'
            runtime.download(response.url, destination, hashlib.sha256(payload).hexdigest())
            request = build_opener.return_value.open.call_args.args[0]
            self.assertEqual(request.get_header('User-agent'), 'StreamHome-control/1')
            self.assertEqual(build_opener.call_args.args[0].proxies, {})
        self.assertEqual(destination.read_bytes(), payload)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

    def test_cancelled_download_discards_partial_artifact_and_never_retries(self):
        response = io.BytesIO(b'verified package')
        response.url = 'https://downloads.example.invalid/package'
        calls = []
        def checkpoint():
            calls.append(True)
            if len(calls) == 3:
                raise RuntimeError('review cancelled')
        destination = self.root / 'cancelled'
        with patch.object(runtime.urllib.request, 'build_opener') as opener:
            opener.return_value.open.return_value = response
            with self.assertRaisesRegex(RuntimeError, 'review cancelled'):
                runtime.download(response.url, destination, hashlib.sha256(b'verified package').hexdigest(), checkpoint=checkpoint)
        self.assertEqual(opener.return_value.open.call_count, 1)
        self.assertFalse(destination.exists())

    def test_cancelled_package_stage_kills_and_reaps_its_exact_owned_child(self):
        marker = self.root / 'builder-pid'
        child = []
        def checkpoint():
            if marker.exists():
                child.append(os.pidfd_open(int(marker.read_text())))
                raise RuntimeError('review cancelled')
        script = 'import os,time;from pathlib import Path;Path('+repr(str(marker))+').write_text(str(os.getpid()));time.sleep(30)'
        try:
            with self.assertRaisesRegex(RuntimeError, 'review cancelled'):
                runtime.run_stage([sys.executable, '-c', script], cwd=self.root, environment=runtime.clean_environment(self.root),
                    log=self.root / 'stage.log', checkpoint=checkpoint)
            self.assertEqual(len(child), 1)
            self.assertEqual(select.select(child, [], [], 1)[0], child)
        finally:
            for descriptor in child:
                os.close(descriptor)

    def test_cancelled_stage_does_not_create_log_or_spawn_an_executable(self):
        with patch.object(runtime.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(RuntimeError, 'review cancelled'):
                runtime.run_stage([sys.executable, '-c', 'raise SystemExit(0)'], cwd=self.root, environment={},
                    log=self.root / 'not-created.log', checkpoint=lambda: (_ for _ in ()).throw(RuntimeError('review cancelled')))
        spawn.assert_not_called()
        self.assertFalse((self.root / 'not-created.log').exists())

    def test_download_removes_unverified_oversized_or_downgraded_artifact(self):
        for name, url, expected, limit in (
            ('corrupted', 'https://downloads.example.invalid/package', '0'*64, 100),
            ('oversized', 'https://downloads.example.invalid/package', hashlib.sha256(b'package').hexdigest(), 3),
            ('downgraded', 'http://downloads.example.invalid/package', hashlib.sha256(b'package').hexdigest(), 100),
        ):
            with self.subTest(name=name):
                response = io.BytesIO(b'package')
                response.url = url
                with patch.object(runtime.urllib.request, 'build_opener') as build_opener:
                    build_opener.return_value.open.return_value = response
                    destination = self.root/name
                    with self.assertRaises(runtime.RuntimeBuildError):
                        runtime.download('https://downloads.example.invalid/package', destination, expected, limit=limit)
                    self.assertFalse(destination.exists())

    def test_transient_transfer_retry_discards_partial_bytes_and_resets_digest(self):
        partial, complete = io.BytesIO(b''), io.BytesIO(b'verified package')
        partial.url = complete.url = 'https://downloads.example.invalid/package'
        with patch.object(partial,'read1',side_effect=[b'untrusted partial prefix',TimeoutError]), patch.object(runtime.urllib.request,'build_opener') as opener:
            opener.return_value.open.side_effect=[partial,complete]
            destination=self.root/'package'
            runtime.download(complete.url,destination,hashlib.sha256(b'verified package').hexdigest())
            self.assertEqual(opener.return_value.open.call_count,2)
        self.assertEqual(destination.read_bytes(),b'verified package')

    def test_transport_retries_are_finite_and_permanent_http_refusal_is_not_retried(self):
        for error,attempts in ((TimeoutError(),3),(urllib.error.HTTPError('https://downloads.example.invalid/package',403,'refused',{},None),1)):
            with self.subTest(error=type(error).__name__), patch.object(runtime.urllib.request,'build_opener') as opener:
                opener.return_value.open.side_effect=error
                destination=self.root/'package'
                with self.assertRaises(runtime.RuntimeBuildError):
                    runtime.download('https://downloads.example.invalid/package',destination,'a'*64)
                self.assertEqual(opener.return_value.open.call_count,attempts)
                self.assertFalse(destination.exists())

    def test_slow_trickle_uses_available_bytes_and_obeys_shared_attempt_deadline(self):
        clock=[0.0]
        response=io.BytesIO(b'package')
        response.url='https://downloads.example.invalid/package'
        def next_bytes(size):
            clock[0]=901.0
            return b'p'
        with patch.object(runtime.time,'monotonic',side_effect=lambda:clock[0]), patch.object(response,'read1',side_effect=next_bytes), patch.object(response,'read',side_effect=AssertionError('read(n) must not refill until n bytes arrive')), patch.object(runtime.urllib.request,'build_opener') as opener:
            opener.return_value.open.return_value=response
            destination=self.root/'package'
            with self.assertRaises(runtime.RuntimeBuildError):
                runtime.download(response.url,destination,hashlib.sha256(b'package').hexdigest())
            self.assertEqual(opener.return_value.open.call_count,1)
            self.assertFalse(destination.exists())

    def test_truncated_content_length_is_retried_before_verification(self):
        short,complete=io.BytesIO(b'short'),io.BytesIO(b'package')
        short.url=complete.url='https://downloads.example.invalid/package'
        short.length=5
        with patch.object(runtime.urllib.request,'build_opener') as opener:
            opener.return_value.open.side_effect=[short,complete]
            destination=self.root/'package'
            runtime.download(complete.url,destination,hashlib.sha256(b'package').hexdigest())
            self.assertEqual(opener.return_value.open.call_count,2)
        self.assertEqual(destination.read_bytes(),b'package')

    def test_package_manager_environment_does_not_inherit_production_omit_or_injection(self):
        with patch.dict(os.environ, {'NODE_ENV': 'production', 'NODE_OPTIONS': '--require /untrusted.js', 'PYTHONPATH': '/untrusted', 'npm_config_omit': 'dev', 'NPM_CONFIG_PREFIX': '/usr'}):
            environment = runtime.clean_environment(self.root, self.root/'node/bin')
        self.assertNotIn('NODE_ENV', environment)
        self.assertNotIn('NODE_OPTIONS', environment)
        self.assertNotIn('npm_config_omit', environment)
        self.assertNotIn('PYTHONPATH', environment)
        self.assertEqual(environment['npm_config_userconfig'], str(self.root/'user.npmrc'))
        self.assertEqual(environment['npm_config_globalconfig'], str(self.root/'global.npmrc'))
        self.assertEqual(environment['PATH'], str(self.root/'node/bin') + ':/usr/bin:/bin')

    def test_inventory_detects_changes_and_rejects_escaping_links_and_special_files(self):
        (self.root/'input').write_text('first')
        first = runtime.tree_inventory(self.root)
        (self.root/'input').write_text('second')
        self.assertNotEqual(first, runtime.tree_inventory(self.root))
        (self.root/'outside').symlink_to('/etc/passwd')
        with self.assertRaises(runtime.RuntimeBuildError):
            runtime.tree_inventory(self.root)
        (self.root/'outside').unlink()
        os.mkfifo(self.root/'fifo')
        with self.assertRaises(runtime.RuntimeBuildError):
            runtime.tree_inventory(self.root)

    def test_tar_rejects_traversal_before_extracting(self):
        archive = self.root/'package.tar.xz'
        with tarfile.open(archive, 'w:xz') as package:
            entry = tarfile.TarInfo(f'node-v{runtime.NODE_VERSION}-linux-x64/../../outside')
            package.addfile(entry)
        destination = self.root/'node'
        destination.mkdir()
        with self.assertRaises(runtime.RuntimeBuildError):
            runtime._node_archive(archive, destination)
        self.assertEqual(list(destination.iterdir()), [])

    def test_generation_never_accepts_paths_zero_or_noncanonical_uuid(self):
        self.assertEqual(runtime.generation(value := str(uuid.uuid4())), value)
        for value in ('../other', '00000000-0000-0000-0000-000000000000', str(uuid.uuid4()).upper(), None):
            with self.assertRaises(runtime.RuntimeBuildError):
                runtime.generation(value)

    def test_source_snapshot_detects_dirty_inputs_without_running_repository_hooks(self):
        names = ('server/main.py', 'server/requirements.txt', 'server/requirements.lock', 'web/package.json', 'web/package-lock.json')
        for name in names:
            path = self.root/name
            path.parent.mkdir(exist_ok=True)
            path.write_text('synthetic committed input')
        environment = runtime.clean_environment(self.root)
        git = ['/usr/bin/git', '-c', 'core.hooksPath=/dev/null']
        subprocess.run(git + ['init', '-q'], cwd=self.root, env=environment, check=True)
        subprocess.run(git + ['add', '--all'], cwd=self.root, env=environment, check=True)
        subprocess.run(git + ['-c', 'user.name=Runtime test', '-c', 'user.email=runtime@example.invalid', 'commit', '-qm', 'test: source snapshot fixture'], cwd=self.root, env=environment, check=True)
        commit = subprocess.check_output(git + ['rev-parse', 'HEAD'], cwd=self.root, env=environment, text=True).strip()
        self.assertEqual(set(runtime.source_inputs(self.root, commit)), set(names))
        subprocess.run(git + ['config', 'core.fsmonitor', str(self.root/'untrusted-hook')], cwd=self.root, env=environment, check=True)
        (self.root/'untrusted-hook').write_text('#!/bin/sh\ntouch marker-must-not-exist\n')
        (self.root/'untrusted-hook').chmod(0o700)
        runtime.source_inputs(self.root, commit)
        self.assertFalse((self.root/'marker-must-not-exist').exists())
        (self.root/'server/main.py').write_text('uncommitted mutation')
        with self.assertRaises(runtime.RuntimeBuildError):
            runtime.source_inputs(self.root, commit)
        with self.assertRaises(runtime.RuntimeBuildError):
            runtime.source_inputs(self.root, 'a'*40)

    def test_failed_package_stage_keeps_private_evidence_and_raises(self):
        with self.assertRaises(runtime.RuntimeBuildError):
            runtime.run_stage(['/usr/bin/false'], cwd=self.root, environment=runtime.clean_environment(self.root), log=self.root/'failure.log', timeout=1)
        self.assertEqual((self.root/'failure.log').stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
