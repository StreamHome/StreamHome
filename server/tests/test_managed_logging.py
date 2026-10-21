import io
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from services import logger as application_logging


class ManagedLoggingTests(unittest.TestCase):
    def test_file_logging_uses_canonical_settings_only_after_explicit_initialization(self):
        logger = application_logging.logger
        previous = tuple(logger.handlers)
        self.assertFalse(any(isinstance(handler, RotatingFileHandler) for handler in previous))
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'canonical-temp'
            settings = SimpleNamespace(TEMP_DIR=str(destination))
            with patch.dict(sys.modules, {'config': SimpleNamespace(settings=settings)}):
                try:
                    self.assertFalse(destination.exists())
                    application_logging.initialize_file_logging()
                    application_logging.initialize_file_logging()
                    file_handlers = [handler for handler in logger.handlers if isinstance(handler, RotatingFileHandler)]
                    self.assertEqual(len(file_handlers), 1)
                    logger.error('Synthetic failure https://private.example/private-value')
                    file_handlers[0].flush()
                    emitted = (destination / 'app.log').read_text()
                    self.assertIn('Synthetic failure', emitted)
                    self.assertNotIn('private-value', emitted)
                finally:
                    for handler in tuple(logger.handlers):
                        if handler not in previous:
                            logger.removeHandler(handler)
                            handler.close()

    def test_uvicorn_error_parent_and_request_target_do_not_receive_private_source(self):
        application_logging.install_uvicorn_access_filter()
        logger = logging.getLogger('uvicorn.error')
        original_level = logger.level
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        logger.addHandler(handler)
        try:
            logger.setLevel(logging.ERROR)
            try:
                raise ValueError('https://private.example/private-value')
            except ValueError:
                logger.exception('Transport failure')
            self.assertNotIn('private.example', output.getvalue())
            self.assertNotIn('private-value', output.getvalue())
            self.assertIn('Transport failure', output.getvalue())
            for target in ('/api/submit?url=https://private.example/private-value',
                           '/api/submit?url=https%3A%2F%2Fprivate.example%2Fprivate-value'):
                self.assertNotIn('private-value', application_logging.redact_access_log_target(target))
        finally:
            logger.removeHandler(handler)
            logger.setLevel(original_level)


if __name__ == '__main__':
    unittest.main()
