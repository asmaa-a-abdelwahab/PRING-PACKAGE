from __future__ import annotations

import logging
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler


def setup_logging(
    *,
    log_dir: Path,
    console_level: str = "INFO",
    file_level: str = "DEBUG",
) -> Path:
    """Configure concise console logging + detailed rotating file logging."""

    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "pring.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    # Clear existing handlers to avoid duplicates when running in notebooks/tests
    for h in list(root.handlers):
        root.removeHandler(h)

    # Console (brief)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(getattr(logging, console_level.upper(), logging.INFO))
    ch.setFormatter(logging.Formatter("%(levelname).1s %(message)s"))
    root.addHandler(ch)

    # File (detailed)
    fh = RotatingFileHandler(log_path, maxBytes=25_000_000, backupCount=5, encoding="utf-8")
    fh.setLevel(getattr(logging, file_level.upper(), logging.DEBUG))
    fh.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(name)s:%(lineno)d | %(message)s")
    )
    root.addHandler(fh)

    # Reduce noise from dependencies unless debugging
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("neo4j").setLevel(logging.INFO)

    return log_path
