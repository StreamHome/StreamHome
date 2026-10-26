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

    def test_target_source_is_private_exact_git_input_and_never_installed_checkout(self):
        from types import SimpleNamespace
        cache = self.root / 'cache'
        base = cache / 'update-preparation'
        operation = base / str(uuid.uuid4())
        source = operation / 'checkout'
        for directory in (cache, base, operation, source):
            directory.mkdir(mode=0o700)
        names = ('server/main.py', 'server/requirements.txt', 'server/requirements.lock',
            'web/package.json', 'web/package-lock.json')
        for name in names:
            path = source / name
            path.parent.mkdir(exist_ok=True)
            path.write_text('synthetic committed target')
        def git(*args):
            return subprocess.check_output(['/usr/bin/git', '-C', str(source), *args],
                stderr=subprocess.DEVNULL, text=True).strip()
        git('init', '-q')
        git('add', 'server', 'web')
        git('-c', 'user.name=Runtime test', '-c', 'user.email=runtime@example.invalid',
            'commit', '-qm', 'test: isolated target source')
        commit = git('rev-parse', 'HEAD')
        layout = SimpleNamespace(managed=True, cache=cache)
        with patch('lifecycle.layout.layout_for_root', return_value=layout):
            selected = runtime.preparation_source(self.root, source)
            self.assertEqual(set(runtime.source_inputs(selected, commit)), set(names))
            self.assertFalse((self.root / 'server').exists())
            (source / 'server/main.py').write_text('changed after target review')
            with self.assertRaises(runtime.RuntimeBuildError):
                runtime.source_inputs(selected, commit)
            # The same bytes at an alias are insufficient path evidence.
            alias = operation / 'alias'
            alias.symlink_to(source, target_is_directory=True)
            for invalid in (alias, base / 'not-a-uuid' / 'checkout'):
                with self.assertRaises(runtime.RuntimeBuildError):
                    runtime.preparation_source(self.root, invalid)
            source.chmod(0o755)
            with self.assertRaises(runtime.RuntimeBuildError):
                runtime.preparation_source(self.root, source)
            source.chmod(0o700)
            moved = operation / 'preserved'
            source.rename(moved)
            source.symlink_to(moved, target_is_directory=True)
            with self.assertRaises(runtime.RuntimeBuildError):
                runtime.preparation_source(self.root, source)
        self.assertEqual(runtime.preparation_source(self.root, None), self.root)

    def test_runtime_ancestor_alias_and_external_writes_fail_before_generation_creation(self):
        protected = runtime.require_runtime_root(self.root, create=True)
        self.assertEqual(protected.stat().st_mode & 0o777, 0o700)
        (protected / 'evidence').write_text('retained original bytes')
        protected.chmod(0o775)
        with self.assertRaises(runtime.RuntimeBuildError):runtime.require_runtime_root(self.root, create=True)
        protected.chmod(0o700)
        retained = self.root / 'retained-runtime'
        protected.rename(retained);protected.symlink_to(retained, target_is_directory=True)
        for create in (False, True):
            with self.subTest(create=create), self.assertRaises(runtime.RuntimeBuildError):
                runtime.require_runtime_root(self.root, create=create)
        self.assertEqual((retained / 'evidence').read_text(), 'retained original bytes')
        self.assertEqual(list(retained.iterdir()), [retained / 'evidence'])

    def test_preflight_runtime_verification_uses_target_source_without_changing_installed_source(self):
        import json
        from types import SimpleNamespace
        cache = self.root / 'cache'
        base = cache / 'update-preparation'
        operation = base / str(uuid.uuid4())
        source = operation / 'checkout'
        for directory in (cache, base, operation, source):
            directory.mkdir(mode=0o700)
        names = ('server/main.py', 'server/requirements.txt', 'server/requirements.lock',
            'web/package.json', 'web/package-lock.json')
        for checkout, value in ((self.root, 'installed source'), (source, 'candidate source')):
            for name in names:
                file = checkout / name
                file.parent.mkdir(exist_ok=True)
                file.write_text(value)
            def git(*args):
                return subprocess.check_output(['/usr/bin/git', '-C', str(checkout), *args],
                    stderr=subprocess.DEVNULL, text=True).strip()
            git('init', '-q');git('add', 'server', 'web')
            git('-c', 'user.name=Runtime test', '-c', 'user.email=runtime@example.invalid',
                'commit', '-qm', 'test: bound source')
        commit = git('rev-parse', 'HEAD')
        bundle, python, node, web = (str(uuid.uuid4()) for _ in range(4))
        (self.root / 'runtime').mkdir(mode=0o700)
        paths = {}
        for category, identity in (('bundles', bundle), ('python', python), ('node', node), ('web', web)):
            paths[category] = self.root / 'runtime' / category / identity
            paths[category].mkdir(parents=True)
        (paths['bundles'] / 'tools').mkdir()
        (paths['web'] / 'assets.json').write_text('{}')
        manifest = {'schema_version': 1, 'bundle_id': bundle, 'application_commit': commit,
            'application_build_id': commit[:12], 'python_id': python, 'node_id': node, 'web_id': web,
            'source': {name: hashlib.sha256(raw).hexdigest() for name, raw in runtime.source_inputs(source, commit).items()},
            'tools': runtime.tree_inventory(paths['bundles'] / 'tools')}
        for category in ('python', 'node', 'web'):
            manifest[category] = runtime.tree_inventory(paths[category])
        file = paths['bundles'] / 'manifest.json'
        file.write_bytes(runtime.canonical_bytes(manifest))
        execution = SimpleNamespace(runtime_bundle_id=bundle, runtime_manifest_sha256=runtime.file_sha256(file),
            application_commit=commit, application_build_id=commit[:12], python_environment_id=python,
            node_environment_id=node, web_bundle_id=web,
            web_asset_manifest_sha256=runtime.file_sha256(paths['web'] / 'assets.json'))
        layout = SimpleNamespace(managed=True, cache=cache)
        with patch('lifecycle.layout.layout_for_root', return_value=layout):
            self.assertEqual(runtime.verify_runtime(self.root, execution, source_root=source), manifest)
            with self.assertRaises(runtime.RuntimeBuildError):
                runtime.verify_runtime(self.root, execution)
            (source / 'server/main.py').write_text('unreviewed candidate mutation')
            with self.assertRaises(runtime.RuntimeBuildError):
                runtime.verify_runtime(self.root, execution, source_root=source)
        self.assertEqual((self.root / 'server/main.py').read_text(), 'installed source')
        self.assertEqual(json.loads(file.read_bytes()), manifest)

    def test_failed_package_stage_keeps_private_evidence_and_raises(self):
        with self.assertRaises(runtime.RuntimeBuildError):
            runtime.run_stage(['/usr/bin/false'], cwd=self.root, environment=runtime.clean_environment(self.root), log=self.root/'failure.log', timeout=1)
        self.assertEqual((self.root/'failure.log').stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
