import os
import logging
import re
from logging.handlers import RotatingFileHandler


# Quotes and angle brackets may belong to a submitted path or an escaped dump;
# they cannot prove where a private URL ends. Whitespace bounds the diagnostic.
_SOURCE_URL = re.compile(r"https?:(?:\\*/){2}\S+", re.IGNORECASE)
_SQL_PARAMETERS = re.compile(r"(^\[parameters:\s*)[^\r\n]*", re.IGNORECASE | re.MULTILINE)
_SENSITIVE_QUERY_VALUE = re.compile(
    r"([?&](?:ticket|[a-z0-9_-]*token|auth(?:orization)?|api[_-]?key|password|passwd|[a-z0-9_-]*secret|signature|sig|code|state)=)[^&#\s\"]*",
    re.IGNORECASE,
)
_SENSITIVE_HEADER = re.compile(
    r"(?<![?&a-z0-9_/-])([\\]*[\"']?(?:authorization|proxy-authorization|cookie|set-cookie|[a-z0-9_-]*(?:token|secret|password|api[-_]?key|signature)[a-z0-9_-]*)[\\]*[\"']?\s*[:=]\s*)"
    r"[^\r\n]*",
    re.IGNORECASE,
)


def redact_diagnostic(value: str) -> str:
    """Keep source addresses and credentials out of application diagnostics."""
    # Positional SQL parameters can contain private input without a sensitive
    # field label. Keep the statement and driver error, not the parameter values.
    value = _SQL_PARAMETERS.sub(r"\1[REDACTED]", value)
    # SQL repr and nested JSON add escaping layers with ambiguous quote bounds.
    # Redact the sensitive record's tail rather than trusting a quote delimiter.
    value = _SENSITIVE_HEADER.sub(r"\1[REDACTED]", value)
    value = _SOURCE_URL.sub("[REDACTED_URL]", value)
    return _SENSITIVE_QUERY_VALUE.sub(r"\1[REDACTED]", value)


class _SafeDiagnosticFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_diagnostic(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = redact_diagnostic(logging.Formatter().formatException(record.exc_info))
        elif record.exc_text:
            record.exc_text = redact_diagnostic(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_diagnostic(record.stack_info)
        return True


class _SafeDiagnosticFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Exception text is rendered after filters run, so sanitize it as well.
        return redact_diagnostic(super().format(record))

# Resolve server root relative to this file
config_dir = os.path.dirname(os.path.abspath(__file__))
server_root = os.path.abspath(os.path.join(config_dir, ".."))
temp_dir = os.path.join(server_root, "temp")
log_file = os.path.join(temp_dir, "app.log")

# Setup logger
logger = logging.getLogger("streamhome")
logger.setLevel(logging.INFO)

# Clear existing handlers to prevent duplicate messages if re-imported
if logger.handlers:
    logger.handlers.clear()

# Formatter
formatter = _SafeDiagnosticFormatter(
    "[%(asctime)s] [%(levelname)s] [%(name)s]: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# Console handler
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)
if not any(isinstance(item, _SafeDiagnosticFilter) for item in logger.filters):
    logger.addFilter(_SafeDiagnosticFilter())

def initialize_file_logging() -> None:
    """Enable canonical file logging after lifecycle startup authorizes writes."""

    if any(isinstance(item, RotatingFileHandler) for item in logger.handlers):
        return
    os.makedirs(temp_dir, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)


def redact_access_log_target(value: str) -> str:
    """Remove browser credentials from a request target before access logging."""

    return _SENSITIVE_QUERY_VALUE.sub(r"\1[REDACTED]", value)


class _SafeAccessFilter(logging.Filter):
    """Redact request credentials and suppress high-volume public media probes."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) >= 3:
            arguments = list(record.args)
            arguments[2] = redact_access_log_target(str(arguments[2]))
            record.args = tuple(arguments)
        else:
            rendered = record.getMessage()
            redacted = redact_access_log_target(rendered)
            if redacted != rendered:
                record.msg = redacted
                record.args = ()
        message = record.getMessage()
        return '"GET /media/' not in message and '"HEAD /media/' not in message


def install_uvicorn_access_filter() -> None:
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, _SafeAccessFilter) for item in access_logger.filters):
        access_logger.addFilter(_SafeAccessFilter())


install_uvicorn_access_filter()
