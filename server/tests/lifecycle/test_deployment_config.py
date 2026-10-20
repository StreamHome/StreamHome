from __future__ import annotations

import json
import unittest
import uuid

from lifecycle.deployment_config import (
    AccessMode, DeploymentConfigError, default_deployment, normalize_public_origin,
    parse_deployment_json, parse_deployment_payload, patch_deployment,
)


class DeploymentConfigTests(unittest.TestCase):
    def test_defaults_keep_backend_private_and_derive_coherent_origins(self) -> None:
        config = default_deployment()
        self.assertEqual(config.web_bind, "127.0.0.1")
        self.assertFalse(config.secure_cookies)
        self.assertEqual(config.allowed_origins, ("http://localhost:3000",))
        self.assertEqual(config.startup_environment()["PUBLIC_URL"], "http://localhost:3000")
        self.assertNotIn("BACKEND_BIND", config.startup_environment())
        self.assertEqual(parse_deployment_json(json.dumps(config.payload())), config)

    def test_port_change_updates_direct_origin_but_not_proxy_origin(self) -> None:
        config = patch_deployment(default_deployment(), {"web_port": "04000"})
        self.assertEqual(config.web_port, 4000)
        self.assertEqual(config.public_origin, "http://localhost:4000")
        proxy = patch_deployment(config, {"access_mode": "reverse_proxy", "public_origin": "https://Media.Example:443/"})
        self.assertEqual(proxy.public_origin, "https://media.example")
        self.assertTrue(proxy.secure_cookies)
        self.assertEqual(patch_deployment(proxy, {"web_port": 5000}).public_origin, proxy.public_origin)

    def test_port_rejects_malformed_backend_and_privileged_values(self) -> None:
        for value in (True, False, 0, 443, 8000, 65536, 3000.0, "٣٠٠٠", "3e3", "3000junk", " 3000", "+3000", "030000", None):
            with self.subTest(value=value), self.assertRaises(DeploymentConfigError):
                patch_deployment(default_deployment(), {"web_port": value})
        for value in (1024, 65535):
            self.assertEqual(patch_deployment(default_deployment(), {"web_port": value}).web_port, value)

    def test_origins_canonicalize_scheme_host_ports_and_ipv6(self) -> None:
        for raw, expected in (
            ("HTTPS://MEDIA.Example:443/", "https://media.example"),
            ("http://192.168.1.5:3000/", "http://192.168.1.5:3000"),
            ("https://[2001:db8::0001]:8443", "https://[2001:db8::1]:8443"),
            ("https://xn--bcher-kva.example", "https://xn--bcher-kva.example"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_public_origin(raw), expected)

    def test_ambiguous_origins_and_browser_numeric_hosts_are_rejected(self) -> None:
        for raw in (
            "https://user:pass@example.com", "https://example.com/path", "https://example.com?",
            "https://example.com#", "https://example.com\\evil", "https://example.com,https://evil.example",
            " https://example.com", "https://example.com.", "https://*.example.com", "https://bücher.example",
            "http://0.0.0.0", "http://[::]", "https://127.1", "https://2130706433", "https://0x7f000001",
            "https://0177.0.0.1", "https://foo.123", "https://example.com:0", "https://example.com:65536",
            "https://exa_mple.com", "https://example.com\x1b[2J", "https://%65xample.com", "file://example.com",
        ):
            with self.subTest(raw=raw), self.assertRaises(DeploymentConfigError):
                normalize_public_origin(raw)

    def test_mode_and_binding_validation(self) -> None:
        lan = patch_deployment(default_deployment(), {"access_mode": "lan", "public_origin": "http://192.168.1.5:3000"})
        self.assertEqual(lan.web_bind, "0.0.0.0")
        for patch in (
            {"access_mode": "lan"},
            {"access_mode": "lan", "public_origin": "http://localhost:3000"},
            {"access_mode": "lan", "public_origin": "http://192.168.1.5:4000"},
            {"access_mode": "lan", "public_origin": "https://192.168.1.5:3000"},
            {"public_origin": "http://evil.example:3000"},
            {"public_origin": "http://[::1]:3000"},
            {"public_origin": "http://127.0.0.2:3000"},
            {"access_mode": "reverse_proxy", "public_origin": "http://media.example"},
            {"unknown": "ignored"},
        ):
            with self.subTest(patch=patch), self.assertRaises(DeploymentConfigError):
                patch_deployment(default_deployment(), patch)

    def test_trust_never_becomes_universal_or_remote_for_local_connectors(self) -> None:
        for proxies in ([], ["0.0.0.0/0"], ["::/0"], ["224.0.0.0/4"], ["192.168.1.0/24"], ["127.0.0.1/24"], ["127.0.0.1", "127.0.0.1/32"]):
            with self.subTest(proxies=proxies), self.assertRaises(DeploymentConfigError):
                patch_deployment(default_deployment(), {"trusted_proxy_cidrs": proxies})
        proxy = patch_deployment(default_deployment(), {
            "access_mode": "reverse_proxy", "public_origin": "https://media.example",
            "trusted_proxy_cidrs": ["192.168.1.2/32"],
        })
        self.assertEqual(proxy.trusted_proxy_cidrs, ("192.168.1.2/32",))

    def test_cloudflare_requires_existing_connector_reference_not_token(self) -> None:
        payload = default_deployment().payload()
        payload.update(access_mode="cloudflare", public_origin="https://media.example", cloudflare_connector_id=str(uuid.uuid4()))
        config = parse_deployment_payload(payload)
        self.assertIs(config.access_mode, AccessMode.CLOUDFLARE)
        self.assertEqual(config.web_bind, "127.0.0.1")
        for connector in (None, "a-token-is-not-a-reference", str(uuid.UUID(int=0))):
            with self.subTest(connector=connector), self.assertRaises(DeploymentConfigError):
                parse_deployment_payload({**payload, "cloudflare_connector_id": connector})
        with self.assertRaises(DeploymentConfigError):
            patch_deployment(default_deployment(), {"cloudflare_connector_id": str(uuid.uuid4())})

    def test_allowed_origins_are_exact_bounded_and_deduplicated(self) -> None:
        config = patch_deployment(default_deployment(), {"extra_allowed_origins": ["https://media.example/"]})
        self.assertEqual(config.allowed_origins, ("http://localhost:3000", "https://media.example"))
        for values in (["*"], ["http://localhost:3000/"], ["https://media.example", "https://MEDIA.example/"], ["https://a.example"] * 9):
            with self.subTest(values=values), self.assertRaises(DeploymentConfigError):
                patch_deployment(config, {"extra_allowed_origins": values})

    def test_json_rejects_duplicates_nonfinite_and_unknown_fields(self) -> None:
        raw = json.dumps(default_deployment().payload())
        for invalid in (raw.replace('"web_port": 3000', '"web_port": 3000, "web_port": 4000'), raw.replace('3000,', 'NaN,'), b'\xff', "[" * 2000, " " * 17000):
            with self.subTest(invalid=repr(invalid)[:80]), self.assertRaises(DeploymentConfigError):
                parse_deployment_json(invalid)
        with self.assertRaises(DeploymentConfigError):
            parse_deployment_payload({**default_deployment().payload(), "token": "never accepted"})


if __name__ == "__main__":
    unittest.main()
