import os
import logging
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


class _MediaAccessFilter(logging.Filter):
    """Keep high-volume internal media probes and streaming reads out of the console."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return '"GET /media/' not in message and '"HEAD /media/' not in message


def install_uvicorn_access_filter() -> None:
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, _MediaAccessFilter) for item in access_logger.filters):
        access_logger.addFilter(_MediaAccessFilter())


install_uvicorn_access_filter()
