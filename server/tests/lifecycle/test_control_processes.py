import signal
import uuid
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from lifecycle.control import processes
from lifecycle.control.rpc import ControlError


class OwnedProcessSignalTests(unittest.TestCase):
    def test_pinned_inspection_requires_root_parent_valid_pin_complete_tree_and_read_only_action(self):
        operation=str(uuid.uuid4())
        prefix=b'/usr/bin/python3\0-I\0/usr/local/libexec/streamhome/provision\0'
        command=[prefix+b'--operation-worker\0'+operation.encode()+b'\0resumption-observe\0']
        child=Mock();child.__truediv__=Mock(return_value=SimpleNamespace(read_bytes=lambda:command[0]))
        parent=Mock();parent.stat.return_value.st_uid=0
        parent.__truediv__=Mock(return_value=SimpleNamespace(read_bytes=lambda:prefix+b'--before-start\0'))
        proc=Mock();proc.__truediv__=Mock(return_value=parent)
        pin={'schema_version':1,'bundle_id':str(uuid.uuid4()),'manifest_sha256':'a'*64}
        with patch.object(processes,'Path',return_value=proc),patch.object(processes.entry,'operation_selection',return_value=pin) as select, \
                patch.object(processes.entry,'verify_bundle') as verify:
            self.assertTrue(processes._protected_reader(child,['S','100']))
            select.assert_called_once_with(operation);verify.assert_called_once_with(pin,rescue=False)
            command[0]=prefix+b'--operation-rescue-worker\0'+operation.encode()+b'\0bound-control-review\0'+operation.encode()+b'\0'
            self.assertTrue(processes._protected_reader(child,['S','100']))
            self.assertEqual(verify.call_args.kwargs,{'rescue':True})
            command[0]=prefix+b'--operation-rescue-worker\0'+operation.encode()+b'\0bound-control-prepare\0'
            self.assertFalse(processes._protected_reader(child,['S','100']))
            command[0]=prefix+b'--operation-rescue-worker\0'+operation.encode()+b'\0resumption-observe\0'
            self.assertFalse(processes._protected_reader(child,['S','100']))
            command[0]=prefix+b'--operation-worker\0'+operation.encode()+b'\0prepare-recovery\0'
            self.assertFalse(processes._protected_reader(child,['S','100']))
            command[0]=prefix+b'--operation-worker\0'+operation.encode()+b'\0resumption-observe\0'
            select.return_value=None
            self.assertFalse(processes._protected_reader(child,['S','100']))
            select.return_value=pin;verify.side_effect=processes.entry.EntryError('corrupt reader')
            self.assertFalse(processes._protected_reader(child,['S','100']))
            verify.side_effect=None;parent.stat.return_value.st_uid=1000
            self.assertFalse(processes._protected_reader(child,['S','100']))

    def test_quiescence_allows_exact_reproved_update_delegate_but_not_other_service_work(self):
        from lifecycle import managed_update, state
        operation = str(uuid.uuid4())
        reference = (201, '301', 202, '302')
        lease = SimpleNamespace(operation_id=operation, pid=200, process_start_identity='300')
        class Process:
            def __init__(self, pid):self.name = str(pid)
            def stat(self):return SimpleNamespace(st_uid=1000)
            def __truediv__(self, _):return SimpleNamespace(read_text=lambda: self.name+' (fixture) S 0')
        peers = [Process(pid) for pid in (200, 201, 202)]
        def path(name):
            if name == '/proc':return SimpleNamespace(iterdir=lambda: peers)
            if name == '/proc/200':return Process(200)
            return SimpleNamespace(read_text=lambda: '')
        with patch.object(processes.os, 'geteuid', return_value=1000), patch.object(
                processes.os, 'getpid', return_value=100), patch.object(processes, 'Path', side_effect=path), patch.object(
                processes, 'process_cgroup', return_value='/system.slice/streamhome-operation@'+operation+'.service'), patch(
                'lifecycle.control.administration_lock.process_identity', return_value=('300', 'S')), patch.object(
                state, 'read_controller_lease', return_value=lease), patch.object(
                managed_update, 'quiescent_delegate', return_value=reference) as proof:
            processes.require_service_quiescent(controller=(operation, 200, '300'), update_delegate=reference)
            self.assertEqual(proof.call_count, 2)
            peers.append(Process(203))
            with self.assertRaisesRegex(ControlError, 'control_foreign_service_process'):
                processes.require_service_quiescent(controller=(operation, 200, '300'), update_delegate=reference)
            peers.pop();proof.return_value = (201, 'changed', 202, '302')
            with self.assertRaisesRegex(ControlError, 'control_controller_changed'):
                processes.require_service_quiescent(controller=(operation, 200, '300'), update_delegate=reference)

    def test_prestart_quiescence_allows_only_the_exact_root_hook_and_its_reader(self):
        members = ['100 200']
        command = [b'/usr/bin/python3\0-I\0/usr/local/libexec/streamhome/provision\0--before-start\0']
        class Hook:
            name = '200'
            def stat(self):return SimpleNamespace(st_uid=0)
            def __truediv__(self, _):return SimpleNamespace(read_bytes=lambda: command[0])
        def path(name):
            if name == '/proc':return SimpleNamespace(iterdir=lambda: [Hook()])
            if name == '/proc/200':return Hook()
            return SimpleNamespace(read_text=lambda: members[0])
        with patch.object(processes.os, 'geteuid', return_value=1000), patch.object(processes.os, 'getpid', return_value=100), patch.object(processes.os, 'getppid', return_value=200), patch.object(processes, 'Path', side_effect=path), patch.object(processes, 'process_cgroup', return_value=processes.RUNTIME_CGROUP), patch('lifecycle.control.administration_lock.process_identity', return_value=('2000', 'S')):
            processes.require_service_quiescent(startup_hook=(200, '2000'))
            members[0] = '100 200 201'
            with self.assertRaisesRegex(ControlError, 'control_children_busy'):
                processes.require_service_quiescent(startup_hook=(200, '2000'))
            members[0] = '100 200';command[0] = b'/usr/bin/python3\0/tmp/untrusted.py\0'
            with self.assertRaisesRegex(ControlError, 'control_unit_hook_forbidden'):
                processes.require_service_quiescent(startup_hook=(200, '2000'))
            with self.assertRaisesRegex(ControlError, 'control_unit_hook_forbidden'):
                processes.require_service_quiescent(startup_hook=(201, '2000'))

    def test_quiescence_exempts_only_the_exact_live_controller_identity(self):
        operation = str(uuid.uuid4())
        class Process:
            def __init__(self, name):self.name = name
            def stat(self):return SimpleNamespace(st_uid=1000)
            def __truediv__(self, _):return SimpleNamespace(read_text=lambda: self.name + ' (fixture) S 0')
        peers = [Process('200')]
        def path(name):
            if name == '/proc':return SimpleNamespace(iterdir=lambda: peers)
            if name == '/proc/200':return SimpleNamespace(stat=lambda: SimpleNamespace(st_uid=1000))
            return SimpleNamespace(read_text=lambda: '')
        with patch.object(processes.os, 'geteuid', return_value=1000), patch.object(processes.os, 'getpid', return_value=100), patch.object(processes, 'Path', side_effect=path), patch.object(processes, 'process_cgroup', return_value='/system.slice/streamhome-operation@' + operation + '.service'), patch('lifecycle.control.administration_lock.process_identity', return_value=('300', 'S')) as identity:
            processes.require_service_quiescent(controller=(operation, 200, '300'))
            peers.append(Process('201'))
            with self.assertRaisesRegex(ControlError, 'control_foreign_service_process'):
                processes.require_service_quiescent(controller=(operation, 200, '300'))
            peers.pop();identity.return_value = ('301', 'S')
            with self.assertRaisesRegex(ControlError, 'control_controller_changed'):
                processes.require_service_quiescent(controller=(operation, 200, '300'))

    def test_independent_retry_reader_rejects_foreign_service_work_and_unknown_processes(self):
        class Process:
            name = '200'
            def __init__(self, uid, status, denied=False):
                self.uid, self.status, self.denied = uid, status, denied
            def stat(self):
                if self.denied:
                    raise PermissionError('unavailable process metadata')
                return SimpleNamespace(st_uid=self.uid)
            def __truediv__(self, name):
                return SimpleNamespace(read_text=lambda: '200 (fixture) ' + self.status + ' 0')
        for process, code in ((Process(1000, 'S'), 'control_foreign_service_process'),
                              (Process(1000, 'Z'), None), (Process(0, 'S'), None),
                              (Process(1000, 'S', denied=True), 'control_quiescence_unproven')):
            def path(name):
                return SimpleNamespace(iterdir=lambda: [process]) if name == '/proc' else SimpleNamespace(read_text=lambda: '')
            with self.subTest(code=code), patch.object(processes.os, 'geteuid', return_value=1000), patch.object(processes.os, 'getpid', return_value=100), patch.object(processes, 'Path', side_effect=path):
                if code is None:
                    processes.require_service_quiescent()
                else:
                    with self.assertRaisesRegex(ControlError, code):
                        processes.require_service_quiescent()

    def test_bounded_quiescence_wait_requires_transient_work_to_actually_exit(self):
        observations=iter(['S','Z'])
        class Process:
            name='200'
            def stat(self):return SimpleNamespace(st_uid=1000)
            def __truediv__(self,name):
                return SimpleNamespace(read_text=lambda:'200 (fixture) '+next(observations)+' 0')
        def path(name):
            if name=='/proc':return SimpleNamespace(iterdir=lambda:[Process()])
            return SimpleNamespace(read_text=lambda:'')
        with patch.object(processes.os,'geteuid',return_value=1000),patch.object(
            processes.os,'getpid',return_value=100),patch.object(processes,'Path',side_effect=path):
            processes.require_service_quiescent(settle_timeout=.1)

    def test_bounded_wait_cannot_exempt_persistent_foreign_work_or_unknown_metadata(self):
        class Process:
            name='200'
            def stat(self):return SimpleNamespace(st_uid=1000)
            def __truediv__(self,name):return SimpleNamespace(read_text=lambda:'200 (fixture) S 0')
        def path(name):
            if name=='/proc':return SimpleNamespace(iterdir=lambda:[Process()])
            return SimpleNamespace(read_text=lambda:'')
        with patch.object(processes.os,'geteuid',return_value=1000),patch.object(
            processes.os,'getpid',return_value=100),patch.object(processes,'Path',side_effect=path):
            with self.assertRaisesRegex(ControlError,'control_foreign_service_process'):
                processes.require_service_quiescent(settle_timeout=.04)
            with patch.object(Process,'stat',side_effect=PermissionError('unknown')),patch.object(
                processes.time,'sleep') as sleep:
                with self.assertRaisesRegex(ControlError,'control_quiescence_unproven'):
                    processes.require_service_quiescent(settle_timeout=2)
                sleep.assert_not_called()
        for invalid in (True,-1,3,float('nan'),float('inf')):
            with self.subTest(invalid=invalid),self.assertRaises(ControlError):
                processes.require_service_quiescent(settle_timeout=invalid)

    def test_exit_between_pidfd_open_and_proc_read_is_not_a_foreign_signal(self):
        with patch.object(processes.os,'pidfd_open',return_value=123), patch.object(processes,'process_cgroup',side_effect=FileNotFoundError), patch.object(processes.signal,'pidfd_send_signal') as send, patch.object(processes.os,'close') as close:
            processes.signal_owned(200,signal.SIGTERM)
        send.assert_not_called()
        close.assert_called_once_with(123)

    def test_operation_unit_is_never_signalled_as_a_runtime_child(self):
        with patch.object(processes.os,'pidfd_open',return_value=123), patch.object(processes,'process_cgroup',return_value='/system.slice/streamhome-operation@other.service'), patch.object(processes.signal,'pidfd_send_signal') as send, patch.object(processes.os,'close') as close:
            with self.assertRaises(ControlError) as error:
                processes.signal_owned(200,signal.SIGKILL)
        self.assertEqual(error.exception.code,'control_process_changed')
        send.assert_not_called()
        close.assert_called_once_with(123)


if __name__ == '__main__':
    unittest.main()
