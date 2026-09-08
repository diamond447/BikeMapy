"""Privacy-conscious logging and error-monitoring helpers.

The application deliberately keeps diagnostics useful without turning request
logs or error events into a second store of report contents and client data.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
from collections.abc import MutableMapping
from datetime import UTC, date, datetime
from typing import Any, cast

import sentry_sdk
from sentry_sdk.types import Event, Hint

_IP_CANDIDATE_PATTERN = re.compile(r"(?<![0-9A-Fa-f:.])[0-9A-Fa-f:.%]+(?![0-9A-Fa-f:.])")
_SECRET_PATTERN = re.compile(
    r"(?i)(\b(?:authorization|cookie|password|secret|token|api[_-]?key)\s*[:=]\s*)"
    r"(?:Bearer\s+)?[^,\s]+"
)
_SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
    "api_key",
    "ip_address",
    "remote_addr",
    "user_ip",
    "client_ip",
    "remote_ip",
    "client_address",
    "ip",
}


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


def _scrub_value(value: Any, *, key: str = "") -> Any:
    normalized_key = key.lower().replace("-", "_")
    if normalized_key in _SENSITIVE_KEYS or any(
        marker in normalized_key
        for marker in ("password", "secret", "token", "ip_address", "client_ip", "remote_ip")
    ):
        return "[REDACTED]"
    if isinstance(value, str):
        return _scrub_text(value)
    if isinstance(value, MutableMapping):
        return {
            str(child_key): _scrub_value(child, key=str(child_key))
            for child_key, child in value.items()
        }
    if isinstance(value, list | tuple):
        return [_scrub_value(child) for child in value]
    if isinstance(value, ipaddress.IPv4Address | ipaddress.IPv6Address):
        return "[REDACTED_IP]"
    if isinstance(value, datetime | date):
        return str(value)
    return value


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
    """Sentry ``before_send`` callback that drops direct request identifiers."""

    del hint
    cleaned = _scrub_value(event)
    if not isinstance(cleaned, dict):
        return cast(Event, event)
    request = cleaned.get("request")
    if isinstance(request, dict):
        request.pop("env", None)
        request.pop("headers", None)
        request.pop("data", None)
        request.pop("query_string", None)
    user = cleaned.get("user")
    if isinstance(user, dict):
        user.pop("ip_address", None)
        user.pop("email", None)
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
        before_send=scrub_event,
        traces_sample_rate=float(getattr(settings, "SENTRY_TRACES_SAMPLE_RATE", 0)),
        environment=getattr(settings, "SENTRY_ENVIRONMENT", "production"),
    )
