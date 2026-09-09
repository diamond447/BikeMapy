from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from django.core.exceptions import ValidationError
from django.test import override_settings
from django.utils import timezone

from apps.catalogue.models import (
    ForumPost,
    ForumThread,
    LoopStatus,
    ProcessingStatus,
    Route,
    RouteSource,
    RouteVersion,
)
from apps.ingestion.gpx import (
    ExportedGpx,
    GpxExtractionError,
    GpxExtractionTaskFailure,
    GpxPayloadTooLarge,
    GpxValidationError,
    MapyGpxExporterAdapter,
    PinnedMapyTransport,
    UnsafeGpxUrlError,
    _GuardedHttpClient,
    extract_gpx,
    parse_gpx,
    validate_gpx_content_type,
    validate_gpx_url,
)
from apps.ingestion.models import (
    ExtractionAttempt,
    ExtractionStatus,
    OrphanPayloadCleanup,
    OrphanPayloadStatus,
)
from apps.ingestion.tasks import extract_gpx as extract_gpx_task

pytestmark = pytest.mark.django_db

GPX = b"""<?xml version="1.0"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  <metadata><name>Morning ride</name></metadata><trk><trkseg>
    <trkpt lat="49.000000" lon="16.000000"><ele>100</ele></trkpt>
    <trkpt lat="49.001000" lon="16.001000"><ele>120</ele></trkpt>
    <trkpt lat="49.000000" lon="16.000000"><ele>110</ele></trkpt>
  </trkseg></trk>
</gpx>"""


def source() -> RouteSource:
    thread = ForumThread.objects.create(url="https://forum.test/thread", title="Ride")
    post = ForumPost.objects.create(thread=thread, url="https://forum.test/thread#1")
    route = Route.objects.create()
    route_source = RouteSource.objects.create(route=route, mapy_url="https://mapy.com/s/test")
    route_source.posts.add(post)
    return route_source


def test_parse_gpx_normalizes_geometry_and_optional_metrics() -> None:
    parsed = parse_gpx(GPX)
    assert parsed.geometry == {
        "type": "LineString",
        "coordinates": [[16.0, 49.0], [16.001, 49.001], [16.0, 49.0]],
    }
    assert parsed.distance_m > 0
    assert parsed.elevations == (100.0, 120.0, 110.0)
    assert parsed.ascent_m == 20
    assert parsed.descent_m == 10
    assert parsed.loop_status == LoopStatus.LOOP
    assert parsed.title == "Morning ride"


@pytest.mark.parametrize(
    "payload",
    [
        (
            b'<gpx><trk><trkseg><trkpt lat="91" lon="16"/>'
            b'<trkpt lat="49" lon="16"/></trkseg></trk></gpx>'
        ),
        b"<html>not a route</html>",
        b"<!DOCTYPE gpx [<!ENTITY xxe SYSTEM 'file:///etc/passwd'>]><gpx>&xxe;</gpx>",
    ],
)
def test_hostile_or_invalid_xml_is_rejected(payload: bytes) -> None:
    with pytest.raises(GpxValidationError):
        parse_gpx(payload)


def test_content_and_url_validation_rejects_unsafe_inputs() -> None:
    with pytest.raises(GpxValidationError):
        validate_gpx_content_type("text/html")
    with pytest.raises(UnsafeGpxUrlError):
        validate_gpx_url("http://127.0.0.1/route", check_dns=False)
    with pytest.raises(UnsafeGpxUrlError):
        validate_gpx_url("https://mapy.com:444/route", check_dns=False)
    with pytest.raises(UnsafeGpxUrlError):
        validate_gpx_url("https://evil.mapy.com/route", check_dns=False)


def test_dns_answers_are_checked_for_private_addresses() -> None:
    with patch(
        "apps.ingestion.gpx.socket.getaddrinfo", return_value=[(0, 0, 0, "", ("127.0.0.1", 443))]
    ):
        with pytest.raises(UnsafeGpxUrlError):
            validate_gpx_url("https://mapy.com/s/test")


