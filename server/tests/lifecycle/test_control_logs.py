import io
import json
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from lifecycle.control import logs
from lifecycle.control.rpc import ControlError


class ControlLogBoundaryTests(unittest.TestCase):
    def test_selectors_are_fixed_and_do_not_interpret_a_unit_or_shell_command(self):
        arguments = logs.journal_arguments(lines=25, follow=True, component='all', level='warning')
        self.assertEqual(arguments[0], '/usr/bin/journalctl')
        self.assertIn('--follow', arguments)
        self.assertIn('--priority=4', arguments)
        self.assertIn('--unit=streamhome-prepare@*.service', arguments)
        self.assertIn('--unit=streamhome.service', arguments)
        for options in ({'component': 'ssh.service'}, {'component': '--all'}, {'level': 'arbitrary'},
                        {'lines': True}, {'lines': 0}, {'lines': 1001}, {'follow': 'yes'}):
            with self.subTest(options=options), self.assertRaises(ControlError):
                logs.journal_arguments(**{'lines': 25, 'follow': False, 'component': 'runtime', 'level': 'debug', **options})

    def test_messages_redact_sources_headers_and_credentials_before_terminal_output(self):
        for private in ('https://private.example/path?opaque=source-private-value',
                        'https%3A%2F%2Fprivate.example%2Fsource-private-value',
                        'https%253A%252F%252Fprivate.example%252Fsource-private-value',
                        'Authorization: Bearer source-private-value', '/api/source?ticket=source-private-value'):
            raw = json.dumps({'MESSAGE': 'Failure ' + private, '__REALTIME_TIMESTAMP': '1760000000000000'}).encode()
            rendered = logs.render_record(raw)
            self.assertNotIn('source-private-value', rendered)
            self.assertNotIn('private.example', rendered)
            self.assertIn('Failure', rendered)
            self.assertTrue(rendered.startswith('2025-'))

    def test_terminal_controls_are_inert_and_invalid_records_never_fall_back_to_raw(self):
        rendered = logs.render_record(json.dumps({'MESSAGE': 'before\x1b[2J\n\u202eafter'}).encode())
        self.assertNotIn('\x1b', rendered)
        self.assertNotIn('\n', rendered)
        self.assertNotIn('\u202e', rendered)
        self.assertIn('before', rendered)
        self.assertIn('after', rendered)
        for raw in (b'not-json private-value', b'{"MESSAGE":[112,114,105,118,97,116,101]}', b'[]', b'{"MESSAGE":null}'):
            self.assertIn('omitted', logs.render_record(raw))
            self.assertNotIn('private-value', logs.render_record(raw))

    def test_oversized_record_is_discarded_without_losing_the_next_safe_record(self):
        private = json.dumps({'MESSAGE': 'source-private-value ' + 'x' * (logs.MAX_RECORD * 2)}).encode() + b'\n'
        safe = json.dumps({'MESSAGE': 'safe next record'}).encode() + b'\n'
        process = Mock(stdout=io.BytesIO(private + safe))
        process.wait.return_value = 0
        process.poll.return_value = 0
        output = io.StringIO()
        with patch.object(logs.subprocess, 'Popen', return_value=process) as spawn, patch.object(logs.sys, 'stdout', output):
            self.assertEqual(logs.read_logs(lines=100, follow=False), 0)
        self.assertNotIn('source-private-value', output.getvalue())
        self.assertIn('Oversized journal record omitted', output.getvalue())
        self.assertIn('safe next record', output.getvalue())
        self.assertNotIn('user', spawn.call_args.kwargs)
        self.assertNotIn('group', spawn.call_args.kwargs)
        self.assertNotIn('shell', spawn.call_args.kwargs)
        process.terminate.assert_not_called()
        self.assertTrue(process.stdout.closed)

    def test_interrupt_stops_and_reaps_only_the_owned_journal_reader(self):
        process = Mock()
        process.stdout.readline.side_effect = KeyboardInterrupt
        process.poll.return_value = None
        with patch.object(logs.subprocess, 'Popen', return_value=process):
            self.assertEqual(logs.read_logs(lines=100, follow=True), 130)
        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=5)
        process.stdout.close.assert_called_once_with()

    def test_protected_reader_imports_no_application_or_canonical_implementation(self):
        code = "import sys; import lifecycle.control.logs; assert not {'config','models','database','services.logger','lifecycle.state'}.intersection(sys.modules)"
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
