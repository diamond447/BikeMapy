from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.contrib.gis.geos import GEOSGeometry
from django.test import Client, override_settings
from django.utils import timezone

from apps.accounts.competition_services import create_competition
from apps.accounts.models import CompetitionMembership, ImportedActivity, Player
from apps.reference_routes.completion_services import calculate_completion, schedule_completion
from apps.reference_routes.models import (
    CompletionStatus,
    CompletionSubject,
    ReferenceCollection,
    ReferenceImport,
    ReferenceRoute,
    ReferenceRouteVersion,
    ReferenceSourceKind,
    RouteCompletionJob,
    RouteCompletionMonthly,
)
from apps.reference_routes.tasks import calculate_route_completion, dispatch_completion_jobs

pytestmark = pytest.mark.django_db


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
        normalized_geometry=GEOSGeometry(
            '{"type":"LineString","coordinates":[[14,50],[14.1,50]]}', srid=4326
        ),
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
        geometry=GEOSGeometry(
            json.dumps({"type": "LineString", "coordinates": coordinates}), srid=4326
        ),
        geometry_hash=provider_id,
    )


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


@override_settings(REFERENCE_ROUTE_VIA_CZECHIA_ENABLED=True)
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
    _activity(player, "api", [[14, 50], [14.02, 50]], date(2026, 4, 1))
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
    assert body["competition"]["status"] == "pending"
    assert body["route_id"] == str(route.pk)
    assert competition.is_active
    assert response["Cache-Control"] == "private, no-store"
