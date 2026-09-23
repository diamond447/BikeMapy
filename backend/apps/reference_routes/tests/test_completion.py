from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from typing import cast

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import close_old_connections, connection
from django.db.models.deletion import ProtectedError
from django.test import Client, override_settings
from django.utils import timezone

from apps.accounts.competition_services import create_competition
from apps.accounts.models import CompetitionMembership, ImportedActivity, Player, StravaSyncState
from apps.reference_routes.completion_services import (
    CompletionLeaseLost,
    calculate_completion,
    schedule_completion,
)
from apps.reference_routes.models import (
    CompletionStatus,
    CompletionSubject,
    ReferenceCollection,
    ReferenceImport,
    ReferenceRoute,
    ReferenceRouteVersion,
    ReferenceSourceKind,
    RouteCompletion,
    RouteCompletionEvidence,
    RouteCompletionJob,
    RouteCompletionMonthly,
)
from apps.reference_routes.tasks import (
    _claim_completion_dispatch,
    _publish_completion_dispatch,
    calculate_route_completion,
    dispatch_completion_jobs,
)

pytestmark = pytest.mark.django_db


def _gis_runtime_available() -> bool:
    try:
        from django.contrib.gis.geos import GEOSGeometry  # noqa: F401
    except (ImportError, ImproperlyConfigured, OSError):
        return False
    return True


requires_gis_runtime = pytest.mark.skipif(
    not _gis_runtime_available(), reason="requires the GeoDjango GEOS/GDAL runtime"
)
requires_postgis = pytest.mark.skipif(
    connection.vendor != "postgresql", reason="requires the PostGIS completion geometry field"
)


def _line_geometry(coordinates: list[list[float]]) -> object:
    geometry = {"type": "LineString", "coordinates": coordinates}
    if connection.vendor != "postgresql":
        return json.dumps(geometry, separators=(",", ":"))

    # The lightweight SQLite CI image intentionally does not install GDAL.
    from django.contrib.gis.geos import GEOSGeometry

    return GEOSGeometry(json.dumps(geometry), srid=4326)


def _player(athlete_id: int) -> Player:
    user = get_user_model().objects.create_user(username=f"completion-{athlete_id}")
    return Player.objects.create(user=user, strava_athlete_id=athlete_id)


def _version() -> ReferenceRouteVersion:
    collection = ReferenceCollection.objects.create(
        slug="completion-route",
        name="Completion route",
        source_kind=ReferenceSourceKind.VIA_CZECHIA,
        source_url="https://example.invalid/source",
        attribution="Test source",
        licence="Test licence",
        permission_granted=True,
        active=True,
    )
    source_import = ReferenceImport.objects.create(
        collection=collection,
        checksum="a" * 64,
        endpoint="https://example.invalid/source",
        raw_payload={},
        raw_response=b"{}",
        raw_response_sha256="a" * 64,
        response_metadata={"source_available": True},
        status="valid",
    )
    route = ReferenceRoute.objects.create(
        collection=collection,
        source_identifier="completion-route",
        title="Completion route",
    )
    version = ReferenceRouteVersion.objects.create(
        route=route,
        source_import=source_import,
        version_number=1,
        checksum="b" * 64,
        source_geometry={"type": "LineString", "coordinates": [[14, 50], [14.1, 50]]},
        normalized_geometry=_line_geometry([[14, 50], [14.1, 50]]),
        attribution="Test source",
        attribution_metadata={
            "attribution_text": "Test",
            "attribution_url": "https://example.invalid/attribution",
            "licence": "Test",
            "licence_uri": "https://example.invalid/licence",
            "derivative_offer_url": "https://example.invalid/offer",
            "rightsholder": "Test",
            "contact_url": "https://example.invalid/contact",
            "source_url": "https://example.invalid/source",
        },
        validation_status="valid",
    )
    route.current_version = version
    route.save(update_fields=("current_version", "updated_at"))
    return version


