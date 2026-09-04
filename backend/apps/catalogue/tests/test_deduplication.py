from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import override_settings

from apps.catalogue.deduplication import (
    SimilarityConfig,
    compare_geometries,
    compare_versions,
    link_similarity,
    normalize_geometry,
    similar_versions,
)
from apps.catalogue.models import (
    ForumPost,
    ForumThread,
    LoopStatus,
    PayloadDeletionRequest,
    ProcessingStatus,
    Route,
    RouteSource,
    RouteSourceMerge,
    RouteVersion,
    SimilarityRelationship,
)
from apps.catalogue.services import (
    approve_version,
    keep_both_routes,
    merge_route_sources,
    quarantine_suspected_duplicate,
    record_route_version,
    register_source,
    restore_route,
)
from apps.ingestion.gpx import extract_gpx
from apps.ingestion.tasks import process_payload_deletion_task

pytestmark = pytest.mark.django_db


LINE: dict[str, object] = {
    "type": "LineString",
    "coordinates": [[16.0, 49.0], [16.002, 49.001], [16.004, 49.002]],
}
LOOP: dict[str, object] = {
    "type": "LineString",
    "coordinates": [
        [16.0, 49.0],
        [16.002, 49.0],
        [16.002, 49.002],
        [16.0, 49.002],
        [16.0, 49.0],
    ],
}
GPX = b"""<?xml version="1.0"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>
<trkpt lat="49.000000" lon="16.000000"/><trkpt lat="49.001000" lon="16.002000"/>
<trkpt lat="49.002000" lon="16.004000"/>
</trkseg></trk></gpx>"""


def test_reversed_lines_are_equivalent() -> None:
    reversed_line = {
        "type": "LineString",
        "coordinates": [[16.004, 49.002], [16.002, 49.001], [16.0, 49.0]],
    }
    result = compare_geometries(LINE, reversed_line)
    assert result.duplicate
    assert result.score == pytest.approx(1.0)
    assert result.reversed_direction


def test_shifted_loop_start_is_equivalent() -> None:
    points: list[list[float]] = [
        [16.0, 49.0],
        [16.002, 49.0],
        [16.002, 49.002],
        [16.0, 49.002],
    ]
    shifted = points[2:] + points[:2] + [points[2]]
    result = compare_geometries(
        LOOP,
        {"type": "LineString", "coordinates": shifted},
        loop_a=True,
        loop_b=True,
    )
    assert result.duplicate
    assert result.start_offset_fraction != 0


def test_meaningful_length_change_is_a_variant() -> None:
    longer = {
        "type": "LineString",
        "coordinates": [[16.0, 49.0], [16.002, 49.002], [16.004, 49.002]],
    }
    result = compare_geometries(LINE, longer)
    assert not result.duplicate
    assert result.variant
    assert result.length_delta_ratio > 0.10


def test_policy_thresholds_are_configurable() -> None:
    strict = SimilarityConfig(
        gps_tolerance_m=1,
        duplicate_max_mean_distance_m=1,
        duplicate_max_max_distance_m=1,
    )
    noisy = {
        "type": "LineString",
        "coordinates": [[16.0, 49.0], [16.0023, 49.0013], [16.004, 49.002]],
    }
    result = compare_geometries(LINE, noisy, config=strict)
    assert not result.duplicate


def test_benchmark_fixture_set_matches_policy() -> None:
    fixture_path = Path(__file__).with_name("fixtures") / "deduplication_benchmark.json"
    cases = json.loads(fixture_path.read_text())
    measured: dict[str, tuple[float, float, str]] = {}
    for case in cases:
        left = normalize_geometry({"type": "LineString", "coordinates": case["a"]})
        right = normalize_geometry({"type": "LineString", "coordinates": case["b"]})
        result = compare_geometries(left, right, loop_a=case["loop"], loop_b=case["loop"])
        outcome = "duplicate" if result.duplicate else "variant" if result.variant else "unrelated"
        measured[case["name"]] = (result.score, result.length_delta_ratio, outcome)
        assert outcome == case["expected"]
        assert case["score_range"][0] <= result.score <= case["score_range"][1]
        assert (
            case["mean_distance_range"][0]
            <= result.mean_distance_m
            <= case["mean_distance_range"][1]
        )
        assert (
            case["length_delta_range"][0]
            <= result.length_delta_ratio
            <= case["length_delta_range"][1]
        )
    assert measured["reversed point-to-point recording"][0] == pytest.approx(1.0)
    assert measured["shifted loop start duplicate"][2] == "duplicate"


