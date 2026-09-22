from __future__ import annotations

from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import reverse

from apps.accounts.competition_services import create_competition, join_competition
from apps.accounts.models import ImportedActivity, Player

pytestmark = pytest.mark.django_db

SETTINGS = {
    "GAME_ENABLED": True,
    "STRAVA_OAUTH_CLIENT_ID": "client-id",
    "STRAVA_OAUTH_CLIENT_SECRET": "client-secret",
    "STRAVA_TOKEN_ENCRYPTION_KEY": "test-key",
    "STRAVA_IDENTITY_GUARD_KEY": "test-identity-key",
}


def make_player(athlete_id: int) -> Player:
    user = get_user_model().objects.create_user(username=f"map-player-{athlete_id}")
    return Player.objects.create(
        user=user, strava_athlete_id=athlete_id, strava_display_name=f"Rider {athlete_id}"
    )


def session_client(current: Player) -> Client:
    client = Client()
    session = client.session
    session["player_id"] = current.pk
    session["player_session_epoch"] = current.session_epoch
    session.save()
    return client


def viewport(**overrides: str) -> dict[str, str]:
    return {
        "west": "14",
        "south": "49",
        "east": "15",
        "north": "50",
        "zoom": "12",
        **overrides,
    }


@override_settings(**SETTINGS)
def test_map_requires_membership_and_does_not_enumerate_competitions() -> None:
    owner = make_player(101)
    outsider = make_player(102)
    competition, _ = create_competition(owner, name="Private map")
    client = session_client(outsider)
    inaccessible = client.get(reverse("game-competition-map", args=[competition.pk]), viewport())
    missing = client.get(reverse("game-competition-map", args=[uuid4()]), viewport())
    assert inaccessible.status_code == missing.status_code == 404
    assert inaccessible.json() == missing.json() == {"detail": "Competition not found."}
    assert inaccessible["Cache-Control"] == "private, no-store"
    assert inaccessible["X-Robots-Tag"] == "noindex, nofollow"


@override_settings(**SETTINGS)
def test_map_is_bounded_and_exposes_only_date_geometry_and_member_color() -> None:
    owner = make_player(103)
    member = make_player(104)
    competition, _ = create_competition(owner, name="Trace map", color="#123456")
    join_competition(member, invite_code=competition.invite_code, color="#654321")
    ImportedActivity.objects.create(
        player=owner,
        provider_activity_id="one",
        title="Private title must not escape",
        started_at="2026-09-20T12:30:00Z",
        calendar_date="2026-09-20",
        geometry={"type": "LineString", "coordinates": [[14.1, 49.1], [14.2, 49.2], [14.3, 49.3]]},
    )
    ImportedActivity.objects.create(
        player=member,
        provider_activity_id="outside",
        calendar_date="2026-09-21",
        geometry={"type": "LineString", "coordinates": [[16.1, 49.1], [16.2, 49.2]]},
    )
    response = session_client(owner).get(
        reverse("game-competition-map", args=[competition.pk]), viewport()
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "loaded"
    assert payload["truncated"] is False
    assert payload["members"][0]["color"] == "#123456"
    assert len(payload["activities"]) == 1
    activity = payload["activities"][0]
    assert set(activity) == {"id", "player_id", "calendar_date", "geometry"}
    assert activity["calendar_date"] == "2026-09-20"
    assert "title" not in activity and "started_at" not in activity
    assert len(activity["geometry"]["coordinates"]) <= 3


@override_settings(**SETTINGS)
def test_map_rejects_unbounded_viewports_and_supports_member_filter() -> None:
    owner = make_player(105)
    member = make_player(106)
    competition, _ = create_competition(owner, name="Filters")
    join_competition(member, invite_code=competition.invite_code)
    owner_activity = {"type": "LineString", "coordinates": [[14.1, 49.1], [14.2, 49.2]]}
    ImportedActivity.objects.create(
        player=owner, provider_activity_id="owner", geometry=owner_activity
    )
    ImportedActivity.objects.create(
        player=member, provider_activity_id="member", geometry=owner_activity
    )
    url = reverse("game-competition-map", args=[competition.pk])
    client = session_client(owner)
    assert client.get(url, viewport(east="180")).status_code == 400
    response = client.get(url, viewport(member=str(owner.pk)))
    assert response.status_code == 200
    assert {activity["player_id"] for activity in response.json()["activities"]} == {owner.pk}
    assert client.get(url, viewport(member="999999")).status_code == 400


@override_settings(**SETTINGS)
def test_map_requires_session() -> None:
    owner = make_player(107)
    competition, _ = create_competition(owner, name="Auth")
    response = Client().get(reverse("game-competition-map", args=[competition.pk]), viewport())
    assert response.status_code == 401
