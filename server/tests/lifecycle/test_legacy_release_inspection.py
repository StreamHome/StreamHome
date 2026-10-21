import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lifecycle.control import legacy_inspection
from lifecycle.control.rpc import ControlError
from lifecycle.install_discovery import ExistingInstallation
from lifecycle.release_probe import ProbeResult, ProbeStatus, ReleasePolicy


class LegacyReleaseInspectionTests(unittest.TestCase):
    def test_root_is_rejected_before_discovery_or_network(self):
        with patch.object(legacy_inspection.os,'geteuid',return_value=0), patch.object(legacy_inspection,'discover_installations') as discover:
            with self.assertRaises(ControlError):
                legacy_inspection.inspect_legacy(['/legacy'])
        discover.assert_not_called()

    def test_incomplete_hint_is_retained_without_probe_or_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            with patch.object(legacy_inspection,'probe_release') as probe:
                result=legacy_inspection.inspect_legacy([str(root)])
            self.assertEqual(result['status'],'unavailable')
            self.assertEqual(list(root.iterdir()),[])
        probe.assert_not_called()

    def test_qualified_legacy_probe_rechecks_identity_and_literal_policy(self):
        from lifecycle.install_discovery import DiscoveryResult, DiscoveryStatus
        installed=ExistingInstallation(Path('/legacy'),os.geteuid(),'a'*40,'main','legacy','b'*64)
        policy=ReleasePolicy(failed_target='c'*40)
        def probe(candidate, current_policy, *, read_current):
            self.assertEqual(candidate, installed)
            self.assertEqual(current_policy, policy)
            self.assertEqual(read_current(), (installed,policy))
            return ProbeResult(ProbeStatus.POLICY_REJECTED,installed.commit,'c'*40,'This exact release previously failed.')
        with patch.object(legacy_inspection,'discover_installations',return_value=DiscoveryResult(DiscoveryStatus.EXISTING,installed,'existing')), patch.object(legacy_inspection,'inspect_candidate',return_value=installed), patch.object(legacy_inspection,'read_policy',return_value=policy), patch.object(legacy_inspection,'probe_release',side_effect=probe):
            result=legacy_inspection.inspect_legacy(['/legacy'])
        self.assertEqual(result, {'status':'policy_rejected','reason':'This exact release previously failed.'})


if __name__ == '__main__':
    unittest.main()
