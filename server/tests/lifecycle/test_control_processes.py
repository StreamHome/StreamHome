import signal
import unittest
from unittest.mock import patch

from lifecycle.control import processes
from lifecycle.control.rpc import ControlError


class OwnedProcessSignalTests(unittest.TestCase):
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
