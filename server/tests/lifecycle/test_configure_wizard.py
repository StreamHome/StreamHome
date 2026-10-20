from __future__ import annotations

import io
import unittest
import uuid

from lifecycle.configure_wizard import (
    ConfigurationDraft, configure_wizard, installation_wizard,
    parse_configuration_request, parse_installation_request, parse_request_json,
)
from lifecycle.deployment_config import DeploymentConfigError, default_deployment, patch_deployment
from lifecycle.terminal import Console, UserCancelled


class ConfigureWizardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.current = ConfigurationDraft(default_deployment(), True)
        self.output = io.StringIO()
        self.checked = []

    def _diagnostics(self, config):
        self.checked.append(config)
        return ("Local listener check passed; public reachability is not yet verified.",)

    def _configure(self, answers: str, **kwargs):
        return configure_wizard(
            Console(io.StringIO(answers), self.output, interactive=True), self.current,
            running=True, diagnostics=self._diagnostics, **kwargs,
        )

    def test_noop_and_cancel_do_not_request_application_or_restart(self) -> None:
        for answers in ("\n", "0\n", "1\n4000\n0\n", "1\n4000\n7\nn\n"):
            with self.subTest(answers=answers):
                self.assertIsNone(self._configure(answers))
        self.assertEqual(self.current.deployment.web_port, 3000)
        self.assertNotIn("Restart StreamHome", self.output.getvalue())

    def test_save_only_keeps_active_snapshot_and_reports_pending(self) -> None:
        review = self._configure("1\n4000\n7\n\nn\n")
        self.assertEqual(review.before, self.current)
        self.assertEqual(review.after.deployment.web_port, 4000)
        self.assertEqual(review.after.deployment.allowed_origins, ("http://localhost:4000",))
        self.assertFalse(review.activate_deployment)
        self.assertTrue(review.restart_required)
        self.assertIn("will remain pending", self.output.getvalue())
        self.assertEqual(self.checked, [review.after.deployment])

    def test_apply_and_restart_are_separate_yes_defaults(self) -> None:
        review = self._configure("1\n4000\n7\n\n\n")
        self.assertTrue(review.activate_deployment)
        self.assertIn("Apply these changes? [Y/n]", self.output.getvalue())
        self.assertIn("Restart StreamHome to apply these changes? [Y/n]", self.output.getvalue())

    def test_back_abandons_edit_and_blank_preserves_field(self) -> None:
        self.assertIsNone(self._configure("1\nback\n1\n\n7\n"))
        self.assertIn("Edit abandoned", self.output.getvalue())

    def test_eof_does_not_approve_valid_draft(self) -> None:
        with self.assertRaises(UserCancelled):
            self._configure("1\n4000\n7\n")

    def test_mode_edit_is_coherent_and_public_origin_automatically_allowed(self) -> None:
        review = self._configure("2\nreverse_proxy\nhttps://media.example\n127.0.0.1/32\n7\ny\nn\n")
        config = review.after.deployment
        self.assertEqual(config.public_origin, "https://media.example")
        self.assertIn(config.public_origin, config.allowed_origins)
        self.assertTrue(config.secure_cookies)
        self.assertEqual(config.web_bind, "127.0.0.1")

    def test_invalid_access_edit_leaves_prior_draft_and_can_be_cancelled(self) -> None:
        self.assertIsNone(self._configure("2\nlan\nhttp://localhost:3000\n7\n"))
        self.assertIn("LAN access requires", self.output.getvalue())

    def test_boot_only_edit_does_not_request_endpoint_restart(self) -> None:
        review = self._configure("5\nn\n7\ny\n")
        self.assertFalse(review.after.start_at_boot)
        self.assertFalse(review.restart_required)
        self.assertFalse(review.activate_deployment)
        self.assertNotIn("Restart StreamHome", self.output.getvalue())

    def test_diagnostics_failure_blocks_review_and_keeps_draft(self) -> None:
        def occupied(config):
            raise DeploymentConfigError("Selected port is owned by another process.")
        result = configure_wizard(
            Console(io.StringIO("1\n4000\n7\n0\n"), self.output, interactive=True),
            self.current, running=True, diagnostics=occupied,
        )
        self.assertIsNone(result)
        self.assertIn("another process", self.output.getvalue())
        self.assertNotIn("Apply these changes?", self.output.getvalue())

    def test_installation_has_fixed_paths_and_can_select_lan(self) -> None:
        draft = installation_wizard(
            Console(io.StringIO("4000\nlan\nhttp://192.168.1.10:4000\n\n\n"), self.output, interactive=True),
            diagnostics=self._diagnostics,
        )
        self.assertEqual(draft.deployment.web_port, 4000)
        self.assertEqual(draft.deployment.web_bind, "0.0.0.0")
        self.assertTrue(draft.start_at_boot)
        self.assertIn("/opt/streamhome", self.output.getvalue())
        self.assertNotIn("Installation directory [", self.output.getvalue())

    def test_cloudflare_selects_protected_reference_without_requesting_token(self) -> None:
        connector = str(uuid.uuid4())
        review = self._configure(
            f"2\ncloudflare\nhttps://media.example\n{connector}\n7\ny\nn\n", connectors=(connector,),
        )
        self.assertEqual(review.after.deployment.cloudflare_connector_id, connector)
        self.assertNotIn("Token:", self.output.getvalue())

    def test_pending_saved_endpoint_is_shown_separately_from_active(self) -> None:
        current = ConfigurationDraft(patch_deployment(default_deployment(), {"web_port": 4000}), True)
        self.assertIsNone(configure_wizard(
            Console(io.StringIO("0\n"), self.output, interactive=True), current, running=True,
            diagnostics=self._diagnostics, active_deployment=default_deployment(),
        ))
        self.assertIn("Currently active deployment:", self.output.getvalue())
        self.assertIn("http://localhost:3000", self.output.getvalue())
        self.assertIn("saved deployment below is pending", self.output.getvalue())
        self.assertIn("http://localhost:4000", self.output.getvalue())

    def test_noninteractive_install_requires_complete_settings_and_explicit_consent(self) -> None:
        payload = {"schema_version": 1, "deployment": default_deployment().payload(), "start_at_boot": False, "install": True}
        self.assertEqual(parse_installation_request(payload), ConfigurationDraft(default_deployment(), False))
        for patch in ({"install": False}, {"install": 1}, {"start_at_boot": "yes"}, {"deployment": {}}, {"schema_version": True}):
            with self.subTest(patch=patch), self.assertRaises(DeploymentConfigError):
                parse_installation_request({**payload, **patch})
        with self.assertRaises(DeploymentConfigError):
            parse_installation_request({})

    def test_noninteractive_configure_binds_base_and_separates_save_from_activation(self) -> None:
        identity = str(uuid.uuid4())
        payload = {
            "schema_version": 1, "expected_desired_revision": identity, "deployment_patch": {"web_port": 4000},
            "start_at_boot": True, "apply": True, "activate_deployment": False,
        }
        request = parse_configuration_request(payload, self.current)
        self.assertEqual(request.expected_desired_revision, identity)
        self.assertFalse(request.review.activate_deployment)
        self.assertEqual(request.review.after.deployment.web_port, 4000)
        for patch in ({"apply": 1}, {"apply": False}, {"expected_desired_revision": "latest"}, {"activate_deployment": "yes"}, {"deployment_patch": {"token": "unsupported"}}):
            with self.subTest(patch=patch), self.assertRaises(DeploymentConfigError):
                parse_configuration_request({**payload, **patch}, self.current)

    def test_request_json_rejects_duplicate_nonfinite_and_unbounded_input(self) -> None:
        self.assertEqual(parse_request_json(b'{"apply":true}'), {"apply": True})
        for raw in (b'{"apply":false,"apply":true}', b'{"value":NaN}', b'[]', b'\xff', b' ' * 33000):
            with self.subTest(raw=raw[:30]), self.assertRaises(DeploymentConfigError):
                parse_request_json(raw)


if __name__ == "__main__":
    unittest.main()
