"""Structured JSON logging for all Mose components."""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class JSONFormatter(logging.Formatter):
    """Format log records as single-line JSON."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Merge extra structured data
        if hasattr(record, "data"):
            entry.update(record.data)
        if record.exc_info and record.exc_info[0]:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def setup_logging(log_dir: str, log_level: str = "INFO") -> None:
    """Configure root logger with JSON file + console handlers."""
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger("mose")
    root.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    root.handlers.clear()

    # JSON file handler — one file per day
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fh = logging.FileHandler(log_path / f"mose-{today}.jsonl", encoding="utf-8")
    fh.setFormatter(JSONFormatter())
    root.addHandler(fh)

    # Console handler — human-readable
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    root.addHandler(ch)


def get_logger(name: str) -> logging.Logger:
    """Get a child logger under the 'mose' namespace."""
    return logging.getLogger(f"mose.{name}")


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **data: Any) -> None:
    """Log a structured event with arbitrary data fields."""
    record = logger.makeRecord(
        logger.name, level, "(observe)", 0, event, (), None
    )
    record.data = data  # type: ignore[attr-defined]
    logger.handle(record)


@contextmanager
def log_duration(logger: logging.Logger, event: str, **extra: Any):
    """Context manager that logs event duration in milliseconds."""
    start = time.monotonic()
    try:
        yield
    finally:
        elapsed_ms = (time.monotonic() - start) * 1000
        log_event(logger, event, latency_ms=round(elapsed_ms, 1), **extra)