def _version(
    route: Route,
    number: int,
    geometry: dict[str, object],
    *,
    loop: bool = False,
    status: str = ProcessingStatus.VALID,
) -> RouteVersion:
    source = RouteSource.objects.create(
        route=route, mapy_url=f"https://mapy.com/s/{route.pk}-{number}"
    )
    return record_route_version(
        source=source,
        checksum=f"checksum-{route.pk}-{number}",
        normalized_geometry=geometry,
        simplified_geometry=geometry,
        distance_m=Decimal("1"),
        loop_status=LoopStatus.LOOP if loop else LoopStatus.POINT_TO_POINT,
        technical_status=status,
    )[0]


def test_similarity_is_persisted_idempotently() -> None:
    first, second = Route.objects.create(), Route.objects.create()
    left, right = _version(first, 1, LINE), _version(second, 1, LINE)
    result = compare_versions(left, right)
    relationship = link_similarity(left, right, result)
    repeated = link_similarity(left, right, result)
    assert relationship.pk == repeated.pk
    assert (
        relationship.relationship_type
        == SimilarityRelationship.RelationshipType.SUSPECTED_DUPLICATE
    )
    assert relationship.evidence["algorithm"]
    assert {
        "duplicate_max_length_delta",
        "duplicate_max_mean_distance_m",
        "duplicate_max_max_distance_m",
        "variant_min_score",
        "duplicate_length_gate_passed",
        "duplicate_mean_gate_passed",
        "duplicate_max_gate_passed",
    } <= relationship.evidence.keys()


def test_similar_versions_only_returns_other_valid_routes() -> None:
    first, second, invalid_route = (
        Route.objects.create(),
        Route.objects.create(),
        Route.objects.create(),
    )
    left, right = _version(first, 1, LINE), _version(second, 1, LINE)
    _version(
        invalid_route,
        1,
        {"type": "LineString", "coordinates": [[10, 10], [11, 11]]},
        status=ProcessingStatus.INVALID,
    )
    assert [candidate.pk for candidate, _ in similar_versions(left)] == [right.pk]


def test_keep_both_and_merge_sources_are_audited() -> None:
    thread = ForumThread.objects.create(url="https://forum.example/thread", title="Ride")
    post = ForumPost.objects.create(thread=thread, url="https://forum.example/thread#1")
    first, second = Route.objects.create(), Route.objects.create()
    primary_source, _ = register_source(
        route=first, post=post, mapy_url="https://mapy.com/s/primary"
    )
    source, _ = register_source(route=second, post=post, mapy_url="https://mapy.com/s/secondary")
    primary_version, _ = record_route_version(source=primary_source, checksum="primary")
    secondary_version, _ = record_route_version(source=source, checksum="secondary")
    approve_version(primary_version)
    approve_version(secondary_version)
    relationship = keep_both_routes(first, second, reason="The routes are intentionally distinct.")
    assert relationship.relationship_type == SimilarityRelationship.RelationshipType.VARIANT
    links = merge_route_sources(first, second, reason="Keep the second forum attribution.")
    assert [link.source_id for link in links] == [source.pk]
    relationship.refresh_from_db()
    assert (
        relationship.relationship_type
        == SimilarityRelationship.RelationshipType.SUSPECTED_DUPLICATE
    )
    assert list(first.provenance_sources) == [primary_source, source]
    second.refresh_from_db()
    assert second.lifecycle == "quarantined"
    keep_both_routes(first, second, reason="Restore both intentional variants.")
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.is_public and second.is_public
    alias = RouteSourceMerge.objects.get(source=source)
    assert not alias.active and alias.deactivated_at is not None
    assert list(first.provenance_sources) == [primary_source]
    assert first.moderation_decisions.filter(action="merge_sources").exists()


def test_one_source_cannot_have_two_active_canonical_merges() -> None:
    thread = ForumThread.objects.create(url="https://forum.example/merge", title="Merge")
    post = ForumPost.objects.create(thread=thread, url="https://forum.example/merge#1")
    canonical, duplicate, other = (
        Route.objects.create(),
        Route.objects.create(),
        Route.objects.create(),
    )
    source, _ = register_source(
        route=duplicate, post=post, mapy_url="https://mapy.com/s/merge-source"
    )
    merge_route_sources(canonical, duplicate, reason="First canonical decision.")
    with pytest.raises(ValidationError, match="already merged"):
        merge_route_sources(other, duplicate, reason="Conflicting canonical decision.")
    assert RouteSourceMerge.objects.filter(source=source, active=True).count() == 1


