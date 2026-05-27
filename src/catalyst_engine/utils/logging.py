"""Structured logging via structlog.

Every log line is JSON when running non-interactively, pretty-printed when on
a TTY. Modules call `get_logger(__name__)` and bind contextual fields.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from catalyst_engine.config import get_settings


def configure_logging() -> None:
    """Configure structlog once at process start."""
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    is_tty = sys.stderr.isatty()

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if is_tty:
        renderer: Any = structlog.dev.ConsoleRenderer(colors=True)
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound logger. Call once at module top."""
    return structlog.get_logger(name)
