"""Automatic input context preserves source intent without credential propagation."""
import os
import subprocess
import sys
import unittest

from services.hls_context import ContextPolicyError, SourceContextPolicy, plain_origin
from services.ingestion_security import UnsafeIngestionSource, source_headers_for_target, validate_headers

INITIAL = {"Origin": "https://player.example", "Referer": "https://player.example/watch/private?token=synthetic#fragment",
           "Cookie": "synthetic=session", "Authorization": "Bearer synthetic"}


class HLSContextPolicyTests(unittest.TestCase):
    def policy(self, source="https://source.example", headers=None):
        return SourceContextPolicy.capture(source + "/master.txt", INITIAL if headers is None else headers)

    def test_automatic_context_needs_no_permission_or_hostname_family(self):
        policy = self.policy()
        for target in ("https://cdn.example/chunk", "https://another.example:8443/chunk", "https://cdn.example.attacker.test/chunk"):
            selected = policy.select(INITIAL, "https://source.example/root", target)
            self.assertEqual(selected, {"origin": "https://player.example", "referer": "https://player.example/"})
            self.assertEqual(policy.decision(target), "automatic-context")

    def test_generic_http_consumers_keep_strict_default(self):
        self.assertEqual(source_headers_for_target(INITIAL, "https://source.example/root", "https://foreign.example/media"), {})

    def test_foreign_hops_and_return_root_never_restore_credentials(self):
        policy = self.policy()
        foreign = policy.select(INITIAL, "https://source.example/root", "https://foreign.example/next")
        second = policy.select(foreign, "https://foreign.example/next", "https://cdn.example/segment")
        child = policy.select(second, "https://cdn.example/segment", "https://cdn.example/child")
        self.assertEqual(foreign, second)
        self.assertEqual(child, second)
        returned = policy.select(child, "https://cdn.example/child", "https://source.example/root")
        self.assertEqual(returned["referer"], INITIAL["Referer"])
        self.assertEqual(returned["origin"], INITIAL["Origin"])
        self.assertNotIn("Cookie", returned)
        self.assertNotIn("Authorization", returned)
        self.assertEqual(policy.select(INITIAL, "https://source.example/root", "https://source.example/child"), INITIAL)

    def test_referer_projection_preserves_ports_idna_and_ipv6_without_private_parts(self):
        cases = [("HTTPS://PLAYER.EXAMPLE.:443/private?secret=synthetic#fragment", "https://player.example/"),
                 ("https://player.example:8443/private", "https://player.example:8443/"),
                 ("https://bücher.example/private", "https://xn--bcher-kva.example/"),
                 ("https://[2001:4860:4860::8888]:8443/private?secret=synthetic", "https://[2001:4860:4860::8888]:8443/"),
                 ("https://player.example/" + "x" * 3000 + "?secret=synthetic", "https://player.example/")]
        for value, projected in cases:
            with self.subTest(value=value):
                selected = self.policy(headers={"Referer": value}).select({}, "https://source.example/root", "https://cdn.example/chunk")
                self.assertEqual(selected, {"referer": projected})

    def test_invalid_context_is_not_invented_or_forwarded(self):
        invalid = ("null", "file:///private", "https://user:synthetic@player.example/", "https://@player.example/",
                   "https://:@player.example/", " https://player.example",
                   "https://player.example:\t", "https://player.example:", "https://[fe80::1%25eth0]",
                   "http://0x7f.0.0.1", "http://0x7f.1", "http://0x8.8.8.8", "http://8.8.0x8.8",
                   "http://１２７。１", "https://player.example/with raw space", "https://player.example\\private", "x" * 4097)
        for name in ("Origin", "Referer"):
            for value in invalid:
                with self.subTest(name=name, value=value):
                    selected = self.policy(headers={name: value}).select({name: value}, "https://source.example/root", "https://cdn.example/chunk")
                    self.assertEqual(selected, {})
        for value in ("https://player.example/", "https://player.example/private", "https://player.example?secret=synthetic", "https://player.example#fragment"):
            self.assertEqual(self.policy(headers={"Origin": value}).select({}, "https://source.example/root", "https://cdn.example/chunk"), {})
        empty = self.policy(headers={})
        self.assertEqual(empty.select({}, "https://source.example/root", "https://cdn.example/chunk"), {})
        self.assertEqual(empty.decision("https://cdn.example/chunk"), "no-safe-context")

    def test_every_downgrade_edge_is_rejected_even_from_original_http_input(self):
        policy = self.policy("http://source.example")
        selected = policy.select({}, "http://source.example/root", "https://cdn.example/next")
        with self.assertRaises(ContextPolicyError):
            policy.select(selected, "https://cdn.example/next", "http://destination.example/media")
        with self.assertRaises(ContextPolicyError):
            self.policy().select({}, "https://source.example/root", "http://cdn.example/media")

    def test_independent_input_context_and_immutable_header_snapshot(self):
        video_headers = {"Origin": "https://video-player.example"}
        video = self.policy(headers=video_headers)
        audio = self.policy(headers={"Origin": "https://audio-player.example"})
        video_headers["Origin"] = "https://changed.example"
        self.assertEqual(video.select({}, "https://source.example/root", "https://cdn.example/media"), {"origin": "https://video-player.example"})
        self.assertEqual(audio.select({}, "https://source.example/root", "https://cdn.example/media"), {"origin": "https://audio-player.example"})
        with self.assertRaises(AttributeError):
            video.context = ()

    def test_strict_source_origins_and_duplicate_header_names(self):
        self.assertEqual(plain_origin("HTTPS://CDN.EXAMPLE.:443"), plain_origin("https://cdn.example"))
        for origin in ("https://source.example/", "https://source.example?", "https://source.example#", "https://source.example:",
                       "https://user@source.example", "https://*.example", "file://source.example", "https://source.example:0",
                       "https://source.example:65536", "https://[fe80::1%25eth0]", "https://127.1", "https://01.2.3.4",
                       "https://0x7f000001", "http://0x7f.0.0.1", "http://0X7F.0x0.1", "http://0177.0.0.1", "http://１２７。１"):
            with self.subTest(origin=origin):
                with self.assertRaises((ContextPolicyError, ValueError)):
                    plain_origin(origin)
        with self.assertRaises(UnsafeIngestionSource):
            validate_headers({"Origin": "https://player.example", "origin": "https://other.example"})
        with self.assertRaises(ContextPolicyError):
            self.policy(headers={"Origin": "https://player.example", "origin": "https://other.example"})

    def test_retired_manual_environment_cannot_change_startup_or_behavior(self):
        # This process is already in the disposable installation. No real .env is read.
        environment = dict(os.environ, INGEST_HLS_CONTEXT_ORIGINS="not-json-anymore")
        command = [sys.executable, "-c", "from config import settings; from services.hls_context import SourceContextPolicy; "
                   "assert not hasattr(settings, 'HLS_CONTEXT_HEADER_PERMISSIONS'); "
                   "p=SourceContextPolicy.capture('https://source.example/master', {'Origin':'https://player.example'}); "
                   "assert p.select({}, 'https://source.example/root', 'https://cdn.example/part') == {'origin':'https://player.example'}"]
        result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
