from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from typing import Any, cast

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import close_old_connections, connection
from django.db.models.deletion import ProtectedError
from django.test import Client, override_settings
from django.utils import timezone

from apps.accounts.competition_services import (
    CURRENT_SHARING_DISCLOSURE_VERSION,
    create_competition,
    grant_sharing_consent,
    join_competition,
    leave_competition,
    remove_member,
    withdraw_sharing_consent,
)
from apps.accounts.models import CompetitionMembership, ImportedActivity, Player, StravaSyncState
from apps.accounts.services import delete_player
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
    allow_completion_evidence_erasure,
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
def test_completion_evidence_erasure_context_only_allows_queryset_delete() -> None:
    version = _version()
    player = _player(23)
    _activity(player, "context", [[14, 50], [14.02, 50]], date(2026, 5, 3))
    calculate_completion(version, CompletionSubject.PLAYER, player=player)
    evidence = RouteCompletionEvidence.objects.get()
    with allow_completion_evidence_erasure():
        with pytest.raises(ValidationError):
            evidence.save()
        with pytest.raises(ProtectedError):
            evidence.delete()
        with pytest.raises(ValidationError):
            RouteCompletionEvidence.objects.filter(pk=evidence.pk).update(
                provider_activity_id="changed"
            )
        with pytest.raises(ValidationError):
            RouteCompletionEvidence.objects.bulk_update([evidence], ["provider_activity_id"])
        with pytest.raises(ValidationError):
            RouteCompletionEvidence.objects.bulk_create([evidence])
        deleted, _ = RouteCompletionEvidence.objects.filter(pk=evidence.pk).delete()
    assert deleted == 1
    assert not RouteCompletionEvidence.objects.filter(pk=evidence.pk).exists()


@requires_gis_runtime
def test_competition_completion_is_union_and_reverses_after_activity_removal() -> None:
    version = _version()
    first = _player(2)
    second = _player(3)
    competition, _ = create_competition(first, name="Union")
    grant_sharing_consent(
        first,
        competition,
        scope="recent",
        disclosure_version=CURRENT_SHARING_DISCLOSURE_VERSION,
        confirmed=True,
    )
    CompetitionMembership.objects.create(competition=competition, player=second, color="#123456")
    grant_sharing_consent(
        second,
        competition,
        scope="recent",
        disclosure_version=CURRENT_SHARING_DISCLOSURE_VERSION,
        confirmed=True,
    )
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
def test_activity_deletion_erases_immutable_completion_evidence() -> None:
    version = _version()
    player = _player(13)
    activity = _activity(player, "privacy", [[14, 50], [14.03, 50]], date(2026, 2, 3))
    completion = calculate_completion(version, CompletionSubject.PLAYER, player=player)
    evidence = RouteCompletionEvidence.objects.get(completion=completion)
    assert evidence.provider_activity_id == "privacy"
    assert evidence.activity_geometry_hash == "privacy"
    activity.delete()
    completion.refresh_from_db()
    assert not RouteCompletionEvidence.objects.filter(pk=evidence.pk).exists()
    assert not RouteCompletionMonthly.objects.filter(route_version=version, player=player).exists()
    assert completion.status == CompletionStatus.PENDING
    assert completion.covered_length_meters == Decimal("0")
    assert completion.completion_percent == Decimal("0")
    assert completion.covered_geometry is None


@override_settings(REFERENCE_ROUTE_VIA_CZECHIA_ENABLED=True)
@requires_gis_runtime
def test_account_deletion_erases_surviving_competition_evidence() -> None:
    version = _version()
    version.active = True
    version.save(update_fields=("active",))
    version.route.active = True
    version.route.save(update_fields=("active", "updated_at"))
    deleted = _player(14)
    survivor = _player(15)
    competition, _ = create_competition(survivor, name="Privacy competition")
    grant_sharing_consent(
        survivor,
        competition,
        scope="recent",
        disclosure_version=CURRENT_SHARING_DISCLOSURE_VERSION,
        confirmed=True,
    )
    CompetitionMembership.objects.create(competition=competition, player=deleted, color="#123456")
    grant_sharing_consent(
        deleted,
        competition,
        scope="recent",
        disclosure_version=CURRENT_SHARING_DISCLOSURE_VERSION,
        confirmed=True,
    )
    _activity(deleted, "deleted", [[14, 50], [14.03, 50]], date(2026, 2, 4))
    calculate_completion(version, CompletionSubject.COMPETITION, competition=competition)
    assert RouteCompletionEvidence.objects.filter(completion__competition=competition).exists()
    delete_player(deleted)
    surviving_completion = RouteCompletion.objects.get(
        route_version=version, competition=competition
    )
    assert surviving_completion.status == CompletionStatus.PENDING
    assert not RouteCompletionEvidence.objects.filter(completion=surviving_completion).exists()
    assert not RouteCompletionMonthly.objects.filter(
        route_version=version, competition=competition
    ).exists()
    assert surviving_completion.covered_geometry is None


