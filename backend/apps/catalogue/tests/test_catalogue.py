import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, ProgrammingError, connection, transaction
from django.db.models.deletion import ProtectedError
from django.test import override_settings

from apps.catalogue.fields import _GIS_AVAILABLE, RouteGeometryField
from apps.catalogue.models import (
    Category,
    ForumAuthor,
    ForumPost,
    ForumThread,
    LoopStatus,
    ModerationDecision,
    PayloadDeletionRequest,
    ProcessingStatus,
    Route,
    RouteCategory,
    RouteLifecycle,
    RouteSource,
    RouteSourcePost,
    RouteVersion,
    SimilarityRelationship,
    SourceDenylistEntry,
    SourceStatus,
    TitleProvenance,
)
from apps.catalogue.services import (
    SourceDeniedError,
    TitleContext,
    apply_generated_title,
    approve_version,
    generate_route_title,
    mark_source_unavailable,
    process_payload_deletion,
    quarantine_route,
    record_route_version,
    register_source,
    restore_route,
    retry_payload_deletions,
    review_route,
    soft_delete_route,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def forum_context() -> tuple[ForumPost, Route]:
    author = ForumAuthor.objects.create(username="rider")
    thread = ForumThread.objects.create(
        url="https://bikeforum.example/t/42", title="Gravel Routes in South Moravia"
    )
    post = ForumPost.objects.create(
        thread=thread,
        author=author,
        url="https://bikeforum.example/t/42#p7",
        posted_at="2025-04-12T10:00:00Z",
    )
    return post, Route.objects.create()


def test_title_generation_follows_specified_fallback_order() -> None:
    assert (
        generate_route_title(
            thread_title="Gravel Routes", start_locality="Brno", end_locality="Mikulov"
        )
        == "Gravel Routes — Brno → Mikulov"
    )
    assert (
        generate_route_title(thread_title="MTB Tips", start_locality="Tišnov", loop=True)
        == "MTB Tips — Loop from Tišnov"
    )
    assert (
        generate_route_title(
            thread_title="Gravel Routes", author="rider", post_date=date(2025, 4, 12)
        )
        == "Gravel Routes — rider · 2025-04-12"
    )
    first_title = generate_route_title(
        thread_title="Gravel Routes",
        author="rider",
        post_date=date(2025, 4, 12),
        route_id="11111111-1111-1111-1111-111111111111",
        disambiguate=True,
    )
    second_title = generate_route_title(
        thread_title="Gravel Routes",
        author="rider",
        post_date=date(2025, 4, 12),
        route_id="22222222-2222-2222-2222-222222222222",
        disambiguate=True,
    )
    assert first_title != second_title
    assert first_title.endswith("Route 11111111")
    geographic_context = TitleContext(
        thread_title="Loops",
        start_locality="Brno",
        loop=True,
        route_id="11111111-1111-1111-1111-111111111111",
        disambiguate=True,
    )
    assert generate_route_title(geographic_context).endswith("Route 11111111")
    assert (
        generate_route_title(
            thread_title="Gravel Routes", route_id=UUID("12345678-1234-5678-1234-567812345678")
        )
        == "Gravel Routes — Route 12345678"
    )
    with pytest.raises(ValidationError):
        generate_route_title(thread_title="No context")


def test_title_fields_keep_source_generated_and_admin_values_separate(
    forum_context: tuple[ForumPost, Route],
) -> None:
    _, route = forum_context
    apply_generated_title(
        route,
        TitleContext(thread_title="Original thread", start_locality="Brno", end_locality="Mikulov"),
        source_title="Mapy source title",
    )
    route.refresh_from_db()
    assert route.original_source_title == "Mapy source title"
    assert route.thread_title == "Original thread"
    assert route.generated_title == "Original thread — Brno → Mikulov"
    assert route.display_title == route.generated_title
    assert route.title_provenance == TitleProvenance.GEOGRAPHIC
    assert route.slug.startswith("route-")
    route.admin_title_override = "Curated title"
    route.display_title = "Curated title"
    route.title_provenance = TitleProvenance.ADMIN_OVERRIDE
    route.save()
    apply_generated_title(route, TitleContext(thread_title="Changed thread", route_id=route.id))
    route.refresh_from_db()
    assert route.display_title == "Curated title"
    assert route.admin_title_override == "Curated title"
    assert route.original_source_title == "Mapy source title"
    assert route.thread_title == "Changed thread"
    assert route.generated_title.endswith("Route " + str(route.id).replace("-", "")[:8])


def test_source_and_versions_are_idempotent_and_version_changes_are_immutable(
    forum_context: tuple[ForumPost, Route],
) -> None:
    post, route = forum_context
    source, created = register_source(route=route, post=post, mapy_url="https://mapy.com/s/a")
    repeated, repeated_created = register_source(
        route=route, post=post, mapy_url="https://mapy.com/s/a", source_title="refreshed"
    )
    assert (source.pk, created) == (repeated.pk, True)
    assert not repeated_created
    source.refresh_from_db()
    assert source.source_title == "refreshed"
    second_post = ForumPost.objects.create(
        thread=post.thread, url="https://bikeforum.example/t/42#p8"
    )
    same_source, source_created = register_source(
        route=route, post=second_post, mapy_url="https://mapy.com/s/a"
    )
    assert same_source.pk == source.pk
    assert source_created is False
    assert RouteSourcePost.objects.filter(source=source).count() == 2
    version, created = record_route_version(
        source=source,
        checksum="abc",
        storage_key="gpx/abc.gpx",
        normalized_geometry={"type": "LineString", "coordinates": [[16, 49], [17, 50]]},
        distance_m=Decimal("12.50"),
        loop_status=LoopStatus.POINT_TO_POINT,
    )
    same, same_created = record_route_version(
        source=source, checksum="abc", storage_key="different"
    )
    changed, changed_created = record_route_version(source=source, checksum="def")
    assert created and not same_created and changed_created
    assert same.pk == version.pk
    assert changed.version_number == 2
    assert version.original_gpx_storage_key == "gpx/abc.gpx"
    assert RouteVersion.objects.filter(source=source).count() == 2
    source.refresh_from_db()
    assert source.processing_status == ProcessingStatus.VALID
    assert source.last_checked_at is not None
    assert source.last_successful_check_at is not None


def test_new_route_versions_accept_distinct_elevation_profiles(
    forum_context: tuple[ForumPost, Route],
) -> None:
    post, route = forum_context
    source, _ = register_source(route=route, post=post, mapy_url="https://mapy.com/s/profiles")
    first, _ = record_route_version(
        source=source,
        checksum="profile-one",
        elevation_profile=[{"distance_m": 0.0, "elevation_m": 120.0}],
    )
    second, _ = record_route_version(
        source=source,
        checksum="profile-two",
        elevation_profile=[{"distance_m": 0.0, "elevation_m": 310.0}],
    )

    assert first.version_number == 1
    assert second.version_number == 2
    assert first.elevation_profile != second.elevation_profile


def test_publication_requires_valid_version_and_optional_metadata_does_not_gate_it(
    forum_context: tuple[ForumPost, Route],
) -> None:
    post, route = forum_context
    source, _ = register_source(route=route, post=post, mapy_url="https://mapy.com/s/b")
    invalid, _ = record_route_version(
        source=source,
        checksum="bad",
        technical_status=ProcessingStatus.INVALID,
        validation_error="not GPX",
    )
    with pytest.raises(ValidationError):
        approve_version(invalid)
    valid, _ = record_route_version(source=source, checksum="good")
    approve_version(valid)
    route.refresh_from_db()
    assert route.is_public
    assert route.current_approved_version_id == valid.pk
    assert valid.distance_m is None
    assert ModerationDecision.objects.filter(route=route).exists()
    assert ModerationDecision.objects.filter(
        route=route, action=ModerationDecision.Action.PUBLISH
    ).exists()
    assert route.reviewed_at is None
    review_route(route, reason="Checked source context and technical validity")
    route.refresh_from_db()
    assert ModerationDecision.objects.filter(
        route=route, action=ModerationDecision.Action.REVIEW
    ).exists()
    assert route.reviewed_at is not None


def test_categories_are_optional_multi_valued_metadata(
    forum_context: tuple[ForumPost, Route],
) -> None:
    _, route = forum_context
    road = Category.objects.create(name="Road", slug="road")
    gravel = Category.objects.create(name="Gravel", slug="gravel")
    RouteCategory.objects.create(route=route, category=road)
    RouteCategory.objects.create(route=route, category=gravel)
    assert set(route.category_links.values_list("category__slug", flat=True)) == {"road", "gravel"}
    with pytest.raises(IntegrityError):
        RouteCategory.objects.create(route=route, category=road)


def test_quarantine_and_soft_delete_are_distinct_and_audited(
    forum_context: tuple[ForumPost, Route],
) -> None:
    post, route = forum_context
    source, _ = register_source(route=route, post=post, mapy_url="https://mapy.com/s/c")
    version, _ = record_route_version(source=source, checksum="ok", storage_key="gpx/ok.gpx")
    approve_version(version)
    quarantine_route(route, reason="Possible duplicate")
    route.refresh_from_db()
    assert route.lifecycle == RouteLifecycle.QUARANTINED
    assert route.is_public is False
    with pytest.raises(ValidationError):
        approve_version(version)
    restore_route(route)
    route.refresh_from_db()
    assert route.lifecycle == RouteLifecycle.PUBLISHED
    quarantine_route(route, reason="Possible duplicate again")
    soft_delete_route(route, reason="Rights-holder request")
    route.refresh_from_db()
    denylist = SourceDenylistEntry.objects.get(source_url=source.mapy_url)
    assert route.lifecycle == RouteLifecycle.SOFT_DELETED
    assert route.deleted_at is not None
    assert denylist.active
    request = PayloadDeletionRequest.objects.get(version=version)
    assert request.status == PayloadDeletionRequest.Status.PENDING
    process_payload_deletion(request)
    version.refresh_from_db()
    assert version.original_gpx_storage_key == ""
    assert version.payload_removed_at is not None
    restore_route(route)
    route.refresh_from_db()
    denylist.refresh_from_db()
    assert route.lifecycle == RouteLifecycle.PUBLISHED
    assert not denylist.active


def test_denylist_blocks_reimport_and_unavailable_source_stays_visible(
    forum_context: tuple[ForumPost, Route],
) -> None:
    post, route = forum_context
    source, _ = register_source(route=route, post=post, mapy_url="https://mapy.com/s/d")
    version, _ = record_route_version(source=source, checksum="recoverable")
    approve_version(version)
    mark_source_unavailable(source, error="404")
    source.refresh_from_db()
    route.refresh_from_db()
    assert source.source_status == SourceStatus.UNAVAILABLE
    assert source.last_checked_at is not None
    assert route.is_public
    same, created = record_route_version(source=source, checksum="recoverable")
    assert same.pk == version.pk
    assert created is False
    source.refresh_from_db()
    assert source.source_status == SourceStatus.AVAILABLE
    assert source.last_successful_check_at is not None
    SourceDenylistEntry.objects.create(source_url=source.mapy_url, reason="takedown")
    with pytest.raises(SourceDeniedError):
        register_source(route=route, post=post, mapy_url=source.mapy_url)


def test_same_source_url_cannot_be_attached_to_a_second_route(
    forum_context: tuple[ForumPost, Route],
) -> None:
    post, route = forum_context
    register_source(route=route, post=post, mapy_url="https://mapy.com/s/shared")
    other_route = Route.objects.create()
    with pytest.raises(ValidationError):
        register_source(route=other_route, post=post, mapy_url="https://mapy.com/s/shared")


def test_source_identity_and_version_delete_are_protected(
    forum_context: tuple[ForumPost, Route],
) -> None:
    post, route = forum_context
    source, _ = register_source(route=route, post=post, mapy_url="https://mapy.com/s/identity")
    version, _ = record_route_version(source=source, checksum="identity")
    source.mapy_url = "https://mapy.com/s/changed"
    with pytest.raises(ValidationError):
        source.save()
    source.refresh_from_db()
    with pytest.raises(ValidationError):
        RouteSource.objects.filter(pk=source.pk).update(mapy_url="https://mapy.com/s/changed")
    with pytest.raises(ProtectedError):
        version.delete()
    with pytest.raises(ProtectedError):
        RouteVersion.objects.filter(pk=version.pk).delete()


def test_quarantine_enqueues_multiple_payload_deletions_with_retry(
    forum_context: tuple[ForumPost, Route], tmp_path: Path
) -> None:
    post, route = forum_context
    source, _ = register_source(route=route, post=post, mapy_url="https://mapy.com/s/file")
    source2, _ = register_source(route=route, post=post, mapy_url="https://mapy.com/s/file2")
    with override_settings(MEDIA_ROOT=tmp_path):
        key = "gpx/file.gpx"
        key2 = "gpx/file2.gpx"
        version, _ = record_route_version(source=source, checksum="file", storage_key=key)
        version2, _ = record_route_version(source=source2, checksum="file2", storage_key=key2)
        approve_version(version)
        approve_version(version2)
        quarantine_route(route, reason="duplicate")
        requests = list(PayloadDeletionRequest.objects.order_by("pk"))
        assert [item.storage_key_snapshot for item in requests] == [key, key2]
        assert all(item.status == PayloadDeletionRequest.Status.PENDING for item in requests)
        route.refresh_from_db()
        assert route.lifecycle == RouteLifecycle.QUARANTINED

    files = {key, key2}
    with patch("apps.catalogue.services.default_storage") as storage:
        storage.exists.side_effect = lambda candidate: candidate in files

        def delete_first(candidate: str) -> None:
            if candidate == key2:
                raise OSError("storage offline")
            files.discard(candidate)

        storage.delete.side_effect = delete_first
        completed = process_payload_deletion(requests[0])
        failed = process_payload_deletion(requests[1])
        assert completed.status == PayloadDeletionRequest.Status.COMPLETED
        assert failed.status == PayloadDeletionRequest.Status.FAILED

        storage.delete.side_effect = lambda candidate: files.discard(candidate)
        retried = retry_payload_deletions()
        assert len(retried) == 1
        assert retried[0].status == PayloadDeletionRequest.Status.COMPLETED

    version.refresh_from_db()
    version2.refresh_from_db()
    assert version.original_gpx_storage_key == ""
    assert version2.original_gpx_storage_key == ""
    assert version.payload_removed_at is not None
    assert version2.payload_removed_at is not None
    assert version2.payload_removal_reason == "Payload removed during quarantine."
    requests = list(PayloadDeletionRequest.objects.order_by("pk"))
    assert all(item.status == PayloadDeletionRequest.Status.COMPLETED for item in requests)
    assert requests[1].attempts == 2


def test_payload_deletion_reconciles_after_db_finalization_failure(
    forum_context: tuple[ForumPost, Route],
) -> None:
    post, route = forum_context
    source, _ = register_source(route=route, post=post, mapy_url="https://bikeforum.example/file3")
    version, _ = record_route_version(source=source, checksum="file3", storage_key="gpx/file3.gpx")
    approve_version(version)
    quarantine_route(route, reason="duplicate")
    request = PayloadDeletionRequest.objects.get(version=version)

    with patch("apps.catalogue.services.default_storage") as storage:
        storage.exists.return_value = True
        storage.delete.return_value = None
        with patch.object(RouteVersion, "save", side_effect=OSError("database unavailable")):
            failed = process_payload_deletion(request)
        assert failed.status == PayloadDeletionRequest.Status.FAILED

    version.refresh_from_db()
    assert version.original_gpx_storage_key == "gpx/file3.gpx"
    request.refresh_from_db()
    assert request.attempts == 1
    assert request.completed_at is None

    with patch("apps.catalogue.services.default_storage") as storage:
        storage.exists.return_value = False
        completed = process_payload_deletion(request)
    version.refresh_from_db()
    assert completed.status == PayloadDeletionRequest.Status.COMPLETED
    assert version.original_gpx_storage_key == ""
    assert version.payload_removed_at is not None


def test_approved_version_pointer_cannot_cross_routes_or_invalid_versions(
    forum_context: tuple[ForumPost, Route],
) -> None:
    post, first = forum_context
    second = Route.objects.create()
    first_source, _ = register_source(route=first, post=post, mapy_url="https://mapy.com/s/first")
    second_source, _ = register_source(
        route=second, post=post, mapy_url="https://mapy.com/s/second"
    )
    valid, _ = record_route_version(source=first_source, checksum="valid")
    other, _ = record_route_version(source=second_source, checksum="other")
    invalid, _ = record_route_version(
        source=first_source, checksum="invalid", technical_status=ProcessingStatus.INVALID
    )
    first.current_approved_version = other
    with pytest.raises(ValidationError):
        first.save()
    first.current_approved_version = invalid
    with pytest.raises(ValidationError):
        first.save()
    with pytest.raises((IntegrityError, ProgrammingError)), transaction.atomic():
        Route.objects.filter(pk=first.pk).update(current_approved_version_id=other.pk)
    approve_version(valid)
    first.refresh_from_db()
    assert first.current_approved_version_id == valid.pk


def test_similarity_and_geometry_payload_constraints() -> None:
    first, second = Route.objects.create(), Route.objects.create()
    with pytest.raises(IntegrityError), transaction.atomic():
        SimilarityRelationship.objects.create(
            route_a=first, route_b=first, relationship_type="variant"
        )
    relationship = SimilarityRelationship.objects.create(
        route_a=first,
        route_b=second,
        relationship_type="variant",
        similarity_score=Decimal("0.9"),
    )
    assert relationship.evidence == {}
    thread = ForumThread.objects.create(url="https://bikeforum.example/t/99", title="Thread")
    post = ForumPost.objects.create(thread=thread, url="https://bikeforum.example/t/99#p1")
    source, _ = register_source(route=first, post=post, mapy_url="https://mapy.com/s/e")
    version, _ = record_route_version(source=source, checksum="payload", storage_key="gpx/payload")
    version.payload_removed_at = version.created_at
    version.original_gpx_storage_key = ""
    version.full_clean()
    assert RouteGeometryField().get_prep_value({"type": "LineString"}) == '{"type":"LineString"}'
    field = RouteGeometryField()
    assert field.srid == 4326
    if connection.vendor == "postgresql":
        assert bool(_GIS_AVAILABLE)
        db_type = field.db_type(connection)
        assert db_type is not None
        assert db_type.lower() == "geometry(linestring,4326)"
        geometry_version, _ = record_route_version(
            source=source,
            checksum="geometry",
            normalized_geometry={
                "type": "LineString",
                "coordinates": [[16, 49], [17, 50]],
            },
        )
        geometry_version.refresh_from_db()
        assert geometry_version.normalized_geometry.geom_type == "LineString"
        assert geometry_version.normalized_geometry.srid == 4326
        from django.contrib.gis.geos import GEOSGeometry

        assert RouteVersion.objects.filter(
            normalized_geometry__intersects=GEOSGeometry("LINESTRING (15 48, 18 51)", srid=4326)
        ).exists()
    else:
        assert field.db_type(connection) == "text"


def test_route_version_content_is_immutable_but_payload_removal_is_explicit(
    forum_context: tuple[ForumPost, Route],
) -> None:
    post, route = forum_context
    source, _ = register_source(route=route, post=post, mapy_url="https://mapy.com/s/immutable")
    version, _ = record_route_version(
        source=source,
        checksum="immutable",
        storage_key="gpx/immutable.gpx",
        distance_m=Decimal("20.0"),
        elevation_profile=[{"distance_m": 0.0, "elevation_m": 180.0}],
    )
    approve_version(version)
    version.refresh_from_db()
    assert version.approved_at is not None
    approved_at = version.approved_at
    version.approved_at = None
    with pytest.raises(ValidationError):
        version.save()
    version.refresh_from_db()
    version.approved_at = approved_at + timedelta(seconds=1)
    with pytest.raises(ValidationError):
        version.save()
    version.refresh_from_db()
    with pytest.raises((IntegrityError, ProgrammingError)), transaction.atomic():
        RouteVersion.objects.filter(pk=version.pk).update(approved_at=None)
    with pytest.raises((IntegrityError, ProgrammingError)), transaction.atomic():
        RouteVersion.objects.filter(pk=version.pk).update(
            approved_at=approved_at + timedelta(seconds=1)
        )
    version.checksum = "changed"
    with pytest.raises(ValidationError):
        version.save()
    version.refresh_from_db()
    version.elevation_profile = [{"distance_m": 0.0, "elevation_m": 220.0}]
    with pytest.raises(ValidationError):
        version.save()
    version.refresh_from_db()
    with pytest.raises(ValidationError, match="elevation_profile"):
        RouteVersion.objects.filter(pk=version.pk).update(
            elevation_profile=[{"distance_m": 0.0, "elevation_m": 220.0}]
        )
    assert version.elevation_profile == [{"distance_m": 0.0, "elevation_m": 180.0}]
    with pytest.raises((IntegrityError, ProgrammingError)), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE catalogue_routeversion SET elevation_profile = %s WHERE id = %s",
                [json.dumps([{"distance_m": 0.0, "elevation_m": 220.0}]), version.pk],
            )
    version.refresh_from_db()
    with pytest.raises((IntegrityError, ProgrammingError)), transaction.atomic():
        RouteVersion.objects.filter(pk=version.pk).update(checksum="direct-change")
    with pytest.raises((IntegrityError, ProgrammingError)), transaction.atomic():
        RouteVersion.objects.filter(pk=version.pk).update(validation_error="direct-change")
    with pytest.raises((IntegrityError, ProgrammingError)), transaction.atomic():
        RouteVersion.objects.filter(pk=version.pk).update(
            created_at=version.created_at.replace(year=2024)
        )
    version.source = RouteSource.objects.create(route=route, mapy_url="https://mapy.com/s/other")
    with pytest.raises(ValidationError):
        version.save()
    version.refresh_from_db()
    version.distance_m = Decimal("21.0")
    with pytest.raises(ValidationError):
        version.save()
    version.refresh_from_db()
    version.validation_error = "rewritten"
    with pytest.raises(ValidationError):
        version.save()
    version.refresh_from_db()
    version.created_at = version.created_at.replace(year=2024)
    with pytest.raises(ValidationError):
        version.save()
    version.refresh_from_db()
    version.original_gpx_storage_key = ""
    version.payload_removed_at = version.created_at
    version.payload_removal_reason = "retention policy"
    version.save(
        update_fields=["original_gpx_storage_key", "payload_removed_at", "payload_removal_reason"]
    )
    version.refresh_from_db()
    version.payload_removal_reason = "rewritten"
    with pytest.raises(ValidationError):
        version.save()
    with pytest.raises((IntegrityError, ProgrammingError)), transaction.atomic():
        RouteVersion.objects.filter(pk=version.pk).update(payload_removed_at=None)
    with pytest.raises((IntegrityError, ProgrammingError)), transaction.atomic():
        RouteVersion.objects.filter(pk=version.pk).update(payload_removal_reason="rewritten")
    assert RouteVersion.objects.get(pk=version.pk).payload_removed_at is not None