def test_extraction_is_idempotent_and_publishes_valid_data(tmp_path: Path) -> None:
    route_source = source()
    with override_settings(MEDIA_ROOT=tmp_path):
        first = extract_gpx(route_source.pk, adapter=lambda _: GPX)
        second = extract_gpx(route_source.pk, adapter=lambda _: GPX)
    assert first["status"] == second["status"] == ExtractionStatus.SUCCEEDED
    assert first["created"] is True
    assert second["created"] is False
    assert RouteVersion.objects.count() == 1
    assert RouteVersion.objects.get().elevation_profile == [
        {"distance_m": 0.0, "elevation_m": 100.0},
        {"distance_m": 132.99, "elevation_m": 120.0},
        {"distance_m": 265.98, "elevation_m": 110.0},
    ]
    assert Route.objects.get(pk=route_source.route_id).is_public
    assert ExtractionAttempt.objects.filter(status=ExtractionStatus.SUCCEEDED).count() == 2


def test_changed_payload_creates_immutable_new_version() -> None:
    route_source = source()
    changed = GPX.replace(b"49.001000", b"49.002000")
    first = extract_gpx(route_source.pk, adapter=lambda _: GPX)
    second = extract_gpx(route_source.pk, adapter=lambda _: ExportedGpx(changed))
    assert first["created"] is True and second["created"] is True
    assert [version.version_number for version in RouteVersion.objects.all()] == [1, 2]


def test_new_gpx_storage_keys_include_immutable_attempt_identity() -> None:
    route_source = source()
    changed = GPX.replace(b"49.001000", b"49.002000")
    with patch(
        "apps.ingestion.gpx.default_storage.save", side_effect=lambda name, _content: name
    ) as save:
        first = extract_gpx(route_source.pk, adapter=lambda _: GPX)
        second = extract_gpx(route_source.pk, adapter=lambda _: ExportedGpx(changed))

    assert first["created"] is True and second["created"] is True
    names = [call.args[0] for call in save.call_args_list]
    attempts = list(ExtractionAttempt.objects.order_by("pk").values_list("pk", flat=True))
    assert names[0].endswith(f"-{attempts[0]}.gpx")
    assert names[1].endswith(f"-{attempts[1]}.gpx")
    assert names[0] != names[1]


def test_failed_extraction_is_isolated_and_diagnostic_is_retained() -> None:
    route_source = source()
    result = extract_gpx(route_source.pk, adapter=lambda _: b"not GPX")
    route_source.refresh_from_db()
    attempt = ExtractionAttempt.objects.get(pk=result["attempt_id"])
    assert result["status"] == ExtractionStatus.FAILED
    assert attempt.status == ExtractionStatus.FAILED
    assert attempt.error
    assert attempt.finished_at is not None
    assert route_source.processing_status == ProcessingStatus.FAILED
    assert route_source.last_error == attempt.error


def test_quarantined_route_keeps_valid_version_without_publication() -> None:
    route_source = source()
    route_source.route.lifecycle = "quarantined"
    route_source.route.quarantine_reason = "Needs review"
    route_source.route.save(update_fields=["lifecycle", "quarantine_reason"])
    result = extract_gpx(route_source.pk, adapter=lambda _: GPX)
    assert result["status"] == ExtractionStatus.SUCCEEDED
    assert result["published"] is False
    assert RouteVersion.objects.count() == 1


def test_quarantined_import_does_not_store_original_payload() -> None:
    route_source = source()
    route_source.route.lifecycle = "quarantined"
    route_source.route.quarantine_reason = "Needs review"
    route_source.route.save(update_fields=["lifecycle", "quarantine_reason"])
    with patch("apps.ingestion.gpx.default_storage.save") as save:
        result = extract_gpx(route_source.pk, adapter=lambda _: GPX)
    assert result["status"] == ExtractionStatus.SUCCEEDED
    save.assert_not_called()
    assert RouteVersion.objects.get().original_gpx_storage_key == ""


def test_version_service_rejects_payloads_for_quarantined_routes() -> None:
    route_source = source()
    route_source.route.lifecycle = "quarantined"
    route_source.route.quarantine_reason = "Needs review"
    route_source.route.save(update_fields=["lifecycle", "quarantine_reason"])
    from apps.catalogue.services import record_route_version

    with pytest.raises(ValidationError, match="cannot retain original payloads"):
        record_route_version(source=route_source, checksum="quarantined", storage_key="gpx/x.gpx")


