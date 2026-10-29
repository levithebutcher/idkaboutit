"""
utils/logger.py
---------------
Centralized structured logging.

Features:
- Console output with colors (INFO green, WARNING yellow, ERROR red).
- Optional rotating file handler (10 MB × 3 backups).
- Helper `log_resolution()` emits a single structured JSON line per
  resolution attempt, so logs can be grepped / parsed by log analyzers.
"""

import json
import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import settings


# ---------- ANSI color codes for console ----------
class _Color:
    RESET = "\033[0m"
    GREY = "\033[90m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"


_LEVEL_COLORS = {
    "DEBUG": _Color.GREY,
    "INFO": _Color.GREEN,
    "WARNING": _Color.YELLOW,
    "ERROR": _Color.RED,
    "CRITICAL": _Color.BOLD + _Color.RED,
}


class _ColorFormatter(logging.Formatter):
    """Formatter that adds ANSI colors to console output."""

    def format(self, record: logging.LogRecord) -> str:
        color = _LEVEL_COLORS.get(record.levelname, _Color.RESET)
        record.levelname_colored = f"{color}{record.levelname:<8}{_Color.RESET}"
        record.name_short = record.name.split(".")[-1][:14].ljust(14)
        return super().format(record)


def _build_console_handler() -> logging.Handler:
    handler = logging.StreamHandler(sys.stdout)
    fmt = (
        f"{_Color.GREY}%(asctime)s{_Color.RESET} "
        f"%(levelname_colored)s "
        f"{_Color.CYAN}%(name_short)s{_Color.RESET} "
        f"%(message)s"
    )
    handler.setFormatter(_ColorFormatter(fmt, datefmt="%H:%M:%S"))
    return handler


def _build_file_handler(log_file: str) -> logging.Handler:
    path = Path(log_file)
    path.parent.mkdir(parents=True, exist_ok=True)

    handler = logging.handlers.RotatingFileHandler(
        filename=str(path),
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=3,
        encoding="utf-8",
    )
    fmt = "%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s"
    handler.setFormatter(logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S"))
    return handler


def setup_logging() -> None:
    """
    Configure the root logger. Call this ONCE at bot startup (in bot.py).
    Subsequent calls are idempotent (won't duplicate handlers).
    """
    root = logging.getLogger()
    if root.handlers:
        return  # already configured

    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    root.setLevel(level)

    root.addHandler(_build_console_handler())

    if settings.log_file:
        root.addHandler(_build_file_handler(settings.log_file))

    # Quieten noisy third-party libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Get a named logger. Use `get_logger(__name__)` in every module."""
    return logging.getLogger(name)


# ---------- Structured resolution logging ----------

def log_resolution(
    *,
    user_id: int | None,
    original_url: str,
    domain: str,
    status: str,
    method: str | None,
    latency_ms: int,
    error_class: str | None = None,
    target_url: str | None = None,
) -> None:
    """
    Emit a single structured JSON line for every resolution attempt.
    Useful for analytics, dashboards, or `grep`-based debugging.

    Do NOT include full URLs with tokens in production log retention policies
    without redaction. This is a debug/observability helper.
    """
    logger = get_logger("resolution")
    payload: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "user_id": user_id,
        "domain": domain,
        "status": status,
        "method": method,
        "latency_ms": latency_ms,
        "error_class": error_class,
        "original_url": original_url,
        "target_url": target_url,
    }
    logger.info(json.dumps(payload, ensure_ascii=False))