def test_keep_both_only_deactivates_aliases_for_selected_route_pair() -> None:
    thread = ForumThread.objects.create(url="https://forum.example/pairs", title="Pairs")
    post = ForumPost.objects.create(thread=thread, url="https://forum.example/pairs#1")
    canonical, duplicate_b, duplicate_c = (
        Route.objects.create(),
        Route.objects.create(),
        Route.objects.create(),
    )
    source_b, _ = register_source(
        route=duplicate_b, post=post, mapy_url="https://mapy.com/s/pair-b"
    )
    source_c, _ = register_source(
        route=duplicate_c, post=post, mapy_url="https://mapy.com/s/pair-c"
    )
    version_b, _ = record_route_version(source=source_b, checksum="pair-b")
    approve_version(version_b)
    merge_route_sources(canonical, duplicate_b, reason="Connect B to the canonical route.")
    merge_route_sources(canonical, duplicate_c, reason="Connect C to the canonical route.")
    keep_both_routes(canonical, duplicate_b, reason="B is an intentional variant.")
    alias_b = RouteSourceMerge.objects.get(source=source_b)
    alias_c = RouteSourceMerge.objects.get(source=source_c)
    assert not alias_b.active and alias_b.deactivated_at is not None
    assert alias_c.active and alias_c.deactivated_at is None
    assert source_c.pk in canonical.provenance_sources.values_list("pk", flat=True)


def test_quarantine_duplicate_retains_evidence_and_restore_is_explicit() -> None:
    first, second = Route.objects.create(), Route.objects.create()
    left, right = _version(first, 1, LINE), _version(second, 1, LINE)
    relationship = link_similarity(left, right)
    quarantine_suspected_duplicate(second, reason="Confirmed duplicate.", relationship=relationship)
    second.refresh_from_db()
    assert second.lifecycle == "quarantined"
    decision = second.moderation_decisions.filter(action="quarantine").get()
    assert decision.metadata["relationship_id"] == relationship.pk
    restore_route(second)
    second.refresh_from_db()
    assert second.is_public


@override_settings(GPX_DNS_CHECK=False)
def test_new_duplicate_import_is_quarantined_and_payload_deletion_is_queued(
    tmp_path: Path,
) -> None:
    existing, incoming = Route.objects.create(), Route.objects.create()
    _version(existing, 1, LINE)
    source = RouteSource.objects.create(route=incoming, mapy_url="https://mapy.com/s/incoming")
    with override_settings(MEDIA_ROOT=tmp_path):
        result = extract_gpx(source.pk, adapter=lambda _: GPX)
    incoming.refresh_from_db()
    assert result["status"] == "succeeded"
    assert result["published"] is False
    assert result["duplicate"] is True
    assert incoming.lifecycle == "quarantined"
    assert PayloadDeletionRequest.objects.count() == 1
    assert result["similarity_relationship_id"] is not None


def test_payload_deletion_task_removes_file_and_finalizes_audit(
    tmp_path: Path, django_capture_on_commit_callbacks: Any
) -> None:
    route = Route.objects.create()
    source = RouteSource.objects.create(route=route, mapy_url="https://mapy.com/s/payload")
    with override_settings(MEDIA_ROOT=tmp_path):
        storage_key = default_storage.save("gpx/payload.gpx", ContentFile(b"payload"))
        version = RouteVersion.objects.create(
            source=source,
            version_number=1,
            checksum="payload-checksum",
            original_gpx_storage_key=storage_key,
            technical_status=ProcessingStatus.VALID,
        )
        with patch("apps.ingestion.tasks.process_payload_deletion_task.delay") as dispatch:
            with django_capture_on_commit_callbacks(execute=True):
                quarantine_suspected_duplicate(route, reason="Confirmed duplicate.")
            dispatch.assert_called_once_with(PayloadDeletionRequest.objects.get(version=version).pk)
        request = PayloadDeletionRequest.objects.get(version=version)
        completed = process_payload_deletion_task.apply(args=[request.pk]).get()
        version.refresh_from_db()
        request.refresh_from_db()
        assert completed["status"] == "completed"
        assert request.status == "completed"
        assert not default_storage.exists(storage_key)
        assert version.original_gpx_storage_key == ""
        assert version.payload_removal_reason == "Payload removed during quarantine."
        assert version.payload_removed_at is not None