def test_adapter_content_type_is_checked() -> None:
    route_source = source()
    result = extract_gpx(
        route_source.pk, adapter=lambda _: ExportedGpx(GPX, content_type="application/json")
    )
    assert result["status"] == ExtractionStatus.FAILED


def test_denylisted_source_fails_before_adapter_is_called() -> None:
    route_source = source()
    from apps.catalogue.models import SourceDenylistEntry

    SourceDenylistEntry.objects.create(source_url=route_source.mapy_url, reason="takedown")
    adapter = patch("apps.ingestion.gpx._export")
    with adapter as export:
        result = extract_gpx(route_source.pk)
    assert result["status"] == ExtractionStatus.FAILED
    export.assert_not_called()


def test_multiple_track_segments_are_rejected_instead_of_flattened() -> None:
    payload = GPX.replace(
        b"</trkseg>",
        b'</trkseg><trkseg><trkpt lat="50" lon="17"/><trkpt lat="50.001" lon="17.001"/></trkseg>',
        1,
    )
    with pytest.raises(GpxValidationError, match="Multiple GPX track segments"):
        parse_gpx(payload)


def test_multiple_tracks_and_extension_points_are_rejected() -> None:
    payload = GPX.replace(
        b"</trkseg></trk>",
        b'</trkseg></trk><trk><trkseg><trkpt lat="50" lon="17"/>'
        b'<trkpt lat="50.001" lon="17.001"/></trkseg></trk>',
    )
    with pytest.raises(GpxValidationError, match="Multiple GPX route containers"):
        parse_gpx(payload)
    extension = (
        b'<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1" '
        b'xmlns:x="urn:attacker"><rte><x:rtept lat="49" lon="16"/>'
        b'<x:rtept lat="49.1" lon="16.1"/></rte></gpx>'
    )
    with pytest.raises(GpxValidationError):
        parse_gpx(extension)


def test_single_route_ignores_ancillary_waypoint_and_extension_elevation() -> None:
    payload = (
        GPX.replace(
            b"</trk>\n</gpx>",
            b'</trk><wpt lat="1" lon="2"/>\n</gpx>',
        )
        .replace(
            b'<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">',
            b'<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1" '
            b'xmlns:x="urn:extension">',
        )
        .replace(b"<ele>100</ele>", b"<x:ele>100</x:ele>")
    )
    parsed = parse_gpx(payload)
    assert len(parsed.geometry["coordinates"]) == 3
    assert parsed.ascent_m == 0


def test_gpx_namespace_and_version_pair_must_match() -> None:
    payload = GPX.replace(b'version="1.1"', b'version="1.0"')
    with pytest.raises(GpxValidationError, match="namespace and version"):
        parse_gpx(payload)


@override_settings(GPX_DNS_CHECK=False)
def test_guarded_transport_enforces_stream_cap() -> None:
    class StreamResponse:
        status_code = 200
        headers = {"content-type": "application/gpx+xml"}
        request = None
        url = "https://mapy.com/route"

        def __enter__(self) -> StreamResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def iter_bytes(self) -> list[bytes]:
            return [b"123", b"456"]

    guarded = _GuardedHttpClient(timeout=1, max_bytes=5)
    with patch.object(guarded._client, "stream", return_value=StreamResponse()):
        with pytest.raises(GpxPayloadTooLarge):
            guarded.get("https://mapy.com/route")
    guarded.close()


@override_settings(GPX_DNS_CHECK=False)
def test_guarded_transport_rejects_non_https_or_nonstandard_port_redirect_target() -> None:
    guarded = _GuardedHttpClient(timeout=1, max_bytes=100)
    with pytest.raises(UnsafeGpxUrlError):
        guarded.get("https://mapy.com:444/redirect")
    with pytest.raises(UnsafeGpxUrlError):
        guarded.get("https://evil.mapy.com/redirect")
    guarded.close()


def test_pinned_transport_never_uses_a_second_private_dns_answer() -> None:
    transport = PinnedMapyTransport(max_bytes=100)
    dns = patch(
        "apps.ingestion.gpx.socket.getaddrinfo",
        side_effect=[
            [(0, 0, 0, "", ("93.184.216.34", 443))],
            [(0, 0, 0, "", ("127.0.0.1", 443))],
        ],
    )
    with dns, pytest.raises(UnsafeGpxUrlError):
        transport.handle_request(httpx.Request("GET", "https://mapy.com/s/test"))
    assert transport._transports == {}
    transport.close()


