from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from lifecycle import recovery, state
from lifecycle.control import runtime_observation as observe
from lifecycle.control.rpc import ControlError
from lifecycle.management_journal import parse_execution_tuple,execution_tuple_sha256
from tests.lifecycle.test_management_journal import _execution_payload


class ManagedRuntimeObservationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(observe.state, 'inspect_state', return_value=state.LifecycleState(2, 0, None)))
        self.root=Path('/opt/streamhome');self.execution=parse_execution_tuple(_execution_payload())
        self.selected=SimpleNamespace(service_uid=1234,installation_id='synthetic-installation')
        self.layout=SimpleNamespace(managed=True,descriptor=SimpleNamespace(descriptor=self.selected))
        self.enterContext(patch.object(observe,'layout_for_root',return_value=self.layout))
        self.enterContext(patch.object(observe.os,'geteuid',return_value=1234))
        self.unit={'ActiveState':'active','MainPID':'4321','InvocationID':'a'*32}
        self.unit_read=self.enterContext(patch.object(observe,'unit_snapshot',return_value=self.unit))
        self.snapshot={'start_identity':'987','launches_open':True,
                       'execution_sha256':execution_tuple_sha256(self.execution),'children':{}}
        self.enterContext(patch.object(observe,'supervisor_snapshot',return_value=self.snapshot))
        self.enterContext(patch.object(observe,'process_identity',return_value=('987','S')))
        self.enterContext(patch.object(observe,'process_cgroup',return_value=observe.RUNTIME_CGROUP))
        self.enterContext(patch.object(observe.Path,'stat',return_value=SimpleNamespace(st_uid=1234)))
        self.enterContext(patch.object(observe,'read_active_execution',return_value=SimpleNamespace(
            installation_id=self.selected.installation_id,execution=self.execution)))
        self.stopped=self.enterContext(patch.object(observe,'require_stopped'))
        self.quiet=self.enterContext(patch.object(observe,'require_service_quiescent'))

    def test_supervisor_without_children_is_active_not_safe_abort_offline(self):
        self.assertEqual(observe.runtime_status(self.root),'active')
        self.stopped.assert_not_called();self.quiet.assert_not_called()

    def test_offline_requires_both_empty_runtime_and_no_foreign_service_uid_work(self):
        self.unit={'ActiveState':'inactive','MainPID':'0','InvocationID':''}
        self.unit_read.return_value=self.unit
        self.assertEqual(observe.runtime_status(self.root),'offline')
        self.stopped.assert_called_once_with();self.quiet.assert_called_once_with(inspections=True,settle_timeout=2)
        self.quiet.side_effect=ControlError('control_foreign_service_process')
        self.assertEqual(observe.runtime_status(self.root),'unknown')

    def test_replaced_stopping_or_wrong_execution_is_unknown(self):
        self.snapshot['execution_sha256']='d'*64
        self.assertEqual(observe.runtime_status(self.root),'unknown')
        self.snapshot['execution_sha256']=execution_tuple_sha256(self.execution)
        self.snapshot['launches_open']=False
        self.assertEqual(observe.runtime_status(self.root),'unknown')
        self.snapshot['launches_open']=True
        self.unit_read.side_effect=[self.unit,{**self.unit,'InvocationID':'b'*32}]
        self.assertEqual(observe.runtime_status(self.root),'unknown')

    def test_canonical_planner_selects_protected_observer_without_legacy_runtime_import(self):
        with patch.object(recovery,'layout_for_root',return_value=self.layout),patch.object(
            observe,'runtime_status',return_value='active') as runtime:
            self.assertEqual(recovery._runtime_status(self.root),'active')
            runtime.assert_called_once_with(self.root)


if __name__=='__main__':unittest.main()
