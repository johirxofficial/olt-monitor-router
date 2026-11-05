"""Logging setup: console + rotating file under ``logs/``."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

_CONFIGURED = False


def setup_logging(level: str = "INFO", root: Path | None = None) -> logging.Logger:
    """Configure root logging once; safe to call many times."""
    global _CONFIGURED
    logger = logging.getLogger("olt_monitor")
    if _CONFIGURED:
        return logger

    log_dir = (root or Path.cwd()) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "olt_monitor.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    if sys.stderr is not None:
        # pythonw.exe (the auto-start task) has no console at all
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(fmt)
        root_logger.addHandler(console)
    root_logger.addHandler(file_handler)

    _CONFIGURED = True
    return logger


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"olt_monitor.{name}")