def _activity(
    player: Player, provider_id: str, coordinates: list[list[float]], day: date
) -> ImportedActivity:
    return ImportedActivity.objects.create(
        player=player,
        provider_activity_id=provider_id,
        calendar_date=day,
        started_at=timezone.now(),
        geometry=_line_geometry(coordinates),
        geometry_hash=provider_id,
    )


@requires_gis_runtime
def test_player_completion_is_unique_and_monthly_distance_is_not_double_counted() -> None:
    version = _version()
    player = _player(1)
    _activity(player, "one", [[14, 50.0001], [14.05, 50.0001]], date(2026, 1, 2))
    _activity(player, "two", [[14, 50.0001], [14.05, 50.0001]], date(2026, 1, 3))
    result = calculate_completion(version, CompletionSubject.PLAYER, player=player)
    assert result.status == CompletionStatus.FRESH
    assert result.covered_length_meters <= result.total_length_meters
    assert result.covered_length_meters > Decimal("0")
    assert (
        RouteCompletionMonthly.objects.get(
            route_version=version, player=player, month=date(2026, 1, 1)
        ).covered_length_meters
        == result.covered_length_meters
    )


@requires_gis_runtime
def test_disjoint_union_preserves_multiline_monthly_geometry_and_evidence_is_append_only() -> None:
    version = _version()
    player = _player(8)
    _activity(player, "left", [[14, 50], [14.02, 50]], date(2026, 5, 1))
    _activity(player, "right", [[14.08, 50], [14.1, 50]], date(2026, 5, 2))
    calculate_completion(version, CompletionSubject.PLAYER, player=player)
    monthly = RouteCompletionMonthly.objects.get(
        route_version=version, player=player, month=date(2026, 5, 1)
    )
    # The monthly geometry for one month is allowed to retain disjoint line
    # components; coercing this to a LineString would lose one contribution.
    assert "MULTILINESTRING" in str(monthly.covered_geometry).upper()
    evidence = RouteCompletionEvidence.objects.first()
    assert evidence is not None
    with pytest.raises(ValidationError):
        evidence.save()
    with pytest.raises(ProtectedError):
        evidence.delete()
    with pytest.raises(ValidationError):
        RouteCompletionEvidence.objects.filter(pk=evidence.pk).update(
            provider_activity_id="changed"
        )
    before = RouteCompletionEvidence.objects.count()
    calculate_completion(version, CompletionSubject.PLAYER, player=player)
    assert RouteCompletionEvidence.objects.count() == before * 2


@requires_gis_runtime
@requires_postgis
def test_dense_evidence_keeps_api_geometry_equal_to_full_completion_metric() -> None:
    version = _version()
    player = _player(801)
    for index in range(1000):
        _activity(
            player,
            f"dense-{index}",
            [[15, 51], [15.05, 51]],
            date(2026, 6, 1),
        )
    _activity(player, "dense-contributor", [[14, 50.0001], [14.05, 50.0001]], date(2026, 6, 1))
    result = calculate_completion(version, CompletionSubject.PLAYER, player=player)
    completion = RouteCompletion.objects.get(pk=result.pk)
    assert (
        RouteCompletionEvidence.objects.filter(
            completion=completion, evidence_generation=completion.evidence_generation
        ).count()
        == 1001
    )
    assert completion.covered_geometry is not None
    metric_geometry = completion.covered_geometry.clone()
    metric_geometry.transform(5514)
    assert (
        Decimal(str(metric_geometry.length)).quantize(Decimal("0.001"))
        == result.covered_length_meters
    )


