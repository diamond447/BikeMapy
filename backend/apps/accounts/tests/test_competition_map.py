from __future__ import annotations

from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

import apps.accounts.competition_map_api as competition_map_api
from apps.accounts.competition_services import (
    create_competition,
    grant_sharing_consent,
    join_competition,
)
from apps.accounts.models import CompetitionMembership, ImportedActivity, Player

pytestmark = pytest.mark.django_db

SETTINGS = {
    "GAME_ENABLED": True,
    "COMPETITION_GAME_ENABLED": True,
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


def consent(membership: CompetitionMembership) -> None:
    grant_sharing_consent(
        membership.player,
        membership.competition,
        scope=CompetitionMembership.SharingScope.RECENT,
    )


def viewport(**overrides: str | list[str]) -> dict[str, str | list[str]]:
    return {
        "west": "14",
        "south": "49",
        "east": "15",
        "north": "50",
        "zoom": "12",
        **overrides,
    }


def line_geometry(coordinates: list[tuple[float, float]]) -> object:
    if connection.vendor == "postgresql":
        from django.contrib.gis.geos import LineString

        return LineString(*coordinates, srid=4326)
    return {"type": "LineString", "coordinates": coordinates}


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
    competition, owner_membership = create_competition(owner, name="Trace map", color="#123456")
    _, member_membership = join_competition(
        member, invite_code=competition.invite_code, color="#654321"
    )
    consent(owner_membership)
    consent(member_membership)
    ImportedActivity.objects.create(
        player=owner,
        provider_activity_id="one",
        title="Private title must not escape",
        started_at="2026-09-20T12:30:00Z",
        calendar_date="2026-09-20",
        geometry=line_geometry([(13.5, 49.1), (15.5, 49.3)]),
    )
    ImportedActivity.objects.create(
        player=member,
        provider_activity_id="outside",
        calendar_date="2026-09-21",
        geometry=line_geometry([(16.1, 49.1), (16.2, 49.2)]),
    )
    ImportedActivity.objects.create(
        player=owner,
        provider_activity_id="removed",
        calendar_date="2026-09-22",
        removed_at=timezone.now(),
        geometry=line_geometry([(14.1, 49.1), (14.2, 49.2)]),
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
    assert payload["bounds"] == {"west": 14.0, "south": 49.0, "east": 15.0, "north": 50.0}
    assert all(
        14 <= point[0] <= 15 and 49 <= point[1] <= 50
        for point in activity["geometry"]["coordinates"]
    )


@override_settings(**SETTINGS)
def test_map_excludes_pending_members_and_removed_activities() -> None:
    owner = make_player(113)
    pending = make_player(114)
    competition, owner_membership = create_competition(owner, name="Consent filter")
    join_competition(pending, invite_code=competition.invite_code)
    consent(owner_membership)
    geometry = line_geometry([(14.1, 49.1), (14.2, 49.2)])
    ImportedActivity.objects.create(
        player=owner, provider_activity_id="eligible", geometry=geometry
    )
    ImportedActivity.objects.create(
        player=pending, provider_activity_id="pending", geometry=geometry
    )
    response = session_client(owner).get(
        reverse("game-competition-map", args=[competition.pk]), viewport()
    )
    assert response.status_code == 200
    payload = response.json()
    assert {activity["player_id"] for activity in payload["activities"]} == {owner.pk}
    assert all(member["player_id"] == owner.pk for member in payload["members"])
    pending_payload = (
        session_client(pending)
        .get(reverse("game-competition-map", args=[competition.pk]), viewport())
        .json()
    )
    assert pending_payload["activities"] == []
    assert pending_payload["members"] == []


@override_settings(**SETTINGS)
def test_map_rejects_unbounded_viewports_and_supports_member_filter() -> None:
    owner = make_player(105)
    member = make_player(106)
    competition, owner_membership = create_competition(owner, name="Filters")
    _, member_membership = join_competition(member, invite_code=competition.invite_code)
    consent(owner_membership)
    consent(member_membership)
    owner_activity = line_geometry([(14.1, 49.1), (14.2, 49.2)])
    ImportedActivity.objects.create(
        player=owner, provider_activity_id="owner", geometry=owner_activity
    )
    ImportedActivity.objects.create(
        player=member, provider_activity_id="member", geometry=owner_activity
    )
    url = reverse("game-competition-map", args=[competition.pk])
    client = session_client(owner)
    assert client.get(url, viewport(east="180")).status_code == 400
    assert client.get(url, viewport(west="-180", east="180")).status_code == 400
    assert client.get(url, viewport(west="180", east="-180")).status_code == 400
    response = client.get(url, viewport(member=str(owner.pk)))
    assert response.status_code == 200
    assert {activity["player_id"] for activity in response.json()["activities"]} == {owner.pk}
    assert client.get(url, viewport(member="999999")).status_code == 400


@override_settings(**SETTINGS)
def test_map_supports_antimeridian_viewports_without_leaking_coordinates() -> None:
    owner = make_player(108)
    competition, owner_membership = create_competition(owner, name="Dateline")
    consent(owner_membership)
    ImportedActivity.objects.create(
        player=owner,
        provider_activity_id="dateline",
        geometry=line_geometry([(179.5, 0.0), (-179.5, 0.0), (179.6, 0.2)]),
    )
    response = session_client(owner).get(
        reverse("game-competition-map", args=[competition.pk]),
        {"west": "179", "south": "-1", "east": "-179", "north": "1", "zoom": "8"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["bounds"] == {"west": 179.0, "south": -1.0, "east": -179.0, "north": 1.0}
    assert payload["activities"]
    coordinates = payload["activities"][0]["geometry"]["coordinates"]
    flattened = (
        [point for part in coordinates for point in part]
        if isinstance(coordinates[0][0], list)
        else coordinates
    )
    assert all((179 <= point[0] <= 180) or (-180 <= point[0] <= -179) for point in flattened)
    assert len(flattened) >= 4


@override_settings(**SETTINGS)
def test_map_requires_session() -> None:
    owner = make_player(107)
    competition, _ = create_competition(owner, name="Auth")
    response = Client().get(reverse("game-competition-map", args=[competition.pk]), viewport())
    assert response.status_code == 401


@override_settings(**SETTINGS)
def test_map_caps_dense_activity_results_deterministically() -> None:
    owner = make_player(109)
    competition, owner_membership = create_competition(owner, name="Dense")
    consent(owner_membership)
    geometry = line_geometry([(14.1, 49.1), (14.2, 49.2)])
    ImportedActivity.objects.bulk_create(
        [
            ImportedActivity(
                player=owner,
                provider_activity_id=f"dense-{index:04d}",
                calendar_date="2026-09-21",
                geometry=geometry,
            )
            for index in range(250)
        ]
    )
    url = reverse("game-competition-map", args=[competition.pk])
    response = session_client(owner).get(url, viewport())
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["activities"]) == competition_map_api.MAX_FEATURES_PER_MEMBER
    assert payload["truncated"] is True
    ids = [activity["id"] for activity in payload["activities"]]
    repeat = session_client(owner).get(url, viewport()).json()
    assert ids == [activity["id"] for activity in repeat["activities"]]


@override_settings(**SETTINGS)
def test_map_caps_member_ids_and_metadata_before_activity_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = make_player(110)
    competition, owner_membership = create_competition(owner, name="Member limits")
    consent(owner_membership)
    url = reverse("game-competition-map", args=[competition.pk])
    client = session_client(owner)
    assert client.get(url, viewport(member=[str(index) for index in range(101)])).status_code == 400
    monkeypatch.setattr(competition_map_api, "MAX_MEMBER_METADATA_BYTES", 1)
    response = client.get(url, viewport())
    assert response.status_code == 413
    assert response.json()["code"] == "member_limit"


@override_settings(**SETTINGS)
def test_map_bounds_membership_materialization_but_allows_explicit_late_member() -> None:
    owner = make_player(111)
    competition, owner_membership = create_competition(owner, name="Large membership")
    consent(owner_membership)
    late_members = [make_player(1_000 + index) for index in range(competition_map_api.MAX_MEMBERS)]
    CompetitionMembership.objects.bulk_create(
        [
            CompetitionMembership(
                competition=competition,
                player=member,
                color="#123456",
            )
            for member in late_members
        ]
    )
    CompetitionMembership.objects.filter(competition=competition).update(
        sharing_scope=CompetitionMembership.SharingScope.RECENT,
        sharing_consent_at=timezone.now(),
    )
    client = session_client(owner)
    url = reverse("game-competition-map", args=[competition.pk])

    unfiltered = client.get(url, viewport()).json()
    assert len(unfiltered["members"]) == competition_map_api.MAX_MEMBERS

    late_member = late_members[-1]
    selected = client.get(url, viewport(member=str(late_member.pk)))
    assert selected.status_code == 200
    assert [member["player_id"] for member in selected.json()["members"]] == [late_member.pk]
