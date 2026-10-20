"""Structured JSON logging, shared by every StudySlot process.

Each call to log_event() writes ONE line of JSON to stdout (which Filebeat
ships to Elasticsearch) and to logs/app.log. SERVICE_NAME can be overridden
with an environment variable so the gRPC service (Part B) can label its own logs.
"""
import json
import logging
import os
import sys
from datetime import datetime, timezone

SERVICE_NAME = os.getenv("SERVICE_NAME", "studyslot")

_logger = logging.getLogger(SERVICE_NAME)
_logger.setLevel(logging.INFO)
_logger.propagate = False
if not _logger.handlers:
    # 1) Print to the screen / container output (Filebeat reads this).
    _stream_handler = logging.StreamHandler(sys.stdout)
    _stream_handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_stream_handler)

    # 2) Also keep a copy in logs/app.log.
    os.makedirs("logs", exist_ok=True)
    _file_handler = logging.FileHandler("logs/app.log", encoding="utf-8")
    _file_handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_file_handler)


def log_event(message: str, *, level: str = "INFO", **fields) -> None:
    """Write one JSON log line with timestamp, service name, level, message and extra fields."""
    record = {
        "@timestamp": datetime.now(timezone.utc).isoformat(),
        "service": {"name": SERVICE_NAME},
        "log": {"level": level.lower()},
        "message": message,
        **fields,
    }
    line = json.dumps(record, separators=(",", ":"))
    getattr(_logger, level.lower(), _logger.info)(line)