def test_pinned_transport_supports_validated_ipv6_answers() -> None:
    transport = PinnedMapyTransport(max_bytes=100)
    response = httpx.Response(200, content=b"ok")
    with (
        patch("apps.ingestion.gpx.validate_gpx_url"),
        patch("apps.ingestion.gpx._public_addresses", return_value=("2606:4700:4700::1111",)),
        patch.object(httpx.HTTPTransport, "handle_request", return_value=response) as handle,
    ):
        transport.handle_request(httpx.Request("GET", "https://mapy.com/s/test"))
    pinned_request = handle.call_args.args[0]
    assert pinned_request.url.host == "2606:4700:4700::1111"
    assert pinned_request.headers["host"] == "mapy.com"
    assert pinned_request.extensions["sni_hostname"] == "mapy.com"
    transport.close()


def test_pinned_transport_uses_distinct_pools_for_logical_hosts_sharing_ip() -> None:
    transport = PinnedMapyTransport(max_bytes=100)
    response = httpx.Response(200, content=b"ok")
    with (
        patch("apps.ingestion.gpx.validate_gpx_url"),
        patch("apps.ingestion.gpx._public_addresses", return_value=("93.184.216.34",)),
        patch.object(httpx.HTTPTransport, "handle_request", return_value=response),
    ):
        transport.handle_request(httpx.Request("GET", "https://mapy.com/s/test"))
        transport.handle_request(httpx.Request("GET", "https://mapy.cz/s/test"))
    assert len(transport._transports) == 2
    assert (
        transport._transports[("mapy.com", "93.184.216.34")]
        is not transport._transports[("mapy.cz", "93.184.216.34")]
    )
    transport.close()


@override_settings(GPX_DNS_CHECK=False)
def test_adapter_retries_transport_failures_with_a_bounded_backoff() -> None:
    delays: list[float] = []
    with patch(
        "mapy_gpx_exporter.resolver.resolve_short_link",
        side_effect=TimeoutError("timed out"),
    ):
        adapter = MapyGpxExporterAdapter(retries=3, backoff=0.1, sleep=delays.append)
        with pytest.raises(GpxExtractionError, match="after 3 attempts"):
            adapter.fetch_gpx("https://mapy.com/s/test")
    assert delays == [0.1, 0.2]


def test_storage_is_cleaned_when_version_transaction_fails() -> None:
    route_source = source()
    with patch("apps.ingestion.gpx.default_storage.save", return_value="gpx/saved.gpx") as save:
        with patch("apps.ingestion.gpx.default_storage.delete") as delete:
            with patch(
                "apps.ingestion.gpx.approve_version",
                side_effect=RuntimeError("database failure"),
            ):
                result = extract_gpx(route_source.pk, adapter=lambda _: GPX)
    assert result["status"] == ExtractionStatus.FAILED
    assert save.call_args.args[1].read() == GPX
    delete.assert_called_once_with("gpx/saved.gpx")
    assert RouteVersion.objects.count() == 0


def test_failed_storage_cleanup_is_durable_and_retryable() -> None:
    route_source = source()
    with patch("apps.ingestion.gpx.default_storage.save", return_value="gpx/orphan.gpx"):
        with patch(
            "apps.ingestion.gpx.default_storage.delete",
            side_effect=OSError("storage offline"),
        ):
            with patch("apps.ingestion.gpx.approve_version", side_effect=RuntimeError("db")):
                result = extract_gpx(route_source.pk, adapter=lambda _: GPX)
    orphan = OrphanPayloadCleanup.objects.get()
    assert result["status"] == ExtractionStatus.FAILED
    assert result["diagnostics"]["orphan_storage_key"] == "gpx/orphan.gpx"
    assert orphan.status == OrphanPayloadStatus.PENDING
    with patch("apps.ingestion.gpx.default_storage.delete") as delete:
        from apps.ingestion.gpx import cleanup_orphan_payload

        cleanup_result = cleanup_orphan_payload(orphan.pk)
    delete.assert_called_once_with("gpx/orphan.gpx")
    orphan.refresh_from_db()
    assert cleanup_result["status"] == OrphanPayloadStatus.COMPLETED
    assert orphan.completed_at is not None


