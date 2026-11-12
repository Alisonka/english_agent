"""Logging setup: stdlib `logging` everywhere, readable text in a terminal, JSON lines on the server.

Every module logs through `logging.getLogger(__name__)`. `configure_logging` is called once at
the edge of each process (the bot, the worker, a CLI). `bind(...)` attaches fields such as the
learner's chat id or the job id to every record logged inside the block, including records from
nested calls and from tasks started inside it (they live in a `ContextVar`), and the filter adds
the current OpenTelemetry trace and span ids, so a log line can be joined with its trace.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, TextIO

from opentelemetry import trace
from pythonjsonlogger.core import RESERVED_ATTRS
from pythonjsonlogger.json import JsonFormatter

if TYPE_CHECKING:
    from english_agent.config import Settings

_BOUND: ContextVar[dict[str, Any] | None] = ContextVar("english_agent_log_fields", default=None)
_FIELDS_ATTR = "bound_fields"
_TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_JSON_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

# Libraries that are chatty at INFO, or worse: httpx logs every request URL at INFO, and a
# Telegram Bot API URL embeds the bot token.
_QUIET_LOGGERS = ("httpx", "httpcore", "aiogram.event", "taskiq.receiver.receiver", "apscheduler.executors")


@contextmanager
def bind(**fields: Any) -> Iterator[None]:
    """Add `fields` to every record logged inside the block (nested binds merge, inner wins)."""
    token = _BOUND.set({**(_BOUND.get() or {}), **fields})
    try:
        yield
    finally:
        _BOUND.reset(token)


def bound_fields() -> dict[str, Any]:
    return dict(_BOUND.get() or {})


class ContextFilter(logging.Filter):
    """Copies the bound fields and the current trace/span ids onto the record."""

    def filter(self, record: logging.LogRecord) -> bool:
        fields = bound_fields()
        span = trace.get_current_span().get_span_context()
        if span.is_valid:
            fields["trace_id"] = format(span.trace_id, "032x")
            fields["span_id"] = format(span.span_id, "016x")
        setattr(record, _FIELDS_ATTR, fields)
        return True


class TextFormatter(logging.Formatter):
    """The classic one-line format, with the bound fields appended as key=value."""

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        fields = {k: v for k, v in getattr(record, _FIELDS_ATTR, {}).items() if k not in ("trace_id", "span_id")}
        if not fields:
            return line
        suffix = " ".join(f"{key}={value}" for key, value in fields.items())
        head, newline, rest = line.partition("\n")  # keep a traceback below the fields
        return f"{head} [{suffix}]{newline}{rest}"


class JsonLineFormatter(JsonFormatter):
    """One JSON object per line; the bound fields become top-level keys."""

    def __init__(self) -> None:
        super().__init__(
            _JSON_FORMAT,
            rename_fields={"asctime": "time", "levelname": "level", "name": "logger"},
            reserved_attrs=[*RESERVED_ATTRS, _FIELDS_ATTR],
            json_ensure_ascii=False,
        )

    def add_fields(self, log_data: dict[str, Any], record: logging.LogRecord, message_dict: dict[str, Any]) -> None:
        super().add_fields(log_data, record, message_dict)
        for key, value in getattr(record, _FIELDS_ATTR, {}).items():
            log_data.setdefault(key, value)


class _StderrHandler(logging.StreamHandler[TextIO]):
    """Writes to whatever `sys.stderr` is at the time of the call (pytest swaps it per test)."""

    def __init__(self) -> None:
        super().__init__()

    @property  # type: ignore[override]
    def stream(self) -> TextIO:
        return sys.stderr

    @stream.setter
    def stream(self, value: TextIO) -> None:
        pass


def configure_logging(settings: Settings) -> None:
    """Install the project handler on the root logger. Safe to call more than once."""
    root = logging.getLogger()
    for handler in [h for h in root.handlers if isinstance(h, _StderrHandler)]:
        root.removeHandler(handler)  # only ours: pytest's capture handlers stay
    handler = _StderrHandler()
    handler.addFilter(ContextFilter())
    handler.setFormatter(JsonLineFormatter() if settings.log_format == "json" else TextFormatter(_TEXT_FORMAT))
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
