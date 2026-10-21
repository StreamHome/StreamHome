import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from lifecycle.install_discovery import DiscoveryError
from lifecycle import release_inspection as inspection


class ReleaseInspectionTests(unittest.TestCase):
    def test_only_literal_policy_values_are_read_and_no_shell_input_is_executed(self):
        raw = b"PRIVATE_VALUE=never-copy-or-print\nexport UPDATE_BRANCH='stable'\nUPDATE_REQUIRE_SIGNED_COMMITS=true # policy\nUPDATE_TRUSTED_SIGNERS=ABCDEF\n"
        self.assertEqual(inspection.literal_update_settings(raw), {'UPDATE_BRANCH':'stable','UPDATE_REQUIRE_SIGNED_COMMITS':'true','UPDATE_TRUSTED_SIGNERS':'ABCDEF'})
        for value in (b'UPDATE_BRANCH=${OTHER}\n', b'UPDATE_BRANCH=$(anything)\n', b'UPDATE_BRANCH=`anything`\n',
                      b'UPDATE_BRANCH=main\nUPDATE_BRANCH=other\n', b'UPDATE_BRANCH=two values\n'):
            with self.subTest(value=value), self.assertRaises(DiscoveryError):
                inspection.literal_update_settings(value)

    def test_private_policy_reader_rejects_aliases_links_permissions_and_size(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            path=root/'policy.env'
            path.write_bytes(b'UPDATE_BRANCH=main\n')
            path.chmod(0o600)
            self.assertEqual(inspection.private_optional_bytes(path),path.read_bytes())
            self.assertIsNone(inspection.private_optional_bytes(root/'missing'))
            alias=root/'alias'
            alias.symlink_to(path)
            with self.assertRaises(OSError): inspection.private_optional_bytes(alias)
            parent_alias=root/'parent-alias'
            parent_alias.symlink_to(root,target_is_directory=True)
            with self.assertRaises(DiscoveryError): inspection.private_optional_bytes(parent_alias/path.name)
            with self.assertRaises(DiscoveryError): inspection.private_optional_bytes(parent_alias/'missing')
            path.chmod(0o644)
            with self.assertRaises(DiscoveryError): inspection.private_optional_bytes(path)
            path.chmod(0o600)
            os.link(path,root/'hardlink')
            with self.assertRaises(DiscoveryError): inspection.private_optional_bytes(path)
            (root/'hardlink').unlink()
            with self.assertRaises(DiscoveryError): inspection.private_optional_bytes(path,maximum=1)

    def test_runtime_env_precedence_signature_requirement_and_failed_target_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            config=root/'configuration';config.mkdir(mode=0o700)
            runtime=root/'state';runtime.mkdir(mode=0o700)
            values={config/'.env':b'UPDATE_BRANCH=old\nUPDATE_REQUIRE_SIGNED_COMMITS=false\n',
                    config/'runtime.env':b'UPDATE_BRANCH=stable\nUPDATE_REQUIRE_SIGNED_COMMITS=true\nUPDATE_TRUSTED_SIGNERS='+b'a'*40+b'\n',
                    runtime/'update-state.json':b'{"failed_target":"'+b'b'*40+b'"}'}
            for path,raw in values.items(): path.write_bytes(raw);path.chmod(0o600)
            layout=SimpleNamespace(configuration=config,durable_runtime=runtime,managed=True)
            before={path:path.read_bytes() for path in values}
            with patch.object(inspection,'layout_for_root',return_value=layout):
                policy=inspection.read_policy(root)
            self.assertEqual(policy.branch,'stable')
            self.assertTrue(policy.require_signed_commits)
            self.assertEqual(policy.trusted_signers,('A'*40,))
            self.assertEqual(policy.failed_target,'b'*40)
            self.assertEqual(before,{path:path.read_bytes() for path in values})

    def test_legacy_root_environment_precedes_server_values_and_ignores_runtime_env(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            (root/'server').mkdir(mode=0o700)
            (root/'.run').mkdir(mode=0o700)
            values={root/'server/.env':b'UPDATE_BRANCH=old\n', root/'.env':b'UPDATE_BRANCH=stable\n', root/'server/runtime.env':b'UPDATE_BRANCH=incorrect\n'}
            for path,raw in values.items():
                path.write_bytes(raw)
                path.chmod(0o600)
            self.assertEqual(inspection.read_policy(root).branch, 'stable')
            self.assertEqual(values, {path:path.read_bytes() for path in values})


if __name__ == '__main__':
    unittest.main()
