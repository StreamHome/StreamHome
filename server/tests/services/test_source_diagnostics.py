"""Actual relay/native failures never emit the submitted source address."""
import io
import logging
from pathlib import Path
import tempfile
import unittest

from services.ingestion_errors import write_task_diagnostics
from services.logger import logger, _SafeDiagnosticFormatter
from services.media_probe import probe_media_stream
from tests.services import test_controlled_source


class SubmittedSourceDiagnosticRegression(unittest.IsolatedAsyncioTestCase):
    async def test_actual_ambiguous_source_retry_keeps_private_path_out_of_sinks(self):
        origin = test_controlled_source.ControlledSourceTests(methodName="test_malformed_source_urls_are_policy_errors")
        await origin.asyncSetUp()
        application, parent = io.StringIO(), io.StringIO()
        safe_handler, parent_handler = logging.StreamHandler(application), logging.StreamHandler(parent)
        safe_handler.setFormatter(_SafeDiagnosticFormatter("%(message)s"))
        parent_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(safe_handler)
        logging.getLogger().addHandler(parent_handler)
        try:
            origin.routes["/public'neutral-private-tail"] = (200, {}, b"not valid media")
            source = f"http://127.0.0.1:{origin.port}/public'neutral-private-tail"
            result = await probe_media_stream(source, client_address="127.0.0.1")
            self.assertFalse(result["has_video"])
            self.assertIn("Retrying ambiguous HTTP source", application.getvalue())
            with tempfile.TemporaryDirectory() as temporary:
                path = write_task_diagnostics("synthetic", "native source failure",
                    source + "\n" + application.getvalue(), temp_dir=temporary)
                durable = Path(path).read_text()
            for emitted in (application.getvalue(), parent.getvalue(), durable):
                self.assertNotIn("neutral-private-tail", emitted)
                self.assertNotIn("/public", emitted)
        finally:
            logger.removeHandler(safe_handler)
            logging.getLogger().removeHandler(parent_handler)
            await origin.asyncTearDown()