@override_settings(GPX_ORPHAN_CLEANUP_RETRY_BASE_SECONDS=60)
def test_orphan_reconciliation_retries_failures_and_handles_missing_payloads() -> None:
    route_source = source()
    attempt = ExtractionAttempt.objects.create(
        source=route_source, source_url=route_source.mapy_url
    )
    retryable = OrphanPayloadCleanup.objects.create(
        source=route_source,
        attempt=attempt,
        storage_key="gpx/retry.gpx",
        status=OrphanPayloadStatus.PENDING,
    )
    already_deleted = OrphanPayloadCleanup.objects.create(
        source=route_source,
        attempt=attempt,
        storage_key="gpx/already-deleted.gpx",
        status=OrphanPayloadStatus.PENDING,
    )

    calls: list[str] = []

    def delete(key: str) -> None:
        calls.append(key)
        if key == retryable.storage_key and calls.count(key) == 1:
            raise OSError("storage offline")
        if key == already_deleted.storage_key:
            raise FileNotFoundError(key)

    from apps.ingestion.gpx import reconcile_orphan_payloads

    with patch("apps.ingestion.gpx.default_storage.delete", side_effect=delete):
        first = reconcile_orphan_payloads(limit=2)
        retryable.refresh_from_db()
        assert retryable.status == OrphanPayloadStatus.FAILED
        assert retryable.next_retry_at is not None
        retryable.next_retry_at = timezone.now()
        retryable.save(update_fields=["next_retry_at"])
        retry = reconcile_orphan_payloads(limit=1)

    retryable.refresh_from_db()
    already_deleted.refresh_from_db()
    assert first == {
        "processed": 2,
        "completed": 1,
        "failed": 1,
        "exhausted": 0,
        "retried": 0,
        "skipped": 0,
    }
    assert retry == {
        "processed": 1,
        "completed": 1,
        "failed": 0,
        "exhausted": 0,
        "retried": 1,
        "skipped": 0,
    }
    assert retryable.status == OrphanPayloadStatus.COMPLETED
    assert retryable.attempts == 2
    assert already_deleted.status == OrphanPayloadStatus.COMPLETED
    assert already_deleted.attempts == 1


@override_settings(
    GPX_ORPHAN_CLEANUP_MAX_ATTEMPTS=2,
    GPX_ORPHAN_CLEANUP_RETRY_BASE_SECONDS=0,
)
def test_orphan_reconciliation_exhausts_permanent_storage_failures() -> None:
    route_source = source()
    attempt = ExtractionAttempt.objects.create(
        source=route_source, source_url=route_source.mapy_url
    )
    orphan = OrphanPayloadCleanup.objects.create(
        source=route_source,
        attempt=attempt,
        storage_key="gpx/permanent.gpx",
    )
    from apps.ingestion.gpx import reconcile_orphan_payloads

    with patch(
        "apps.ingestion.gpx.default_storage.delete", side_effect=OSError("storage offline")
    ) as delete:
        first = reconcile_orphan_payloads(limit=1)
        second = reconcile_orphan_payloads(limit=1)
        third = reconcile_orphan_payloads(limit=1)

    orphan.refresh_from_db()
    assert first["failed"] == 1 and first["exhausted"] == 0
    assert second["failed"] == 0 and second["exhausted"] == 1
    assert third["processed"] == 0
    assert orphan.status == OrphanPayloadStatus.EXHAUSTED
    assert orphan.attempts == 2
    assert orphan.next_retry_at is None
    assert delete.call_count == 2