@requires_gis_runtime
@requires_postgis
@override_settings(
    GAME_ENABLED=True,
    REFERENCE_ROUTE_VIA_CZECHIA_ENABLED=True,
    STRAVA_OAUTH_CLIENT_ID="client",
    STRAVA_OAUTH_CLIENT_SECRET="secret",
    STRAVA_TOKEN_ENCRYPTION_KEY="token-key",
    STRAVA_IDENTITY_GUARD_KEY="identity-key",
)
def test_via_czechia_parent_and_stage_share_completion_metrics_in_api() -> None:
    parent_version = _version()
    parent = parent_version.route
    collection = parent.collection
    source_import = parent_version.source_import
    stage = ReferenceRoute.objects.create(
        collection=collection,
        parent=parent,
        source_identifier="completion-stage",
        title="Completion stage",
        route_number="1A",
    )
    stage_version = ReferenceRouteVersion.objects.create(
        route=stage,
        source_import=source_import,
        version_number=1,
        checksum="c" * 64,
        source_geometry={"type": "LineString", "coordinates": [[14, 50], [14.05, 50]]},
        normalized_geometry=_line_geometry([[14, 50], [14.05, 50]]),
        attribution="Test source",
        attribution_metadata=parent_version.attribution_metadata,
        validation_status="valid",
    )
    stage_version.active = True
    stage_version.save(update_fields=("active",))
    parent_version.active = True
    parent_version.save(update_fields=("active",))
    parent.active = True
    parent.publication_status = "approved"
    parent.save(update_fields=("active", "publication_status", "updated_at"))
    stage.current_version = stage_version
    stage.active = True
    stage.publication_status = "approved"
    stage.save(update_fields=("current_version", "active", "publication_status", "updated_at"))
    player = _player(802)
    competition, _ = create_competition(player, name="Via completion")
    _activity(player, "via-parent-stage", [[14, 50.0001], [14.1, 50.0001]], date(2026, 7, 1))
    parent_result = calculate_completion(parent_version, CompletionSubject.PLAYER, player=player)
    stage_result = calculate_completion(stage_version, CompletionSubject.PLAYER, player=player)

    client = Client()
    session = client.session
    session["player_id"] = player.pk
    session["player_session_epoch"] = player.session_epoch
    session.save()
    response = client.get(
        f"/api/v1/game/reference-routes/{parent.pk}/completion/?competition_id={competition.pk}"
    )
    assert response.status_code == 200, response.json()
    payload = response.json()
    stage_payload = next(item for item in payload["stages"] if item["route_id"] == str(stage.pk))
    assert payload["source_kind"] == ReferenceSourceKind.VIA_CZECHIA
    assert (
        Decimal(str(payload["player"]["covered_length_meters"])).quantize(Decimal("0.001"))
        == parent_result.covered_length_meters
    )
    assert (
        Decimal(str(stage_payload["player"]["covered_length_meters"])).quantize(Decimal("0.001"))
        == stage_result.covered_length_meters
    )
    assert (
        Decimal(str(payload["player"]["completion_percent"])).quantize(Decimal("0.001"))
        == parent_result.completion_percent
    )
    assert (
        Decimal(str(stage_payload["player"]["completion_percent"])).quantize(Decimal("0.001"))
        == stage_result.completion_percent
    )


@requires_gis_runtime
def test_competition_completion_is_union_and_reverses_after_activity_removal() -> None:
    version = _version()
    first = _player(2)
    second = _player(3)
    competition, _ = create_competition(first, name="Union")
    CompetitionMembership.objects.create(competition=competition, player=second, color="#123456")
    _activity(first, "first", [[14, 50], [14.05, 50]], date(2026, 2, 1))
    activity = _activity(second, "second", [[14.05, 50], [14.1, 50]], date(2026, 2, 2))
    union = calculate_completion(version, CompletionSubject.COMPETITION, competition=competition)
    individual = calculate_completion(version, CompletionSubject.PLAYER, player=first)
    assert union.covered_length_meters > individual.covered_length_meters
    activity.delete()
    reversed_result = calculate_completion(
        version, CompletionSubject.COMPETITION, competition=competition
    )
    assert reversed_result.covered_length_meters == individual.covered_length_meters


@requires_gis_runtime
def test_completion_job_is_idempotent_and_exposes_fresh_state() -> None:
    version = _version()
    player = _player(4)
    _activity(player, "queued", [[14, 50], [14.02, 50]], date(2026, 3, 1))
    job = schedule_completion(version, CompletionSubject.PLAYER, player=player, reason="test")
    assert job.status == RouteCompletionJob.Status.PENDING
    assert calculate_route_completion.apply(args=[job.pk]).get()["status"] == "complete"
    assert calculate_route_completion.apply(args=[job.pk]).get()["status"] == "complete"


