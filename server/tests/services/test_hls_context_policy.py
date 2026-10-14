"""Exact operator policy cannot widen credentials, origin or redirect authority."""
import json
import unittest

from services.hls_context import (ContextPolicyError, SourceContextPolicy,
                                  parse_hls_context_permissions, plain_origin)
from services.ingestion_security import UnsafeIngestionSource, validate_headers


class HLSContextPolicyTests(unittest.TestCase):
    def policy(self, source="https://source.example", targets=None, headers=None):
        permissions = parse_hls_context_permissions(json.dumps({source: targets or ["https://cdn.example"]}))
        return SourceContextPolicy.capture(permissions, source + "/master.txt", headers or {
            "Origin": "https://player.example", "Referer": "https://player.example/",
            "Cookie": "synthetic=session", "Authorization": "Bearer synthetic",
        })

    def test_empty_policy_keeps_existing_cross_origin_credential_boundary(self):
        policy = SourceContextPolicy.capture(parse_hls_context_permissions("{}"), "https://source.example/root",
                                            {"Origin": "https://player.example", "Cookie": "synthetic=only-root"})
        self.assertEqual(policy.select({"Origin": "https://player.example", "Cookie": "synthetic=only-root"},
                                       "https://source.example/root", "https://cdn.example/chunk"), {})

    def test_approved_context_reappears_after_unapproved_hop_without_credentials(self):
        policy = self.policy()
        initial = {"Origin": "https://player.example", "Referer": "https://player.example/",
                   "Cookie": "synthetic=session", "Authorization": "Bearer synthetic"}
        unapproved = policy.select(initial, "https://source.example/master", "https://foreign.example/next")
        self.assertEqual(unapproved, {})
        approved = policy.select(unapproved, "https://foreign.example/next", "https://cdn.example/segment")
        self.assertEqual(approved, {"origin": "https://player.example", "referer": "https://player.example/"})
        returned = policy.select(approved, "https://cdn.example/segment", "https://source.example/root")
        self.assertEqual(returned, approved)
        self.assertNotIn("Cookie", returned)
        self.assertNotIn("Authorization", returned)

    def test_private_path_query_fragment_and_userinfo_context_never_cross(self):
        for value in ("https://player.example/private", "https://player.example/?secret=synthetic",
                      "https://player.example/#fragment", "https://user:synthetic@player.example/",
                      "https://player.example?", "https://player.example#", " https://player.example",
                      "https://player.example:\t", "https://player.example:",
                      "http://0x7f.0.0.1", "http://0x7f.1", "http://0x8.8.8.8", "http://8.8.0x8.8",
                      "http://１２７。１"):
            with self.subTest(value=value):
                policy = self.policy(headers={"Referer": value})
                self.assertEqual(policy.select({"Referer": value}, "https://source.example/root",
                                               "https://cdn.example/segment"), {})

    def test_exact_scheme_host_and_port_and_no_hostname_pattern_inference(self):
        policy = self.policy()
        for target in ("https://cdn.example:8443/chunk", "https://cdn.example.attacker.test/chunk",
                       "https://another-cdn.example/chunk"):
            self.assertEqual(policy.select({}, "https://source.example/root", target), {})
        self.assertEqual(plain_origin("HTTPS://CDN.EXAMPLE.:443"), plain_origin("https://cdn.example"))

    def test_every_downgrade_edge_is_rejected_even_from_original_http_input(self):
        policy = self.policy("http://source.example", ["https://cdn.example", "http://destination.example"])
        selected = policy.select({}, "http://source.example/root", "https://cdn.example/next")
        with self.assertRaises(ContextPolicyError):
            policy.select(selected, "https://cdn.example/next", "http://destination.example/media")
        policy = self.policy()
        with self.assertRaises(ContextPolicyError):
            policy.select({}, "https://source.example/root", "http://cdn.example/media")

    def test_independent_input_context_and_immutable_permission_snapshot(self):
        video = self.policy(headers={"Origin": "https://video-player.example"})
        audio = self.policy(headers={"Origin": "https://audio-player.example"})
        self.assertEqual(video.select({}, "https://source.example/root", "https://cdn.example/media"),
                         {"origin": "https://video-player.example"})
        self.assertEqual(audio.select({}, "https://source.example/root", "https://cdn.example/media"),
                         {"origin": "https://audio-player.example"})
        with self.assertRaises(AttributeError):
            video.allowed = frozenset()

    def test_configuration_rejects_duplicate_canonical_origins_and_ambiguous_inputs(self):
        invalid = ['{"https://source.example":[],"https://source.example":[]}',
                   '{"https://source.example":[],"https://SOURCE.EXAMPLE:443":[]}',
                   '{"https://source.example":["https://cdn.example","https://CDN.EXAMPLE:443"]}',
                   '[]', 'null', '{"https://source.example":null}',
                   '{"https://source.example":[true]}', '{"https://source.example":[0]}']
        for origin in ("https://source.example/", "https://source.example/path", "https://source.example?",
                       "https://source.example#", "https://source.example:", " https://source.example",
                       "https://source.example\n", "https://user@source.example", "https://*.example",
                       "file://source.example", "https://source.example:0", "https://source.example:65536",
                       "https://[fe80::1%25eth0]", "https://127.1", "https://01.2.3.4", "https://0x7f000001",
                       "http://0x7f.0.0.1", "http://0X7F.0x0.1", "http://0177.0.0.1",
                       "http://0x7f.1", "http://0x8.8.8.8", "http://8.8.0x8.8", "http://１２７。１"):
            invalid.append(json.dumps({origin: []}))
            invalid.append(json.dumps({"https://source.example": [origin]}))
        for raw in invalid:
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(ContextPolicyError, "^Invalid HLS context-header configuration$"):
                    parse_hls_context_permissions(raw)

    def test_configuration_and_header_limits_are_explicit(self):
        invalid = [" " * (64 * 1024 + 1),
                   json.dumps({f"https://source{i}.example": [] for i in range(65)}),
                   json.dumps({"https://source.example": [f"https://target{i}.example" for i in range(33)]})]
        for raw in invalid:
            with self.assertRaises(ContextPolicyError):
                parse_hls_context_permissions(raw)
        with self.assertRaises(UnsafeIngestionSource):
            validate_headers({"Origin": "https://player.example", "origin": "https://other.example"})


if __name__ == "__main__":
    unittest.main()