def test_orphan_reconciliation_prioritizes_pending_over_old_failed_rows() -> None:
    route_source = source()
    attempt = ExtractionAttempt.objects.create(
        source=route_source, source_url=route_source.mapy_url
    )
    old_failed = OrphanPayloadCleanup.objects.create(
        source=route_source,
        attempt=attempt,
        storage_key="gpx/old-failed.gpx",
        status=OrphanPayloadStatus.FAILED,
        last_error="still unavailable",
    )
    pending = OrphanPayloadCleanup.objects.create(
        source=route_source,
        attempt=attempt,
        storage_key="gpx/new-pending.gpx",
    )
    from apps.ingestion.models import OrphanPayloadReconciliationState

    cursor, _ = OrphanPayloadReconciliationState.objects.get_or_create(pk=1)
    cursor.last_selected_bucket = OrphanPayloadReconciliationState.Bucket.FAILED
    cursor.save(update_fields=["last_selected_bucket", "updated_at"])
    from apps.ingestion.gpx import reconcile_orphan_payloads

    with patch("apps.ingestion.gpx.default_storage.delete") as delete:
        result = reconcile_orphan_payloads(limit=1)

    old_failed.refresh_from_db()
    pending.refresh_from_db()
    assert result["completed"] == 1
    assert pending.status == OrphanPayloadStatus.COMPLETED
    assert old_failed.status == OrphanPayloadStatus.FAILED
    delete.assert_called_once_with("gpx/new-pending.gpx")


@override_settings(GPX_ORPHAN_CLEANUP_RETRY_BASE_SECONDS=0)
def test_orphan_reconciliation_interleaves_pending_and_failed_limit_one() -> None:
    route_source = source()
    attempt = ExtractionAttempt.objects.create(
        source=route_source, source_url=route_source.mapy_url
    )
    old_failed = OrphanPayloadCleanup.objects.create(
        source=route_source,
        attempt=attempt,
        storage_key="gpx/interleave-failed.gpx",
        status=OrphanPayloadStatus.FAILED,
        last_error="temporary",
    )
    pending = OrphanPayloadCleanup.objects.create(
        source=route_source,
        attempt=attempt,
        storage_key="gpx/interleave-pending.gpx",
    )
    from apps.ingestion.models import OrphanPayloadReconciliationState

    cursor, _ = OrphanPayloadReconciliationState.objects.get_or_create(pk=1)
    cursor.last_selected_bucket = OrphanPayloadReconciliationState.Bucket.FAILED
    cursor.save(update_fields=["last_selected_bucket", "updated_at"])
    from apps.ingestion.gpx import reconcile_orphan_payloads

    def delete(key: str) -> None:
        if key == old_failed.storage_key:
            raise OSError("temporary")

    with patch("apps.ingestion.gpx.default_storage.delete", side_effect=delete) as storage_delete:
        first = reconcile_orphan_payloads(limit=1)
        new_pending = OrphanPayloadCleanup.objects.create(
            source=route_source,
            attempt=attempt,
            storage_key="gpx/interleave-new-pending.gpx",
        )
        second = reconcile_orphan_payloads(limit=1)
        third = reconcile_orphan_payloads(limit=1)

    pending.refresh_from_db()
    old_failed.refresh_from_db()
    new_pending.refresh_from_db()
    assert first["completed"] == 1
    assert second["failed"] == 1
    assert third["completed"] == 1
    assert pending.status == OrphanPayloadStatus.COMPLETED
    assert old_failed.status == OrphanPayloadStatus.FAILED
    assert new_pending.status == OrphanPayloadStatus.COMPLETED
    assert [call.args[0] for call in storage_delete.call_args_list] == [
        "gpx/interleave-pending.gpx",
        "gpx/interleave-failed.gpx",
        "gpx/interleave-new-pending.gpx",
    ]


def test_orphan_cleanup_protects_a_live_payload_reference() -> None:
    route_source = source()
    attempt = ExtractionAttempt.objects.create(
        source=route_source, source_url=route_source.mapy_url
    )
    from apps.catalogue.services import record_route_version

    version, _ = record_route_version(
        source=route_source, checksum="live-reference", storage_key="gpx/reused.gpx"
    )
    orphan = OrphanPayloadCleanup.objects.create(
        source=route_source,
        attempt=attempt,
        storage_key="gpx/reused.gpx",
    )
    from apps.ingestion.gpx import cleanup_orphan_payload

    with patch("apps.ingestion.gpx.default_storage.delete") as delete:
        result = cleanup_orphan_payload(orphan.pk)

    version.refresh_from_db()
    orphan.refresh_from_db()
    assert result["status"] == OrphanPayloadStatus.COMPLETED
    assert result["protected"] is True
    assert version.original_gpx_storage_key == "gpx/reused.gpx"
    delete.assert_not_called()


