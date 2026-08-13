import os
import logging
import re
from logging.handlers import RotatingFileHandler

# Resolve server root relative to this file
config_dir = os.path.dirname(os.path.abspath(__file__))
server_root = os.path.abspath(os.path.join(config_dir, ".."))
temp_dir = os.path.join(server_root, "temp")
os.makedirs(temp_dir, exist_ok=True)

log_file = os.path.join(temp_dir, "app.log")

# Setup logger
logger = logging.getLogger("streamhome")
logger.setLevel(logging.INFO)

# Clear existing handlers to prevent duplicate messages if re-imported
if logger.handlers:
    logger.handlers.clear()

# Formatter
formatter = logging.Formatter(
    "[%(asctime)s] [%(levelname)s] [%(name)s]: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# Console handler
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# Rotating file handler
file_handler = RotatingFileHandler(
    log_file,
    maxBytes=5 * 1024 * 1024,  # 5 MB
    backupCount=3,
    encoding="utf-8"
)
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)


_SENSITIVE_QUERY_VALUE = re.compile(
    r"([?&](?:ticket|[a-z0-9_-]*token|auth(?:orization)?|api[_-]?key|password|passwd|[a-z0-9_-]*secret|signature|sig|code|state)=)[^&#\s\"]*",
    re.IGNORECASE,
)


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
