from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from lifecycle.control import resumption_contract as contract, resumption_store as store
from lifecycle.control.rpc import ControlError
from tests.lifecycle.test_resumption_contract import plan


class ResumptionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plan = plan();self.evidence = {'owner': {'pid': os.getpid()}, 'scope': {}}
        self.plan['issuer_sha256'] = contract.digest(self.evidence)
        selected = SimpleNamespace(status=SimpleNamespace(value='ready'), installation_id=self.plan['installation_id'], service_gid=os.getgid())
        def directory(path, **kwargs):
            path.mkdir(parents=True, exist_ok=True)
            return os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        def path(value):
            if value == '/proc/sys/kernel/random/boot_id':return SimpleNamespace(read_text=lambda: self.plan['boot_id'])
            if value == '/proc/self/cgroup':return SimpleNamespace(read_text=lambda: '0::/system.slice/streamhome.service\n')
            return Path(value)
        self.stack = self.enterContext(ExitStack())
        for target, replacement in (('ROOT', self.root), ('START_INTENT', self.root / 'start-intent.json'), ('require_stopped', lambda: None), ('require_root', lambda: None), ('Path', path),
                ('descriptor', lambda: selected), ('root_directory', directory),
                ('read_protected_json', lambda target: json.loads(target.read_text())),
                ('atomic_root_json', lambda target, value, **kwargs: target.write_text(json.dumps(value))),
                ('read_stop_epoch', lambda: self.plan['stop_epoch'])):
            self.stack.enter_context(patch.object(store, target, replacement))
        self.live = self.stack.enter_context(patch.object(store, 'require_issuer'))
        self.stack.enter_context(patch.object(store.time, 'monotonic', return_value=100))
        self.stack.enter_context(patch.object(store.os, 'fstat', return_value=SimpleNamespace(st_mode=0o100600, st_uid=0, st_nlink=1)))
        self.stack.enter_context(patch.dict(store.os.environ, INVOCATION_ID='3' * 32))
        self.manifest = self.plan['control_manifest_sha256']

    def publish(self):store.publish(self.plan, self.evidence, control_manifest_sha256=self.manifest)

    def observation(self, index=0):
        stage = self.plan['stages'][index]
        return {**{name: self.plan[name] for name in ('installation_id', 'operation_id', 'owner_type', 'action')},
            **{name: stage[name] for name in ('generation', 'execution_sha256', 'handoff_sha256')},
            'accepted': False, 'candidate_failed': bool(index), 'previous_restored': bool(index)}

    def consume(self, index=0, observation=None):
        return store.consume_stage(self.plan['plan_id'], self.plan['stages'][index]['kind'],
            self.observation(index) if observation is None else observation, control_manifest_sha256=self.manifest)

    def receipt_path(self, index=0):return self.root / self.plan['plan_id'] / (self.plan['stages'][index]['kind'] + '.json')

    def test_published_plan_and_consumed_intent_never_rearm_after_a_lost_reply(self):
        self.publish();receipt = self.consume();original = self.receipt_path().read_bytes()
        with self.assertRaises(ControlError):self.publish()
        with self.assertRaises(ControlError):self.consume()
        self.assertEqual(self.receipt_path().read_bytes(), original)
        bound = store.bind_start(self.plan['plan_id'], 'candidate_start', '3' * 32, control_manifest_sha256=self.manifest)
        self.assertEqual(bound['nonce'], receipt['nonce'])
        self.assertEqual(bound['status'], 'bound')
        original = self.receipt_path().read_bytes()
        with self.assertRaises(ControlError):store.bind_start(self.plan['plan_id'], 'candidate_start', '3' * 32, control_manifest_sha256=self.manifest)
        self.assertEqual(self.receipt_path().read_bytes(), original)

    def test_control_cutover_issuer_and_new_runtime_cannot_borrow_each_others_reader(self):
        self.plan.update(schema_version=3,action='provision_control')
        self.plan['stages'][0]['control_manifest_sha256']='1'*64
        self.plan['stages'][1]['control_manifest_sha256']=self.manifest
        self.publish()
        observed={**self.observation(),'control_manifest_sha256':'1'*64}
        self.consume(observation=observed)
        original=self.receipt_path().read_bytes()
        with self.assertRaises(ControlError):
            store.bind_start(self.plan['plan_id'],'candidate_start','3'*32,control_manifest_sha256=self.manifest)
        self.assertEqual(self.receipt_path().read_bytes(),original)
        with self.assertRaises(ControlError):
            store.consume_stage(self.plan['plan_id'],'candidate_start',observed,control_manifest_sha256='1'*64)
        bound=store.bind_start(self.plan['plan_id'],'candidate_start','3'*32,control_manifest_sha256='1'*64)
        self.assertEqual(bound['status'],'bound')
        with self.assertRaises(ControlError):
            store.live_state(self.plan['plan_id'],'candidate_start',control_manifest_sha256=self.manifest,runtime_reader=True)
        self.assertEqual(store.live_state(self.plan['plan_id'],'candidate_start',
            control_manifest_sha256='1'*64,runtime_reader=True)['receipt'],bound)

    def test_concurrent_issuers_cannot_consume_the_same_stage_twice(self):
        self.publish()
        def attempt(_):
            try:self.consume();return True
            except ControlError:return False
        with ThreadPoolExecutor(max_workers=4) as executor:results = list(executor.map(attempt, range(8)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(contract.parse_receipt(json.loads(self.receipt_path().read_text()), self.plan)['status'], 'consumed')

    def test_changed_live_issuer_before_write_cannot_publish_consumption(self):
        self.publish();self.live.side_effect = [None, ControlError('control_resumption_issuer_changed')]
        with self.assertRaises(ControlError):self.consume()
        self.assertFalse(self.receipt_path().exists())

    def test_changed_tuple_and_wrong_manifest_preserve_unconsumed_plan(self):
        self.publish()
        with self.assertRaises(ControlError):self.consume(observation={**self.observation(), 'execution_sha256': 'e' * 64})
        with self.assertRaises(ControlError):store.consume_stage(self.plan['plan_id'], 'candidate_start', self.observation(), control_manifest_sha256='f' * 64)
        self.assertFalse(self.receipt_path().exists())

    def test_previous_receipt_copied_to_candidate_path_does_not_prove_candidate_consumption(self):
        self.publish()
        fake = contract.consume(self.plan, self.plan['stages'][1], '1' * 64, existing=None)
        self.receipt_path().write_text(json.dumps(fake))
        with self.assertRaises(ControlError):self.consume(1)
        self.assertFalse(self.receipt_path(1).exists())

    def test_fallback_is_separate_one_use_and_acceptance_disables_it(self):
        self.publish();self.consume()
        with self.assertRaises(ControlError):self.consume(1, observation={**self.observation(1), 'accepted': True})
        self.assertFalse(self.receipt_path(1).exists())
        self.consume(1)
        with self.assertRaises(ControlError):self.consume(1)
        self.assertNotEqual(json.loads(self.receipt_path().read_text())['nonce'], json.loads(self.receipt_path(1).read_text())['nonce'])

    def test_only_runtime_start_hook_can_bind_the_current_invocation(self):
        self.publish();self.consume()
        original = self.receipt_path().read_bytes()
        with self.assertRaises(ControlError):store.bind_start(self.plan['plan_id'], 'candidate_start', '4' * 32, control_manifest_sha256=self.manifest)
        with patch.object(store, 'Path', return_value=SimpleNamespace(read_text=lambda: '0::/system.slice/foreign.service\n')):
            with self.assertRaises(ControlError):store.bind_start(self.plan['plan_id'], 'candidate_start', '3' * 32, control_manifest_sha256=self.manifest)
        self.assertEqual(self.receipt_path().read_bytes(), original)

    def test_failed_dispatch_intent_publication_leaves_consumption_irreversible(self):
        self.publish()
        def write(path, value, **kwargs):
            if path == store.START_INTENT:raise OSError('intent publication interrupted')
            path.write_text(json.dumps(value))
        with patch.object(store, 'atomic_root_json', side_effect=write):
            with self.assertRaises(OSError):self.consume()
        self.assertEqual(json.loads(self.receipt_path().read_text())['status'], 'consumed')
        with self.assertRaises(ControlError):self.consume()
        self.assertFalse(store.START_INTENT.exists())

    def test_retargeted_dispatch_intent_cannot_bind_an_unrelated_permit(self):
        self.publish();self.consume()
        original = self.receipt_path().read_bytes()
        intent = json.loads(store.START_INTENT.read_text());intent['nonce'] = '0' * 64
        store.START_INTENT.write_text(json.dumps(intent))
        with self.assertRaises(ControlError):store.bind_current_start(control_manifest_sha256=self.manifest)
        self.assertEqual(self.receipt_path().read_bytes(), original)

    def test_queued_or_live_runtime_blocks_consumption_before_dispatch(self):
        self.publish()
        with patch.object(store, 'require_stopped', side_effect=ControlError('control_stopped_repair_service_changed')):
            with self.assertRaises(ControlError):self.consume()
        self.assertFalse(self.receipt_path().exists())
        self.assertFalse(store.START_INTENT.exists())

    def test_root_store_imports_no_canonical_or_application_authority(self):
        script = "import sys;import lifecycle.control.resumption_store;assert not {'lifecycle.state','lifecycle.management_journal','config','database','application'}.intersection(sys.modules)"
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':unittest.main()
