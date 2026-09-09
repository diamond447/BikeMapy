"""Privacy-conscious logging and error-monitoring helpers.

The application deliberately keeps diagnostics useful without turning request
logs or error events into a second store of report contents and client data.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import math
import re
from datetime import UTC, datetime
from typing import Any, cast

import sentry_sdk
from sentry_sdk.types import Event, Hint

_IP_CANDIDATE_PATTERN = re.compile(r"(?<![0-9A-Fa-f:.])[0-9A-Fa-f:.%]+(?![0-9A-Fa-f:.])")
_SECRET_PATTERN = re.compile(
    r"(?i)(\b(?:authorization|cookie|password|secret|token|api[_-]?key)\s*[:=]\s*)"
    r"(?:Bearer\s+)?[^,\s]+"
)
# Sentry serializes arbitrary values in several event sections.  Keep only
# fields that are useful for diagnosing an application failure and whose
# values do not contain request/report content.  In particular, do not rely
# on key-based redaction for these sections: exception values, frame context,
# breadcrumb fields, and custom contexts are all user-controlled strings.
_EVENT_LEVELS = frozenset({"debug", "info", "warning", "error", "fatal"})
_EVENT_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z", re.IGNORECASE)
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}\Z")
_BREADCRUMB_LEVELS = frozenset({"debug", "info", "warning", "error", "fatal"})
_BREADCRUMB_TYPES = frozenset(
    {"default", "http", "navigation", "query", "ui", "user", "system", "error", "debug"}
)
_BREADCRUMB_CATEGORIES = frozenset(
    {"console", "django", "fetch", "http", "navigation", "query", "task", "ui", "xhr"}
)
_TRACE_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z", re.IGNORECASE)
_SPAN_ID_PATTERN = re.compile(r"[0-9a-f]{16}\Z", re.IGNORECASE)


def _scrub_text(value: str) -> str:
    value = _SECRET_PATTERN.sub(r"\1[REDACTED]", value)

    def replace_ip(match: re.Match[str]) -> str:
        candidate = match.group(0).rstrip(".")
        try:
            ipaddress.ip_address(candidate.split("%", 1)[0])
        except ValueError:
            return match.group(0)
        return "[REDACTED_IP]"

    return _IP_CANDIDATE_PATTERN.sub(replace_ip, value)


def _scrub_exception(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    cleaned: dict[str, Any] = {}
    for key in ("module", "type"):
        candidate = value.get(key)
        if isinstance(candidate, str) and _IDENTIFIER_PATTERN.fullmatch(candidate):
            cleaned[key] = candidate
    mechanism = value.get("mechanism")
    if isinstance(mechanism, dict):
        safe_mechanism: dict[str, Any] = {}
        mechanism_type = mechanism.get("type")
        if isinstance(mechanism_type, str) and _IDENTIFIER_PATTERN.fullmatch(mechanism_type):
            safe_mechanism["type"] = mechanism_type
        for key in ("handled", "synthetic"):
            if isinstance(mechanism.get(key), bool):
                safe_mechanism[key] = mechanism[key]
        cleaned["mechanism"] = safe_mechanism
    stacktrace = value.get("stacktrace")
    if isinstance(stacktrace, dict):
        cleaned["stacktrace"] = _scrub_stacktrace(stacktrace)
    return cleaned


def _scrub_stacktrace(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    frames = value.get("frames")
    if not isinstance(frames, list):
        return {}
    return {"frames": [_scrub_frame(frame) for frame in frames if isinstance(frame, dict)]}


def _scrub_frame(value: dict[str, Any]) -> dict[str, Any]:
    """Retain typed source coordinates, never user-controlled source strings."""

    cleaned: dict[str, Any] = {}
    for key in ("lineno", "colno"):
        candidate = value.get(key)
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            cleaned[key] = candidate
    if isinstance(value.get("in_app"), bool):
        cleaned["in_app"] = value["in_app"]
    return cleaned


def _scrub_contexts(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    trace = value.get("trace")
    if not isinstance(trace, dict):
        return {}
    cleaned_trace: dict[str, Any] = {}
    trace_id = trace.get("trace_id")
    if isinstance(trace_id, str) and _TRACE_ID_PATTERN.fullmatch(trace_id):
        cleaned_trace["trace_id"] = trace_id
    for key in ("span_id", "parent_span_id"):
        span_id = trace.get(key)
        if span_id is None and key == "parent_span_id":
            cleaned_trace[key] = None
        elif isinstance(span_id, str) and _SPAN_ID_PATTERN.fullmatch(span_id):
            cleaned_trace[key] = span_id
    return {"trace": cleaned_trace} if {"trace_id", "span_id"} & cleaned_trace.keys() else {}


def _safe_timestamp(value: Any) -> str | int | float | None:
    """Accept only finite numbers or timezone-aware ISO-8601 timestamps."""

    if isinstance(value, int | float) and not isinstance(value, bool):
        return value if math.isfinite(value) else None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if parsed.tzinfo is not None else None


def _scrub_breadcrumbs(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    values = value.get("values")
    if not isinstance(values, list):
        return {}
    cleaned_values: list[dict[str, Any]] = []
    for breadcrumb in values:
        if not isinstance(breadcrumb, dict):
            continue
        cleaned: dict[str, Any] = {}
        timestamp = _safe_timestamp(breadcrumb.get("timestamp"))
        if timestamp is not None:
            cleaned["timestamp"] = timestamp
        for key, allowed in (
            ("type", _BREADCRUMB_TYPES),
            ("category", _BREADCRUMB_CATEGORIES),
            ("level", _BREADCRUMB_LEVELS),
        ):
            candidate = breadcrumb.get(key)
            if isinstance(candidate, str) and candidate in allowed:
                cleaned[key] = candidate
        cleaned_values.append(cleaned)
    return {"values": cleaned_values}


def _scrub_threads(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    values = value.get("values")
    if not isinstance(values, list):
        return {}
    cleaned_values: list[dict[str, Any]] = []
    for thread in values:
        if not isinstance(thread, dict):
            continue
        cleaned: dict[str, Any] = {}
        thread_id = thread.get("id")
        if isinstance(thread_id, int) and not isinstance(thread_id, bool):
            cleaned["id"] = thread_id
        for key in ("crashed", "current"):
            if isinstance(thread.get(key), bool):
                cleaned[key] = thread[key]
        cleaned["stacktrace"] = _scrub_stacktrace(thread.get("stacktrace"))
        cleaned_values.append(cleaned)
    return {"values": cleaned_values}


def _scrub_event_metadata(value: Any, *, transaction: bool = False) -> dict[str, Any]:
    """Keep only scalar event metadata with a fixed, verifiable shape."""

    if not isinstance(value, dict):
        return {}
    cleaned: dict[str, Any] = {}
    event_id = value.get("event_id")
    if isinstance(event_id, str) and _EVENT_ID_PATTERN.fullmatch(event_id):
        cleaned["event_id"] = event_id
    for key in ("timestamp", "start_timestamp"):
        timestamp = _safe_timestamp(value.get(key))
        if timestamp is not None:
            cleaned[key] = timestamp
    level = value.get("level")
    if isinstance(level, str) and level in _EVENT_LEVELS:
        cleaned["level"] = level
    if value.get("platform") == "python":
        cleaned["platform"] = "python"
    if transaction and value.get("type") == "transaction":
        cleaned["type"] = "transaction"
    return cleaned


class JsonFormatter(logging.Formatter):
    """Emit one stable JSON object per log record."""

    def format(self, record: logging.LogRecord) -> str:
        message = _scrub_text(record.getMessage())
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": message,
        }
        task_name = getattr(record, "task_name", None)
        if task_name:
            payload["task"] = _scrub_text(str(task_name))
        if record.exc_info:
            # Exception text can contain request data. Keep the event useful
            # for triage while leaving the complete traceback to Sentry.
            exception_type = record.exc_info[0]
            if exception_type is not None:
                payload["exception"] = exception_type.__name__
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


class PrivacyLogFilter(logging.Filter):
    """Remove secret-like values before formatters or handlers see a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _scrub_text(record.getMessage())
        record.args = ()
        return True


