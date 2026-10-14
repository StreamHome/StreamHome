"""Restore frontend launch configuration and its production forwarding consumer."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from lifecycle import restore_controller


class RestoreFrontendEnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="restore-frontend-env-")
        self.root = Path(self.temporary.name)
        (self.root / "web").mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _launch(self, content: str | None, ambient: dict[str, str] | None = None) -> dict[str, str]:
        if content is not None:
            (self.root / ".env").write_text(content, encoding="utf-8")
        else:
            (self.root / ".env").unlink(missing_ok=True)
        process = mock.Mock(pid=12345)
        process.poll.return_value = None
        response = mock.MagicMock()
        response.__enter__.return_value.headers = {"X-StreamHome-Web-Build": "synthetic-build"}
        opener = mock.Mock()
        opener.open.return_value = response
        runtime = mock.Mock()
        with (
            mock.patch.dict(os.environ, ambient or {}, clear=True),
            mock.patch.object(restore_controller.subprocess, "Popen", return_value=process) as launch,
            mock.patch.object(restore_controller, "_runtime_token", return_value="synthetic-token"),
            mock.patch.object(restore_controller, "_runtime_control", return_value=runtime),
            mock.patch.object(restore_controller.urlrequest, "build_opener", return_value=opener),
        ):
            self.assertIs(restore_controller._start_web(self.root, 3456, "synthetic-build"), process)
        launch.assert_called_once()
        arguments, keywords = launch.call_args
        self.assertEqual(arguments[0], [
            str(self.root / "web/node_modules/.bin/tsx"), str(self.root / "web/server.ts"),
        ])
        self.assertEqual(keywords["cwd"], str(self.root / "web"))
        self.assertIs(keywords["start_new_session"], True)
        self.assertEqual(keywords["stdin"], subprocess.DEVNULL)
        self.assertEqual(keywords["stdout"], subprocess.DEVNULL)
        self.assertEqual(keywords["stderr"], subprocess.DEVNULL)
        environment = keywords["env"]
        self.assertEqual(environment["STREAMHOME_INSTANCE_ROOT"], str(self.root))
        self.assertEqual(environment["STREAMHOME_INSTANCE_TOKEN"], "synthetic-token")
        self.assertEqual(environment["STREAMHOME_SERVICE"], "web")
        self.assertEqual(environment["STREAMHOME_BUILD_ID"], "synthetic-build")
        self.assertEqual(environment["WEB_PORT"], "3456")
        self.assertEqual(environment["NODE_ENV"], "production")
        runtime.record_service.assert_called_once_with(self.root, "web", 12345, "synthetic-token")
        opener.open.assert_called_once_with("http://127.0.0.1:3456/api/health/live", timeout=1)
        process.terminate.assert_not_called()
        return environment

    def test_clean_operator_environment_uses_exact_configured_cidrs(self) -> None:
        environment = self._launch(
            '\ufeff# Synthetic installation\nexport TRUSTED_PROXY_CIDRS="127.0.0.1/32, ::1/128, 192.0.2.0/24"\n'
            "PUBLIC_URL=https://synthetic.example\nSETUP=true\n"
        )
        self.assertEqual(environment["TRUSTED_PROXY_CIDRS"], "127.0.0.1/32, ::1/128, 192.0.2.0/24")
        self.assertEqual(environment["PUBLIC_URL"], "https://synthetic.example")
        self.assertEqual(environment["SETUP"], "true")

    def test_missing_or_empty_config_uses_loopback_default_over_stale_ambient(self) -> None:
        for content in (None, "# No configured proxy\n", "TRUSTED_PROXY_CIDRS=\n", "TRUSTED_PROXY_CIDRS=''\n"):
            with self.subTest(content=content):
                environment = self._launch(content, {"TRUSTED_PROXY_CIDRS": "0.0.0.0/0,::/0"})
                self.assertEqual(environment["TRUSTED_PROXY_CIDRS"], "127.0.0.1/32,::1/128")
                self.assertEqual(environment["PUBLIC_URL"], "http://localhost:3456")
                self.assertEqual(environment["SETUP"], "false")

    def test_root_config_replaces_ambient_trust_without_expanding_it(self) -> None:
        environment = self._launch("TRUSTED_PROXY_CIDRS=192.0.2.0/24\n", {
            "TRUSTED_PROXY_CIDRS": "203.0.113.0/24", "SYNTHETIC_INHERITED": "preserved",
        })
        self.assertEqual(environment["TRUSTED_PROXY_CIDRS"], "192.0.2.0/24")
        self.assertEqual(environment["SYNTHETIC_INHERITED"], "preserved")

    def test_actual_forwarding_consumer_uses_the_restore_child_configuration(self) -> None:
        project = Path(__file__).resolve().parents[3]
        node = shutil.which("node")
        loader = project / "web/node_modules/tsx/dist/loader.mjs"
        if node is None or not loader.is_file():
            self.skipTest("Installed Node.js and TSX are required for the forwarding consumer")
        cases = []
        inputs = (
            ("TRUSTED_PROXY_CIDRS=192.0.2.0/24\n", {}, "192.0.2.23", True),
            ("TRUSTED_PROXY_CIDRS=2001:db8::/32\n", {}, "2001:db8::23", True),
            (None, {"TRUSTED_PROXY_CIDRS": "0.0.0.0/0"}, "192.0.2.23", False),
            ("TRUSTED_PROXY_CIDRS=\n", {"TRUSTED_PROXY_CIDRS": "0.0.0.0/0"}, "192.0.2.23", False),
            ("TRUSTED_PROXY_CIDRS=192.0.2.0/24\n", {"TRUSTED_PROXY_CIDRS": "203.0.113.0/24"}, "203.0.113.23", False),
            ("TRUSTED_PROXY_CIDRS=invalid\n", {"TRUSTED_PROXY_CIDRS": "0.0.0.0/0"}, "192.0.2.23", False),
        )
        for content, ambient, peer, trusted in inputs:
            environment = self._launch(content, ambient)
            cases.append({"cidrs": environment["TRUSTED_PROXY_CIDRS"], "peer": peer, "trusted": trusted})
        script = """
import fs from 'node:fs';
import { pathToFileURL } from 'node:url';
const { proxyForwardingHeaders } = await import(pathToFileURL(process.argv[1]).href);
const cases = JSON.parse(fs.readFileSync(0, 'utf8'));
console.log(JSON.stringify(cases.map(({cidrs, peer}) => proxyForwardingHeaders({
  socket: {remoteAddress: peer},
  headers: {
    host: 'synthetic.example',
    'x-forwarded-for': '198.51.100.7',
    'x-forwarded-proto': 'https',
    'x-forwarded-host': 'synthetic-proxy.example',
  },
}, cidrs))));
"""
        completed = subprocess.run(
            [node, "--import", str(loader), "--input-type=module", "-e", script, str(project / "web/proxySecurity.ts")],
            cwd=project / "web", input=json.dumps(cases), text=True, capture_output=True, timeout=15,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        outputs = json.loads(completed.stdout)
        self.assertEqual(len(outputs), len(cases))
        for case, output in zip(cases, outputs):
            with self.subTest(case=case):
                self.assertEqual(output, {
                    "x-forwarded-for": "198.51.100.7" if case["trusted"] else case["peer"],
                    "x-forwarded-proto": "https" if case["trusted"] else "http",
                    "x-forwarded-host": "synthetic-proxy.example" if case["trusted"] else "synthetic.example",
                })


if __name__ == "__main__":
    unittest.main()