def test_new_activity_fences_a_running_completion_job() -> None:
    version = _version()
    player = _player(6)
    job = schedule_completion(version, CompletionSubject.PLAYER, player=player, reason="initial")
    job.status = RouteCompletionJob.Status.RUNNING
    job.lease_token = "old-worker"
    job.lease_until = timezone.now()
    job.save(update_fields=("status", "lease_token", "lease_until"))

    replacement = schedule_completion(
        version, CompletionSubject.PLAYER, player=player, reason="activity-change"
    )

    replacement.refresh_from_db()
    assert replacement.status == RouteCompletionJob.Status.PENDING
    assert replacement.lease_token == ""
    assert replacement.lease_until is None


def test_dispatcher_requeues_expired_worker_leases(monkeypatch: pytest.MonkeyPatch) -> None:
    version = _version()
    player = _player(7)
    job = schedule_completion(version, CompletionSubject.PLAYER, player=player, reason="initial")
    job.status = RouteCompletionJob.Status.RUNNING
    job.lease_token = "expired-worker"
    job.lease_until = timezone.now()
    job.save(update_fields=("status", "lease_token", "lease_until"))
    published: list[int] = []
    monkeypatch.setattr(calculate_route_completion, "delay", published.append)

    assert dispatch_completion_jobs(limit=1) == {"dispatched": 1}
    assert published == [job.pk]
    job.refresh_from_db()
    assert job.status == RouteCompletionJob.Status.FAILED
    assert job.error == "Worker lease expired."


def test_worker_commit_is_fenced_when_subject_is_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    version = _version()
    player = _player(9)
    job = schedule_completion(version, CompletionSubject.PLAYER, player=player, reason="initial")

    def replaced(*args: object, **kwargs: object) -> object:
        schedule_completion(version, CompletionSubject.PLAYER, player=player, reason="replacement")
        raise CompletionLeaseLost("superseded")

    monkeypatch.setattr("apps.reference_routes.tasks.calculate_completion", replaced)
    result = calculate_route_completion.apply(args=[job.pk]).get()

    assert result["status"] == "in_progress"
    job.refresh_from_db()
    assert job.status == RouteCompletionJob.Status.PENDING
    assert job.lease_token == ""
    assert not RouteCompletionEvidence.objects.filter(completion__route_version=version).exists()


def test_dispatch_claim_is_single_use_and_releases_on_publish_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    version = _version()
    player = _player(10)
    job = schedule_completion(version, CompletionSubject.PLAYER, player=player, reason="dispatch")
    claim = _claim_completion_dispatch()
    assert claim is not None
    assert _claim_completion_dispatch() is None

    def fail_publish(*args: object, **kwargs: object) -> None:
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(calculate_route_completion, "delay", fail_publish)
    assert not _publish_completion_dispatch(*claim)
    job.refresh_from_db()
    assert job.dispatch_token == ""
    assert job.dispatch_lease_until is None
    assert job.next_attempt_at <= timezone.now()


def test_successful_dispatch_lease_blocks_an_immediate_second_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    version = _version()
    player = _player(12)
    job = schedule_completion(version, CompletionSubject.PLAYER, player=player, reason="dispatch")
    claim = _claim_completion_dispatch()
    assert claim is not None
    published: list[int] = []
    monkeypatch.setattr(calculate_route_completion, "delay", published.append)

    assert _publish_completion_dispatch(*claim)
    assert published == [job.pk]
    assert _claim_completion_dispatch() is None
    job.refresh_from_db()
    assert job.dispatch_token
    assert job.dispatch_lease_until is not None


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="requires PostGIS row locking")
def test_concurrent_completion_workers_have_one_effective_claim() -> None:
    version = _version()
    player = _player(11)
    _activity(player, "concurrent", [[14, 50], [14.02, 50]], date(2026, 6, 1))
    job = schedule_completion(version, CompletionSubject.PLAYER, player=player, reason="race")

    def run_worker() -> dict[str, object]:
        close_old_connections()
        try:
            return cast(dict[str, object], calculate_route_completion.apply(args=[job.pk]).get())
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: run_worker(), range(2)))
    assert sorted(str(result["status"]) for result in results) == ["complete", "in_progress"]
    assert RouteCompletionEvidence.objects.filter(completion__route_version=version).count() == 1


