import ipaddress
import json
import logging
import os
import subprocess
from pathlib import Path

import sentry_sdk
from django.conf import settings
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport

from config.observability import JsonFormatter, scrub_event


def test_json_logs_redact_secrets_and_ip_addresses() -> None:
    record = logging.LogRecord(
        "test",
        logging.ERROR,
        __file__,
        1,
        "request from %s token=%s",
        ("192.0.2.4", "secret"),
        None,
    )
    payload = json.loads(JsonFormatter().format(record))
    assert payload["message"] == "request from [REDACTED_IP] token=[REDACTED]"
    assert payload["level"] == "ERROR"


def test_json_logs_redact_compressed_ipv6_and_bearer_token() -> None:
    record = logging.LogRecord(
        "test",
        logging.WARNING,
        __file__,
        1,
        "peer=%s Authorization: Bearer %s",
        ("2001:db8::1", "super-secret-token"),
        None,
    )
    payload = json.loads(JsonFormatter().format(record))
    assert "2001:db8::1" not in payload["message"]
    assert "super-secret-token" not in payload["message"]
    assert "[REDACTED_IP]" in payload["message"]
    assert "Authorization: [REDACTED]" in payload["message"]


def test_sentry_scrubbing_removes_request_pii() -> None:
    event = {
        "request": {
            "headers": {"Authorization": "Bearer secret"},
            "env": {"REMOTE_ADDR": "192.0.2.4"},
            "data": {"message": "a report"},
        },
        "user": {"ip_address": "192.0.2.4", "email": "reporter@example.test"},
        "extra": {
            "token": "secret",
            "message": "failed",
            "client_ip": ipaddress.ip_address("2001:db8::2"),
        },
    }
    scrubbed = scrub_event(event)
    assert "headers" not in scrubbed["request"]
    assert "data" not in scrubbed["request"]
    assert "env" not in scrubbed["request"]
    assert scrubbed["user"] == {}
    assert scrubbed["extra"]["token"] == "[REDACTED]"
    assert scrubbed["extra"]["client_ip"] == "[REDACTED]"


def test_configured_sentry_capture_runs_before_send_scrubber() -> None:
    captured: list[dict[str, object]] = []

    class MemoryTransport(Transport):
        def capture_envelope(self, envelope: Envelope) -> None:
            for item in envelope.items:
                if item.type == "event":
                    captured.append(json.loads(item.get_bytes().decode("utf-8")))

    sentry_sdk.init(
        dsn="https://public@example.invalid/1",
        transport=MemoryTransport,
        before_send=scrub_event,
        send_default_pii=False,
    )
    scope = sentry_sdk.Scope()
    scope.set_user({"ip_address": "2001:db8::1", "email": "person@example.test"})
    scope.set_extra("Authorization", "Bearer top-secret")
    sentry_sdk.capture_message("report processing failed", scope=scope)
    sentry_sdk.flush()
    sentry_sdk.get_client().close()

    assert captured
    event = captured[0]
    assert "2001:db8::1" not in repr(event)
    assert "person@example.test" not in repr(event)
    assert "top-secret" not in repr(event)
    assert settings.SENTRY_RETENTION_DAYS == 30


def test_nginx_diagnostic_adapter_redacts_native_error_details(tmp_path: Path) -> None:
    fake_nginx = tmp_path / "nginx"
    fake_nginx.write_text(
        "#!/bin/sh\n"
        "echo '2026/01/01 client: 172.20.0.1/2001:db8::1, request: "
        '"GET /api/test?token=raw-secret '
        'HTTP/1.1", upstream: "http://backend:8000/api/test?token=upstream-secret"\' >&2\n'
        "exit 1\n"
    )
    fake_nginx.chmod(0o755)
    adapter = Path(__file__).parents[3] / "deploy" / "nginx-json-entrypoint.sh"
    environment = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    result = subprocess.run([str(adapter)], capture_output=True, text=True, env=environment)
    assert result.returncode == 1
    assert "172.20.0.1" not in result.stdout
    assert "2001:db8::1" not in result.stdout
    assert "raw-secret" not in result.stdout
    assert "upstream-secret" not in result.stdout
    assert "?token" not in result.stdout
