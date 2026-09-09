"""SDK-level regression coverage for Sentry's report-data boundary."""

import json
import logging
from datetime import datetime
from typing import Any, cast

import sentry_sdk
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport

from config.observability import scrub_event, scrub_transaction


def test_captured_exception_drops_report_data_from_all_sdk_sections() -> None:
    captured: list[dict[str, object]] = []

    class MemoryTransport(Transport):
        def capture_envelope(self, envelope: Envelope) -> None:
            for item in envelope.items:
                if item.type in {"event", "transaction"}:
                    captured.append(json.loads(item.get_bytes().decode("utf-8")))

    previous_client = getattr(sentry_sdk.get_global_scope(), "client", None)
    sentry_sdk.init(
        dsn="https://public@example.invalid/1",
        transport=MemoryTransport,
        before_send=scrub_event,
        before_send_transaction=scrub_transaction,
        send_default_pii=False,
        include_local_variables=False,
        traces_sample_rate=1.0,
    )
    try:
        report_text = "SYNTHETIC REPORT TEXT 82"
        contact_email = "reporter-82@example.test"
        scope = sentry_sdk.Scope()
        scope.set_level(cast(Any, contact_email))
        scope.set_user({"email": contact_email, "ip_address": "192.0.2.82"})
        scope.set_extra("report", report_text)
        scope.set_context("report", {"message": report_text, "email": contact_email})
        scope.set_context("trace", {"trace_id": report_text, "span_id": contact_email})
        scope.add_breadcrumb(
            category="report",
            message=report_text,
            data={"contact_email": contact_email},
        )

        try:
            locals()["local_report"] = report_text
            raise RuntimeError(f"failed to process {report_text} for {contact_email}")
        except RuntimeError:
            sentry_sdk.capture_exception(scope=scope)

        sentry_sdk.capture_event(
            {
                "threads": {
                    "values": [
                        {
                            "name": report_text,
                            "crashed": False,
                            "current": True,
                            "stacktrace": {"frames": []},
                        }
                    ]
                }
            },
            scope=scope,
        )
        sentry_sdk.capture_event(
            cast(
                Any,
                {
                    "timestamp": report_text,
                    "level": contact_email,
                    "logger": contact_email,
                    "platform": report_text,
                    "sdk": {"name": report_text},
                    "measurements": {"report": report_text},
                    "transaction_info": {"source": report_text},
                },
            ),
            scope=scope,
        )
        try:
            raise RuntimeError(report_text)
        except RuntimeError:
            logging.getLogger(contact_email).error(report_text, exc_info=True)
        with sentry_sdk.start_transaction(name=report_text, op="report") as transaction:
            transaction.set_data("report", report_text)
            transaction.set_tag("contact_email", contact_email)

        sentry_sdk.flush()
    finally:
        sentry_sdk.get_client().close()
        sentry_sdk.get_global_scope().set_client(previous_client)

    assert captured
    event = next(event for event in captured if "exception" in event)
    serialized = repr(captured)
    assert report_text not in serialized
    assert contact_email not in serialized
    assert isinstance(event["timestamp"], str)
    datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))

    exception = event["exception"]["values"][0]  # type: ignore[index]
    assert "value" not in exception
    for frame in exception["stacktrace"]["frames"]:
        assert "vars" not in frame
    thread_event = next(event for event in captured if "threads" in event)
    thread = thread_event["threads"]["values"][0]  # type: ignore[index]
    assert "name" not in thread
    breadcrumb = event["breadcrumbs"]["values"][0]  # type: ignore[index]
    assert "category" not in breadcrumb
    assert "message" not in breadcrumb
    assert "data" not in breadcrumb
    assert "report" not in event["contexts"]  # type: ignore[operator]
    assert "extra" not in event
    assert "user" not in event

    transaction_event = next(event for event in captured if event.get("type") == "transaction")
    transaction_serialized = repr(transaction_event)
    assert report_text not in transaction_serialized
    assert contact_email not in transaction_serialized
    assert "transaction" not in transaction_event
    assert "spans" not in transaction_event
    assert isinstance(transaction_event["timestamp"], str)
    assert isinstance(transaction_event["start_timestamp"], str)
    datetime.fromisoformat(transaction_event["timestamp"].replace("Z", "+00:00"))
    datetime.fromisoformat(transaction_event["start_timestamp"].replace("Z", "+00:00"))