def scrub_event(event: Event | dict[str, Any], hint: Hint | None = None) -> Event:
    """Keep only privacy-safe Sentry event metadata and diagnostic structure.

    Report text and contact data can enter an event through exception values,
    traceback locals, breadcrumbs, or custom contexts.  Those structures are
    therefore copied using explicit allow-lists instead of recursively
    retaining arbitrary values. Top-level metadata is also type-validated;
    logger names and other arbitrary diagnostic strings are never retained.
    """

    del hint
    cleaned = _scrub_event_metadata(event)
    if "stacktrace" in event:
        cleaned["stacktrace"] = _scrub_stacktrace(event["stacktrace"])
    exceptions = event.get("exception")
    if isinstance(exceptions, dict):
        exception_values = exceptions.get("values")
        if isinstance(exception_values, list):
            cleaned["exception"] = {
                "values": [_scrub_exception(value) for value in exception_values]
            }
    threads = event.get("threads")
    if isinstance(threads, dict):
        cleaned["threads"] = _scrub_threads(threads)
    if "breadcrumbs" in event:
        cleaned["breadcrumbs"] = _scrub_breadcrumbs(event["breadcrumbs"])
    if "contexts" in event:
        cleaned["contexts"] = _scrub_contexts(event["contexts"])
    return cast(Event, cleaned)


def scrub_transaction(event: Event | dict[str, Any], hint: Hint | None = None) -> Event:
    """Keep only privacy-safe metadata for Sentry transaction events.

    Transactions use a separate SDK callback and can contain report data in
    their name, span data, tags, measurements, or arbitrary extras.  Keep the
    event identity/timing and the same validated trace/breadcrumb metadata;
    drop the transaction payload itself and all other user-controlled fields.
    """

    del hint
    cleaned = _scrub_event_metadata(event, transaction=True)
    if "contexts" in event:
        cleaned["contexts"] = _scrub_contexts(event["contexts"])
    if "breadcrumbs" in event:
        cleaned["breadcrumbs"] = _scrub_breadcrumbs(event["breadcrumbs"])
    return cast(Event, cleaned)


def init_sentry() -> None:
    """Initialize Sentry only when a deployment explicitly supplies a DSN."""

    from django.conf import settings

    dsn = getattr(settings, "SENTRY_DSN", "")
    if not dsn:
        return
    sentry_sdk.init(
        dsn=dsn,
        send_default_pii=False,
        include_local_variables=False,
        before_send=scrub_event,
        before_send_transaction=scrub_transaction,
        traces_sample_rate=float(getattr(settings, "SENTRY_TRACES_SAMPLE_RATE", 0)),
        environment=getattr(settings, "SENTRY_ENVIRONMENT", "production"),
    )
