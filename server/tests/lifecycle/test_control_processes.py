import signal
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from lifecycle.control import processes
from lifecycle.control.rpc import ControlError


class OwnedProcessSignalTests(unittest.TestCase):
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
