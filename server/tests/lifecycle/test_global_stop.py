from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from lifecycle.control import stop
from lifecycle.control.rpc import ControlError


class GlobalStopTests(unittest.TestCase):
    def test_only_exact_qualified_unit_instances_are_selected(self):
        identity = str(uuid.uuid4())
        names = ['streamhome-operation@' + identity + '.service', 'streamhome-startup@' + '1' * 32 + '.service']
        def ctl(*arguments, **keywords):
            if arguments[0] == 'list-units':
                return '\n'.join(name + ' loaded active running Task fixture' for name in names)
            name = arguments[1]
            role = 'startup' if 'startup@' in name else 'operation'
            return 'LoadState=loaded\nFragmentPath=/etc/systemd/system/streamhome-' + role + '@.service\nUser=' + ('root' if role == 'startup' else 'streamhome') + '\nControlGroup=/system.slice/' + name
        with patch.object(stop, 'systemctl', side_effect=ctl) as controller, patch.object(stop, 'installation_scopes', return_value=()):
            self.assertEqual(stop.auxiliary_units(), tuple(sorted(names)))
        self.assertEqual(controller.call_args_list[0].args[0], 'list-units')

    def test_foreign_units_aliases_owners_and_cgroups_are_never_stopped(self):
        valid = 'streamhome-operation@' + str(uuid.uuid4()) + '.service'
        fixtures = [('ssh.service', ''), ('streamhome-prepare@not-an-id.service', ''),
            ('streamhome-operation@00000000-0000-0000-0000-000000000000.service', ''),
            (valid, 'FragmentPath=/run/systemd/system/foreign.service\nUser=streamhome\nControlGroup='),
            (valid, 'FragmentPath=/etc/systemd/system/streamhome-operation@.service\nUser=root\nControlGroup='),
            (valid, 'FragmentPath=/etc/systemd/system/streamhome-operation@.service\nUser=streamhome\nControlGroup=/foreign')]
        for name, metadata in fixtures:
            with self.subTest(name=name, metadata=metadata), patch.object(stop, 'require_root'), patch.object(stop, 'systemctl', side_effect=[name + ' loaded active running fixture', metadata]) as ctl:
                with self.assertRaisesRegex(ControlError, 'control_stop_unit_unqualified'):
                    stop.stop_units(runtime_registered=True)
            self.assertFalse(any(call.args[0] == 'stop' for call in ctl.call_args_list))

    def test_global_stop_waits_for_auxiliary_and_runtime_cgroups(self):
        actor = 'streamhome-prepare@' + str(uuid.uuid4()) + '.service'
        runtime = {'ActiveState': 'inactive', 'MainPID': '0'}
        with patch.object(stop, 'require_root'), patch.object(stop, 'auxiliary_units', side_effect=[(actor,), (actor,)]), patch.object(stop, 'systemctl') as ctl, patch.object(stop, '_require_empty') as empty, patch.object(stop, 'unit_snapshot', return_value=runtime):
            stop.stop_units(runtime_registered=True)
        ctl.assert_called_once_with('stop', 'streamhome.service', actor, timeout=75)
        self.assertEqual([call.args[0] for call in empty.call_args_list], ['streamhome.service', actor])

    def test_early_stop_still_stops_preparation_without_a_runtime_definition(self):
        actor = 'streamhome-provision@' + str(uuid.uuid4()) + '.service'
        with patch.object(stop, 'require_root'), patch.object(stop, 'auxiliary_units', side_effect=[(actor,), ()]), patch.object(stop, 'systemctl') as ctl, patch.object(stop, '_require_empty'), patch.object(stop, 'unit_snapshot') as runtime:
            stop.stop_units(runtime_registered=False)
        ctl.assert_called_once_with('stop', actor, timeout=75)
        runtime.assert_not_called()

    def test_installer_scope_is_bound_to_the_protected_installation_intent(self):
        installation = str(uuid.uuid4())
        name = 'streamhome-install-' + installation + '-' + '2' * 32 + '.scope'
        metadata = 'LoadState=loaded\nTransient=yes\nControlGroup=/system.slice/' + name + '\nDescription=StreamHome installation ' + installation
        with patch('lifecycle.control.provisioning.read_intent', return_value=SimpleNamespace(installation_id=installation)), patch.object(stop, 'systemctl', side_effect=[name + ' loaded active running fixture', metadata]):
            self.assertEqual(stop.installation_scopes(), (name,))
        with patch('lifecycle.control.provisioning.read_intent', return_value=SimpleNamespace(installation_id=str(uuid.uuid4()))), patch.object(stop, 'systemctl', return_value=name + ' loaded active running fixture'):
            with self.assertRaisesRegex(ControlError, 'control_stop_unit_unqualified'):
                stop.installation_scopes()

    def test_collected_units_are_skipped_only_with_empty_cgroup_proof(self):
        name = 'streamhome-prepare@' + str(uuid.uuid4()) + '.service'
        with patch.object(stop, 'installation_scopes', return_value=()), patch.object(stop, 'systemctl', side_effect=[name + ' loaded active running fixture', 'LoadState=not-found']), patch.object(stop, '_require_empty') as empty:
            self.assertEqual(stop.auxiliary_units(), ())
        empty.assert_called_once_with(name)
        with patch.object(stop, 'installation_scopes', return_value=()), patch.object(stop, 'systemctl', return_value=name + ' loaded failed failed fixture') as ctl, patch.object(stop, '_require_empty') as empty:
            self.assertEqual(stop.auxiliary_units(), ())
        self.assertEqual(ctl.call_count, 1)
        empty.assert_called_once_with(name)

    def test_new_actor_or_remaining_descendant_prevents_false_success(self):
        with patch.object(stop, 'require_root'), patch.object(stop, 'auxiliary_units', side_effect=[(), ('new-actor',)]), patch.object(stop, 'systemctl'):
            with self.assertRaisesRegex(ControlError, 'control_stop_unit_changed'):
                stop.stop_units(runtime_registered=True)
        with patch.object(stop.Path, 'read_text', return_value='123\n'):
            with self.assertRaisesRegex(ControlError, 'control_stop_children_busy'):
                stop._require_empty('streamhome.service')


if __name__ == '__main__':
    unittest.main()
