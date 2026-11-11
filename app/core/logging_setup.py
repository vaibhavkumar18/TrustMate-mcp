
"""
Central logging configuration for TRUSTMATE-MCP.

Suppresses INFO and DEBUG logs from application and external libraries.
WARNING and ERROR logs remain visible for debugging.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone


_STANDARD_LOG_RECORD_ATTRS = frozenset({
    "args", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "message", "module", "msecs", "msg",
    "name", "pathname", "process", "processName", "relativeCreated",
    "stack_info", "thread", "threadName", "color_message", "levelprefix",
    "taskName",
})

_KNOWN_STRUCTURED_FIELDS = (
    "provider",
    "operation",
    "endpoint",
    "method",
    "http_status_code",
    "error_code",
    "exception_type",
    "sanitized_error_body",
    "duration_ms",
    "country",
    "currency",
    "attempts",
    "attempt",
    "query",
    "provider_status",
    "product_count",
    "offer_count",
    "offers_ranked",
    "offers_recommended",
    "error_type",
    "seller_key",
    "provider_check",
    "provider_error_code",
    "cache_scope",
    "cache_path",
    "cache_ttl_days",
    "identity_match",
    "location_hint_source",
    "location_hint_used",
    "result_count",
    "seller_count",
    "offers_blocked_by_recommendation_gate",
)


class StructuredFormatter(logging.Formatter):
    """Format application logs with structured fields."""

    _BLOCKED = frozenset({
        "api_key",
        "key",
        "secret",
        "token",
        "password",
        "authorization",
        "x_api_key",
        "auth",
        "credential",
        "credentials",
    })

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(
            record.created,
            tz=timezone.utc,
        ).strftime("%Y-%m-%d %H:%M:%S")

        base = (
            f"{timestamp}  "
            f"{record.levelname:<8s}  "
            f"{record.name}  "
            f"{record.getMessage()}"
        )

        extra = {
            key: value
            for key, value in vars(record).items()
            if key not in _STANDARD_LOG_RECORD_ATTRS
            and key not in self._BLOCKED
            and not key.startswith("_")
        }

        if not extra:
            return base

        parts = []
        emitted = set()

        for field in _KNOWN_STRUCTURED_FIELDS:
            if field in extra:
                parts.append(_render(field, extra[field]))
                emitted.add(field)

        for field in sorted(extra):
            if field not in emitted:
                parts.append(_render(field, extra[field]))

        return base + "  " + "  ".join(parts)


def _render(key: str, value: object) -> str:
    """Render structured fields safely."""

    if value is None:
        return f"{key}=None"

    if isinstance(value, bool):
        return f"{key}={value}"

    if isinstance(value, (int, float)):
        return f"{key}={value}"

    rendered = str(value)

    if any(character in rendered for character in (" ", "=", '"')):
        rendered = rendered.replace('"', '\\"')
        return f'{key}="{rendered}"'

    return f"{key}={rendered}"


def configure_logging(log_level: str = "WARNING") -> None:
    """
    Suppress INFO and DEBUG logs.

    WARNING, ERROR and CRITICAL logs remain visible.
    """

    requested_level = getattr(
        logging,
        str(log_level).upper(),
        logging.WARNING,
    )

    effective_level = max(
        requested_level,
        logging.WARNING,
    )

    app_logger = logging.getLogger("app")

    structured_handler = next(
        (
            handler
            for handler in app_logger.handlers
            if isinstance(handler.formatter, StructuredFormatter)
        ),
        None,
    )

    if structured_handler is None:
        structured_handler = logging.StreamHandler(sys.stderr)
        structured_handler.setFormatter(StructuredFormatter())
        app_logger.addHandler(structured_handler)

    structured_handler.setLevel(effective_level)
    app_logger.setLevel(effective_level)
    app_logger.propagate = False

    # Suppress repetitive third-party HTTP, MCP and server INFO logs.
    noisy_loggers = (
        "httpx",
        "httpcore",
        "mcp",
        "uvicorn",
        "uvicorn.access",
        "uvicorn.error",
        "watchfiles",
    )

    for logger_name in noisy_loggers:
        logging.getLogger(logger_name).setLevel(effective_level)
