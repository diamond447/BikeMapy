from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, override_settings
from django.urls import reverse

from apps.accounts.activity_services import remove_activity
from apps.accounts.capture_services import calculate_capture
from apps.accounts.competition_services import create_competition, join_competition
from apps.accounts.models import CaptureCalculation, ImportedActivity, Player
from apps.accounts.tasks import recompute_competition_results_task

pytestmark = pytest.mark.django_db

SETTINGS = {
    "GAME_ENABLED": True,
    "STRAVA_OAUTH_CLIENT_ID": "client-id",
    "STRAVA_OAUTH_CLIENT_SECRET": "client-secret",
    "STRAVA_TOKEN_ENCRYPTION_KEY": "test-key",
    "STRAVA_IDENTITY_GUARD_KEY": "test-identity-key",
}


def make_player(athlete_id: int) -> Player:
    user = get_user_model().objects.create_user(username=f"capture-api-{athlete_id}")
    return Player.objects.create(
        user=user,
        strava_athlete_id=athlete_id,
        strava_display_name=f"Rider {athlete_id}",
    )


def session_client(player: Player) -> Client:
    client = Client()
    session = client.session
    session["player_id"] = player.pk
    session["player_session_epoch"] = player.session_epoch
    session.save()
    return client


def viewport() -> dict[str, str]:
    return {
        "west": "14",
        "south": "49",
        "east": "15",
        "north": "50",
        "zoom": "10",
    }


def line_geometry(coordinates: list[tuple[float, float]]) -> object:
    from django.contrib.gis.geos import LineString

    return LineString(*coordinates, srid=4326)


@override_settings(**SETTINGS)
def test_capture_is_private_bounded_and_exposes_pending_help() -> None:
    owner = make_player(601)
    outsider = make_player(602)
    competition, _ = create_competition(owner, name="Private capture")
    url = reverse("game-competition-capture", args=[competition.pk])

    response = session_client(outsider).get(url, viewport())
    missing = session_client(owner).get(
        reverse("game-competition-capture", args=[uuid4()]), viewport()
    )
    assert response.status_code == missing.status_code == 404
    assert response.json() == missing.json() == {"detail": "Competition not found."}
    assert response["Cache-Control"] == "private, no-store"

    payload = session_client(owner).get(url, viewport()).json()
    assert payload["status"] == "pending"
    assert payload["is_final"] is False
    assert payload["faces"] == []
    assert float(payload["members"][0]["area_m2"]) == 0
    assert "connection_rule" in payload["help"]
    assert payload["limits"]["max_faces"] > 0


@pytest.mark.skipif(connection.vendor != "postgresql", reason="capture API requires PostGIS")
@override_settings(**SETTINGS)
def test_capture_monthly_delta_is_signed_and_visibility_keeps_ranks() -> None:
    owner = make_player(603)
    member = make_player(604)
    competition, _ = create_competition(owner, name="Capture ranking", color="#123456")
    join_competition(member, invite_code=competition.invite_code, color="#654321")
    square = [(14.1, 49.1), (14.2, 49.1), (14.2, 49.2), (14.1, 49.2), (14.1, 49.1)]
    activity = ImportedActivity.objects.create(
        player=owner,
        provider_activity_id="capture-api-owner",
        started_at="2025-01-02T12:00:00Z",
        calendar_date="2025-01-02",
        geometry=line_geometry(square),
    )
    ImportedActivity.objects.create(
        player=member,
        provider_activity_id="capture-api-member",
        started_at="2025-01-04T12:00:00Z",
        calendar_date="2025-01-04",
        geometry=line_geometry(
            [(14.3, 49.1), (14.4, 49.1), (14.4, 49.2), (14.3, 49.2), (14.3, 49.1)]
        ),
    )
    calculate_capture(competition)
    initial_calculation = CaptureCalculation.objects.get(competition=competition, is_current=True)
    initial_calculation.completed_at = datetime(2025, 1, 15, tzinfo=UTC)
    initial_calculation.save(update_fields=("completed_at",))
    url = reverse("game-competition-capture", args=[competition.pk])
    initial = session_client(owner).get(url, viewport()).json()
    owner_initial = next(item for item in initial["members"] if item["player_id"] == owner.pk)
    assert any(float(item["net_change_m2"]) > 0 for item in owner_initial["monthly_net_change_m2"])

    narrow = session_client(owner).get(url, viewport() | {"east": "14.25"}).json()
    owner_narrow = next(item for item in narrow["members"] if item["player_id"] == owner.pk)
    assert owner_narrow["monthly_net_change_m2"] == owner_initial["monthly_net_change_m2"]

    filtered = session_client(owner).get(url, viewport() | {"member": str(owner.pk)}).json()
    assert [item["player_id"] for item in filtered["members"]] == [
        item["player_id"] for item in initial["members"]
    ]
    assert [item["rank"] for item in filtered["members"]] == [
        item["rank"] for item in initial["members"]
    ]

    assert remove_activity(owner, activity.provider_activity_id, reason="privacy")
    job = competition.recomputations.order_by("-generation").first()
    assert job is not None
    recompute_competition_results_task.apply(args=[job.pk]).get()
    latest_calculation = CaptureCalculation.objects.get(competition=competition, is_current=True)
    latest_calculation.completed_at = datetime(2025, 2, 15, tzinfo=UTC)
    latest_calculation.save(update_fields=("completed_at",))
    after = session_client(owner).get(url, viewport()).json()
    owner_after = next(item for item in after["members"] if item["player_id"] == owner.pk)
    assert any(
        item["month"] == "2025-02" and float(item["net_change_m2"]) < 0
        for item in owner_after["monthly_net_change_m2"]
    )