@override_settings(REFERENCE_ROUTE_VIA_CZECHIA_ENABLED=True)
def test_competition_creation_and_join_schedule_route_completion() -> None:
    version = _version()
    version.active = True
    version.save(update_fields=("active",))
    version.route.active = True
    version.route.save(update_fields=("active", "updated_at"))
    owner = _player(16)
    joiner = _player(17)
    _activity(joiner, "before-join", [[14, 50], [14.03, 50]], date(2026, 2, 5))
    competition, _ = create_competition(owner, name="Scheduled competition")
    created_job = RouteCompletionJob.objects.get(route_version=version, competition=competition)
    assert created_job.status == RouteCompletionJob.Status.PENDING
    join_competition(joiner, invite_code=competition.invite_code)
    joined_job = RouteCompletionJob.objects.get(route_version=version, competition=competition)
    assert joined_job.status == RouteCompletionJob.Status.PENDING
    assert joined_job.reason == "competition-member-joined"


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


def test_completion_worker_redacts_unexpected_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    version = _version()
    player = _player(18)
    job = schedule_completion(version, CompletionSubject.PLAYER, player=player, reason="failure")

    def fail(*args: object, **kwargs: object) -> object:
        raise RuntimeError("provider token must never be persisted or exposed")

    monkeypatch.setattr("apps.reference_routes.tasks.calculate_completion", fail)
    result = calculate_route_completion.apply(args=[job.pk]).get()
    assert result == {
        "status": "failed",
        "job_id": job.pk,
        "error": "completion_unavailable",
    }
    job.refresh_from_db()
    assert job.error == "completion_unavailable"
    completion = RouteCompletion.objects.get(route_version=version, player=player)
    assert completion.error == "completion_unavailable"


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


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="requires PostGIS row locking")
def test_activity_erasure_fences_worker_after_activity_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    version = _version()
    owner = _player(21)
    member = _player(22)
    competition, _ = create_competition(owner, name="Erasure race")
    CompetitionMembership.objects.create(competition=competition, player=member, color="#123456")
    activity = _activity(owner, "race-delete", [[14, 50], [14.02, 50]], date(2026, 7, 1))
    player_job = schedule_completion(
        version, CompletionSubject.PLAYER, player=owner, reason="race-player"
    )
    competition_job = schedule_completion(
        version, CompletionSubject.COMPETITION, competition=competition, reason="race-competition"
    )

    activity_read = threading.Event()
    release_worker = threading.Event()
    original_union = __import__(
        "apps.reference_routes.completion_services", fromlist=["_union_coverage"]
    )._union_coverage

    def paused_union(*args: object, **kwargs: object) -> object:
        result = original_union(*args, **kwargs)
        activity_read.set()
        assert release_worker.wait(timeout=10)
        return result

    monkeypatch.setattr("apps.reference_routes.completion_services._union_coverage", paused_union)

    def run_worker() -> dict[str, object]:
        close_old_connections()
        try:
            return cast(
                dict[str, object], calculate_route_completion.apply(args=[player_job.pk]).get()
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run_worker)
        assert activity_read.wait(timeout=10)
        activity_id = activity.pk
        activity.delete()
        release_worker.set()
        result = future.result(timeout=10)

    assert result["status"] == "in_progress"
    assert not RouteCompletionEvidence.objects.filter(activity_id=activity_id).exists()
    for completion in RouteCompletion.objects.filter(
        route_version=version, player=owner
    ) | RouteCompletion.objects.filter(route_version=version, competition=competition):
        completion.refresh_from_db()
        assert completion.status == CompletionStatus.PENDING
        assert completion.covered_length_meters == Decimal("0")
        assert completion.completion_percent == Decimal("0")
        assert completion.evidence_digest == ""
    for job in (player_job, competition_job):
        job.refresh_from_db()
        assert job.status == RouteCompletionJob.Status.PENDING
        assert job.lease_token == ""
        assert job.dispatch_token == ""


