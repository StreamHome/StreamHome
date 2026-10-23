import signal
import uuid
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from lifecycle.control import processes
from lifecycle.control.rpc import ControlError


class OwnedProcessSignalTests(unittest.TestCase):
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
