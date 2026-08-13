from __future__ import annotations

import logging
import unittest

from services.logger import _SafeAccessFilter, install_uvicorn_access_filter, redact_access_log_target


class AccessLogSecurityRegression(unittest.TestCase):
    def test_redacts_every_supported_sensitive_query_value(self) -> None:
        target = (
            "/api/playback/hls/m_1/segment.m4s?ticket=playback-secret&source_id=main"
            "&access_token=oauth-secret&api-key=integration-secret&signature=signed-secret"
            "&refresh_token=refresh-secret&client_secret=client-secret"
            "&code=oauth-code&state=oauth-state"
        )

        redacted = redact_access_log_target(target)

        self.assertEqual(redacted.count("[REDACTED]"), 8)
        self.assertIn("source_id=main", redacted)
        for secret in (
            "playback-secret",
            "oauth-secret",
            "integration-secret",
            "signed-secret",
            "refresh-secret",
            "client-secret",
            "oauth-code",
            "oauth-state",
        ):
            self.assertNotIn(secret, redacted)

    def test_redacts_uvicorn_request_target_without_losing_diagnostics(self) -> None:
        record = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            '%s - "%s %s HTTP/%s" %d',
            (
                "198.51.100.20:1234",
                "GET",
                "/api/playback/manifest/m_1?ticket=live-ticket&quality=original",
                "1.1",
                200,
            ),
            None,
        )

        self.assertTrue(_SafeAccessFilter().filter(record))
        rendered = record.getMessage()
        self.assertNotIn("live-ticket", rendered)
        self.assertIn("ticket=[REDACTED]", rendered)
        self.assertIn("quality=original", rendered)
        self.assertIn("GET /api/playback/manifest/m_1", rendered)
        self.assertTrue(rendered.endswith('HTTP/1.1" 200'))

    def test_redacts_preformatted_records_and_retains_media_suppression(self) -> None:
        filter_instance = _SafeAccessFilter()
        playback_record = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            '198.51.100.20 - "GET /api/playback/source/m_1?TOKEN=secret HTTP/1.1" 206',
            (),
            None,
        )
        media_record = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            '127.0.0.1 - "HEAD /media/private.mp4?ticket=secret HTTP/1.1" 403',
            (),
            None,
        )

        self.assertTrue(filter_instance.filter(playback_record))
        self.assertNotIn("secret", playback_record.getMessage())
        self.assertFalse(filter_instance.filter(media_record))
        self.assertNotIn("secret", media_record.getMessage())

    def test_installer_is_idempotent(self) -> None:
        access_logger = logging.getLogger("uvicorn.access")
        install_uvicorn_access_filter()
        before = sum(isinstance(item, _SafeAccessFilter) for item in access_logger.filters)
        install_uvicorn_access_filter()
        after = sum(isinstance(item, _SafeAccessFilter) for item in access_logger.filters)

        self.assertEqual(before, 1)
        self.assertEqual(after, 1)


if __name__ == "__main__":
    unittest.main()