def test_expired_orphan_claim_cannot_delete_a_later_live_payload() -> None:
    route_source = source()
    attempt = ExtractionAttempt.objects.create(
        source=route_source, source_url=route_source.mapy_url
    )
    from apps.catalogue.services import record_route_version
    from apps.ingestion.gpx import _claim_orphan_payload, cleanup_orphan_payload

    storage_key = f"gpx/routes/{route_source.route_id}/legacy-1.gpx"
    orphan = OrphanPayloadCleanup.objects.create(
        source=route_source,
        attempt=attempt,
        storage_key=storage_key,
    )
    token, _, claim_result = _claim_orphan_payload(orphan.pk, respect_backoff=False)
    assert token is not None and claim_result is None
    orphan.refresh_from_db()
    orphan.claimed_until = timezone.now() - timedelta(seconds=1)
    orphan.save(update_fields=["claimed_until"])
    record_route_version(source=route_source, checksum="legacy-reused", storage_key=storage_key)

    with patch("apps.ingestion.gpx.default_storage.delete") as delete:
        result = cleanup_orphan_payload(orphan.pk)

    assert result["status"] == OrphanPayloadStatus.COMPLETED
    assert result["protected"] is True
    delete.assert_not_called()


def test_orphan_cleanup_claim_prevents_concurrent_storage_deletes() -> None:
    route_source = source()
    attempt = ExtractionAttempt.objects.create(
        source=route_source, source_url=route_source.mapy_url
    )
    orphan = OrphanPayloadCleanup.objects.create(
        source=route_source,
        attempt=attempt,
        storage_key="gpx/claimed.gpx",
    )
    from apps.ingestion.gpx import _claim_orphan_payload, cleanup_orphan_payload

    calls = 0
    concurrent: dict[str, object] = {}

    def delete(_key: str) -> None:
        nonlocal calls
        calls += 1
        claim_result = _claim_orphan_payload(orphan.pk, respect_backoff=False)[2]
        assert claim_result is not None
        concurrent.update(claim_result)

    with patch("apps.ingestion.gpx.default_storage.delete", side_effect=delete):
        result = cleanup_orphan_payload(orphan.pk)

    assert result["status"] == OrphanPayloadStatus.COMPLETED
    assert concurrent["claimed"] is True
    assert calls == 1


def test_repeated_completed_orphan_cleanup_is_idempotent() -> None:
    route_source = source()
    attempt = ExtractionAttempt.objects.create(
        source=route_source, source_url=route_source.mapy_url
    )
    orphan = OrphanPayloadCleanup.objects.create(
        source=route_source,
        attempt=attempt,
        storage_key="gpx/repeated.gpx",
    )
    from apps.ingestion.gpx import cleanup_orphan_payload

    with patch("apps.ingestion.gpx.default_storage.delete") as delete:
        first = cleanup_orphan_payload(orphan.pk)
        second = cleanup_orphan_payload(orphan.pk)

    assert first["status"] == second["status"] == OrphanPayloadStatus.COMPLETED
    delete.assert_called_once_with("gpx/repeated.gpx")


@override_settings(GPX_DNS_CHECK=False)
def test_local_export_uses_generated_content_metadata() -> None:
    from mapy_gpx_exporter import RouteParams  # type: ignore[import-untyped]

    with (
        patch(
            "mapy_gpx_exporter.resolver.resolve_short_link",
            return_value=RouteParams(resolution_method="local_decode"),
        ) as resolve,
        patch("mapy_gpx_exporter.exporter.export_gpx", return_value=GPX) as export,
    ):
        result = MapyGpxExporterAdapter().fetch_gpx("https://mapy.com/s/test")
    assert result.content == GPX
    assert result.content_type == "application/gpx+xml"
    assert result.final_url == ""
    resolve.assert_called_once()
    export.assert_called_once()


@override_settings(
    GPX_EXTRACTION_ENABLED=True,
    GPX_PROVIDER_AUTHORIZED=True,
    GPX_LEGAL_APPROVED=True,
)
def test_celery_task_marks_domain_failure_as_failed() -> None:
    with patch(
        "apps.ingestion.tasks.run_gpx_extraction",
        return_value={"status": ExtractionStatus.FAILED, "error": "invalid GPX"},
    ):
        with pytest.raises(GpxExtractionTaskFailure, match="invalid GPX"):
            extract_gpx_task.apply(args=[1]).get()
