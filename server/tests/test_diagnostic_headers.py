"""Sensitive header values remain private even inside SQL parameter diagnostics."""
import io
import json
import logging
from pathlib import Path
import tempfile
import traceback
import unittest

from sqlalchemy import create_engine, text
from services.ingestion_errors import write_task_diagnostics
from services.ingestion_security import validate_headers
from services.logger import logger, redact_diagnostic, _SafeDiagnosticFormatter


class DiagnosticHeaderRegression(unittest.TestCase):
    def test_json_and_repr_headers_are_redacted(self):
        for value in ('{"Authorization": "Bearer private-value"}', "{'Cookie': 'session=private-value'}", 'Authorization: Bearer private-value', 'proxy-authorization=private-value', 'X-Api-Key: private-value', '{"X-Auth-Token":"private-value"}'):
            self.assertNotIn('private-value', redact_diagnostic(value))

    def test_escaped_quoted_values_and_nested_serialization_are_complete(self):
        headers = validate_headers({"Authorization": 'Bearer prefix"neutral-private-tail',
                                    "Cookie": 'session="neutral-cookie-tail"'})
        for serialized in (json.dumps(headers), repr(headers), json.dumps(json.dumps(headers)),
                           repr({"Authorization": "value'neutral-private-tail"})):
            with self.subTest(serialized=serialized):
                redacted = redact_diagnostic(serialized)
                self.assertNotIn("neutral-private-tail", redacted)
                self.assertNotIn("neutral-cookie-tail", redacted)

    def test_header_redaction_precedes_url_lexical_replacement(self):
        serialized = json.dumps({"status": 500,
                                 "Authorization": 'https://source.example/path"neutral-private-tail'})
        redacted = redact_diagnostic(serialized)
        self.assertNotIn("neutral-private-tail", redacted)
        self.assertNotIn("source.example", redacted)
        self.assertIn('"status": 500', redacted)

    def test_source_query_names_do_not_consume_the_following_error_message(self):
        redacted = redact_diagnostic("Failed https://source.example/private-path?token=secret-value and returned 404")
        self.assertIn("returned 404", redacted)
        self.assertNotIn("source.example", redacted)
        self.assertNotIn("private-path", redacted)
        self.assertNotIn("secret-value", redacted)

    def test_source_path_punctuation_stays_private_in_all_diagnostic_sinks(self):
        source = 'https://source.example/public\'neutral-private-tail"<private-angle-tail>?opaque=value'
        escaped_slashes = json.dumps({"url": source}).replace("/", "\\/")
        payloads = (source, repr(source), json.dumps(source), json.dumps(json.dumps(source)),
                    repr({"url": source}), json.dumps({"url": source}), escaped_slashes,
                    json.dumps(escaped_slashes))
        application, parent = io.StringIO(), io.StringIO()
        application_handler, parent_handler = logging.StreamHandler(application), logging.StreamHandler(parent)
        application_handler.setFormatter(_SafeDiagnosticFormatter("%(message)s"))
        parent_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(application_handler)
        logging.getLogger().addHandler(parent_handler)
        try:
            with tempfile.TemporaryDirectory() as temporary:
                for index, payload in enumerate(payloads):
                    logger.error("Neutral source failure: %s returned 404", payload)
                    path = write_task_diagnostics(str(index), "source failure", payload, temp_dir=temporary)
                    for emitted in (redact_diagnostic(payload), Path(path).read_text()):
                        self.assertNotIn("neutral-private-tail", emitted)
                        self.assertNotIn("private-angle-tail", emitted)
                        self.assertNotIn("source.example", emitted)
            for emitted in (application.getvalue(), parent.getvalue()):
                self.assertNotIn("neutral-private-tail", emitted)
                self.assertNotIn("private-angle-tail", emitted)
                self.assertNotIn("source.example", emitted)
                self.assertIn("returned 404", emitted)
            record = logging.LogRecord("streamhome", logging.ERROR, __file__, 1,
                "Preformatted source failure", (), None)
            record.exc_text = "TransportError: " + source
            record.stack_info = "Private stack source: " + source
            logger.handle(record)
            for emitted in (application.getvalue(), parent.getvalue()):
                self.assertNotIn("neutral-private-tail", emitted)
                self.assertNotIn("private-angle-tail", emitted)
                self.assertNotIn("source.example", emitted)
        finally:
            logger.removeHandler(application_handler)
            logging.getLogger().removeHandler(parent_handler)

    def test_actual_sql_exception_is_private_in_application_parent_and_durable_sinks(self):
        headers = validate_headers({"Authorization": 'Bearer prefix"neutral-private-tail',
                                    "Cookie": 'session="neutral-cookie-tail"'})
        application, parent = io.StringIO(), io.StringIO()
        application_handler, parent_handler = logging.StreamHandler(application), logging.StreamHandler(parent)
        application_handler.setFormatter(_SafeDiagnosticFormatter("%(message)s"))
        parent_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(application_handler)
        logging.getLogger().addHandler(parent_handler)
        engine = create_engine("sqlite://")
        captured = ""
        try:
            with engine.begin() as connection:
                connection.execute(text("CREATE TABLE neutral (id INTEGER CHECK (id > 0), headers_str TEXT)"))
                for payload in (headers, validate_headers({"User-Agent": "neutral-custom-private-value"})):
                    try:
                        connection.execute(text("INSERT INTO neutral VALUES (:id, :headers_str)"),
                                           {"id": 0, "headers_str": json.dumps(payload)})
                    except Exception:
                        logger.exception("Neutral database failure")
                        captured += traceback.format_exc()
            self.assertTrue(captured)
            with tempfile.TemporaryDirectory() as temporary:
                diagnostic = write_task_diagnostics("neutral", "queue exception", captured, temp_dir=temporary)
                self.assertIsNotNone(diagnostic)
                durable = Path(diagnostic).read_text()
            for emitted in (application.getvalue(), parent.getvalue(), durable):
                self.assertIn("CHECK constraint failed", emitted)
                self.assertNotIn("neutral-private-tail", emitted)
                self.assertNotIn("neutral-cookie-tail", emitted)
                self.assertNotIn("neutral-custom-private-value", emitted)
        finally:
            engine.dispose()
            logger.removeHandler(application_handler)
            logging.getLogger().removeHandler(parent_handler)