@override_settings(
    GAME_ENABLED=True,
    COMPETITION_GAME_ENABLED=True,
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
    grant_sharing_consent(
        player,
        competition,
        scope="recent",
        disclosure_version=CURRENT_SHARING_DISCLOSURE_VERSION,
        confirmed=True,
    )
    _activity(player, "api", [[14, 50], [14.02, 50]], date(2026, 4, 1))
    StravaSyncState.objects.create(player=player, status=StravaSyncState.Status.PAUSED)
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
    assert (
        body["player"]["monthly"][0]["gain_length_meters"]
        == body["player"]["monthly"][0]["covered_length_meters"]
    )
    assert body["geometry"]["type"] == "LineString"
    assert body["attribution"]["attribution_text"] == "Test"
    assert body["competition"]["status"] == "pending"
    assert body["player"]["partial"] is True
    assert body["player"]["sync_status"] == "paused"
    assert body["route_id"] == str(route.pk)
    assert competition.is_active
    assert response["Cache-Control"] == "private, no-store"

    failed_competition = RouteCompletion.objects.get(route_version=version, competition=competition)
    failed_competition.status = CompletionStatus.FAILED
    failed_competition.error = "internal credentials must not be exposed"
    failed_competition.save(update_fields=("status", "error", "updated_at"))
    redacted = client.get(f"/api/v1/game/reference-routes/{route.pk}/completion/")
    assert redacted.json()["competition"]["error"] == "completion_unavailable"

    with override_settings(GAME_ENABLED=False):
        assert (
            client.get(f"/api/v1/game/reference-routes/{route.pk}/completion/").status_code == 404
        )
    collection = route.collection
    collection.permission_granted = False
    collection.save(update_fields=("permission_granted", "updated_at"))
    assert client.get(f"/api/v1/game/reference-routes/{route.pk}/completion/").status_code == 404


@override_settings(
    GAME_ENABLED=True,
    COMPETITION_GAME_ENABLED=True,
    STRAVA_OAUTH_CLIENT_ID="client",
    STRAVA_OAUTH_CLIENT_SECRET="secret",
    STRAVA_TOKEN_ENCRYPTION_KEY="token-key",
    STRAVA_IDENTITY_GUARD_KEY="identity-key",
    REFERENCE_ROUTE_VIA_CZECHIA_ENABLED=True,
)
def test_completion_group_sync_status_ignores_nonconsenting_members() -> None:
    version = _version()
    route = version.route
    version.active = True
    version.save(update_fields=("active",))
    route.active = True
    route.publication_status = "approved"
    route.save(update_fields=("active", "publication_status", "updated_at"))
    owner = _player(50)
    active_member = _player(51)
    nonconsenting_member = _player(52)
    competition, _ = create_competition(owner, name="Sync status competition")
    join_competition(active_member, invite_code=competition.invite_code)
    join_competition(nonconsenting_member, invite_code=competition.invite_code)
    grant_sharing_consent(
        owner,
        competition,
        scope="recent",
        disclosure_version=CURRENT_SHARING_DISCLOSURE_VERSION,
        confirmed=True,
    )
    grant_sharing_consent(
        active_member,
        competition,
        scope="full_history",
        disclosure_version=CURRENT_SHARING_DISCLOSURE_VERSION,
        confirmed=True,
    )
    StravaSyncState.objects.update_or_create(
        player=owner, defaults={"status": StravaSyncState.Status.PAUSED}
    )
    StravaSyncState.objects.update_or_create(
        player=active_member, defaults={"status": StravaSyncState.Status.FAILED}
    )
    StravaSyncState.objects.update_or_create(
        player=nonconsenting_member, defaults={"status": StravaSyncState.Status.FAILED}
    )

    client = Client()
    client.force_login(owner.user)
    session = client.session
    session["player_id"] = owner.pk
    session["player_session_epoch"] = owner.session_epoch
    session.save()
    url = f"/api/v1/game/reference-routes/{route.pk}/completion/"

    assert client.get(url).json()["competition"]["sync_status"] == "failed"
    withdraw_sharing_consent(active_member, competition)
    body = client.get(url).json()
    assert body["competition"]["sync_status"] == "paused"
    assert body["competition"]["partial"] is True


@override_settings(
    GAME_ENABLED=True,
    COMPETITION_GAME_ENABLED=True,
    STRAVA_OAUTH_CLIENT_ID="client",
    STRAVA_OAUTH_CLIENT_SECRET="secret",
    STRAVA_TOKEN_ENCRYPTION_KEY="token-key",
    STRAVA_IDENTITY_GUARD_KEY="identity-key",
    REFERENCE_ROUTE_VIA_CZECHIA_ENABLED=True,
)
@requires_gis_runtime
@pytest.mark.parametrize("operation", ("withdraw", "leave", "remove"))
@pytest.mark.parametrize(
    "job_status", (RouteCompletionJob.Status.FAILED, RouteCompletionJob.Status.RUNNING)
)
def test_competition_privacy_reset_redacts_projection_before_recomputation(
    operation: str, job_status: str
) -> None:
    version = _version()
    route = version.route
    version.active = True
    version.save(update_fields=("active",))
    route.active = True
    route.publication_status = "approved"
    route.save(update_fields=("active", "publication_status", "updated_at"))
    owner = _player(60)
    member = _player(61)
    competition, _ = create_competition(owner, name=f"Privacy {operation}")
    join_competition(member, invite_code=competition.invite_code)
    for player, scope in (
        (owner, "recent"),
        (member, "full_history"),
    ):
        grant_sharing_consent(
            player,
            competition,
            scope=scope,
            disclosure_version=CURRENT_SHARING_DISCLOSURE_VERSION,
            confirmed=True,
        )
    _activity(owner, f"privacy-{operation}", [[14, 50], [14.03, 50]], date(2026, 8, 1))
    completion = calculate_completion(
        version, CompletionSubject.COMPETITION, competition=competition
    )
    assert completion.status == CompletionStatus.FRESH
    assert completion.covered_geometry is not None
    assert RouteCompletionMonthly.objects.filter(competition=competition).exists()
    job = RouteCompletionJob.objects.filter(route_version=version, competition=competition).latest(
        "pk"
    )
    job.status = job_status
    job.error = (
        "delayed worker" if job_status == RouteCompletionJob.Status.RUNNING else "worker failed"
    )
    job.lease_token = "delayed-worker" if job_status == RouteCompletionJob.Status.RUNNING else ""
    job.save(update_fields=("status", "error", "lease_token"))

    if operation == "withdraw":
        withdraw_sharing_consent(owner, competition)
    elif operation == "leave":
        leave_competition(member, competition)
    else:
        remove_member(owner, competition, member)

    completion.refresh_from_db()
    redacted = cast(Any, completion)
    assert redacted.status == CompletionStatus.PENDING
    assert redacted.covered_geometry is None
    assert redacted.covered_length_meters == Decimal("0")
    assert redacted.completion_percent == Decimal("0")
    assert not RouteCompletionMonthly.objects.filter(competition=competition).exists()
    job.refresh_from_db()
    assert job.status == RouteCompletionJob.Status.PENDING
    assert job.lease_token == ""

    client = Client()
    client.force_login(owner.user)
    session = client.session
    session["player_id"] = owner.pk
    session["player_session_epoch"] = owner.session_epoch
    session.save()
    response = client.get(f"/api/v1/game/reference-routes/{route.pk}/completion/")
    assert response.status_code == 200
    projection = response.json()["competition"]
    assert projection["status"] == "pending"
    assert projection["covered_length_meters"] == "0.000"
    assert projection["completion_percent"] == "0.000"
    assert projection["covered_geometry"] is None
    assert projection["monthly"] == []