@override_settings(
    GAME_ENABLED=True,
    STRAVA_OAUTH_CLIENT_ID="client",
    STRAVA_OAUTH_CLIENT_SECRET="secret",
    STRAVA_TOKEN_ENCRYPTION_KEY="token-key",
    STRAVA_IDENTITY_GUARD_KEY="identity-key",
    REFERENCE_ROUTE_VIA_CZECHIA_ENABLED=True,
)
@requires_gis_runtime
def test_private_completion_api_exposes_fresh_and_pending_projections() -> None:
    version = _version()
    route = version.route
    version.active = True
    version.save(update_fields=("active",))
    route.active = True
    route.publication_status = "approved"
    route.save(update_fields=("active", "publication_status", "updated_at"))
    player = _player(5)
    competition, _ = create_competition(player, name="API competition")
    StravaSyncState.objects.create(player=player, status="running")
    activity = _activity(player, "api", [[14, 50], [14.02, 50]], date(2026, 4, 1))
    calculate_completion(version, CompletionSubject.PLAYER, player=player)

    client = Client()
    client.force_login(player.user)
    session = client.session
    session["player_id"] = player.pk
    session["player_session_epoch"] = player.session_epoch
    session.save()
    response = client.get(f"/api/v1/game/reference-routes/{route.pk}/completion/")

    assert response.status_code == 200
    body = response.json()
    assert body["player"]["status"] == "fresh"
    assert body["player"]["covered_length_meters"] != "0.000"
    if connection.vendor == "postgresql":
        assert body["player"]["covered_geometry"] is not None
    assert body["player"]["monthly"]
    assert body["geometry"]["type"] == "LineString"
    assert body["attribution"]["attribution_text"] == "Test"
    assert body["competition"]["status"] == "pending"
    assert body["player"]["partial"] is True
    assert body["competition"]["partial"] is True
    assert body["route_id"] == str(route.pk)
    assert competition.is_active
    assert response["Cache-Control"] == "private, no-store"

    player_completion = RouteCompletion.objects.get(route_version=version, player=player)
    player_completion.status = CompletionStatus.FAILED
    player_completion.error = "database credentials leaked"
    player_completion.save(update_fields=("status", "error", "updated_at"))
    safe_error = client.get(f"/api/v1/game/reference-routes/{route.pk}/completion/").json()
    assert safe_error["player"]["error"] == "completion_unavailable"
    assert "credentials" not in json.dumps(safe_error)

    activity.delete()
    calculate_completion(version, CompletionSubject.PLAYER, player=player)
    recalculated = client.get(f"/api/v1/game/reference-routes/{route.pk}/completion/")
    assert recalculated.status_code == 200
    if connection.vendor == "postgresql":
        assert recalculated.json()["player"]["covered_geometry"] is None

    second, _ = create_competition(player, name="Second API competition")
    calculate_completion(version, CompletionSubject.COMPETITION, competition=second)
    selected = client.get(
        f"/api/v1/game/reference-routes/{route.pk}/completion/?competition_id={second.pk}"
    )
    assert selected.status_code == 200
    assert selected.json()["competition"]["status"] == "fresh"
    outsider = _player(15)
    game_client = Client()
    game_session = game_client.session
    game_session["player_id"] = outsider.pk
    game_session["player_session_epoch"] = outsider.session_epoch
    game_session.save()
    assert (
        game_client.get(
            f"/api/v1/game/reference-routes/{route.pk}/completion/?competition_id={second.pk}"
        ).status_code
        == 404
    )

    with override_settings(GAME_ENABLED=False):
        assert (
            client.get(f"/api/v1/game/reference-routes/{route.pk}/completion/").status_code == 404
        )
    collection = route.collection
    collection.permission_granted = False
    collection.save(update_fields=("permission_granted", "updated_at"))
    assert client.get(f"/api/v1/game/reference-routes/{route.pk}/completion/").status_code == 404
