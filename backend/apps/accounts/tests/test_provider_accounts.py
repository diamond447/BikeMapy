"""Tests for provider-neutral account onboarding and activity uploads."""

# mypy: disable-error-code="import-untyped"

from __future__ import annotations

import io
import struct
import zipfile
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.account_services import (
    AccountError,
    authenticate_player,
    create_invited_account,
)
from apps.accounts.models import (
    ActivityUploadBatch,
    Competition,
    CompetitionInviteRedemption,
    Player,
)
from apps.accounts.upload_services import (
    UploadError,
    _safe_archive_members,
    create_batch,
    parse_activity,
)

pytestmark = pytest.mark.django_db


def _competition() -> Competition:
    owner_user = get_user_model().objects.create_user(username="owner", password="owner-pass")
    owner = Player.objects.create(user=owner_user, nickname="Owner")
    return Competition.objects.create(owner=owner, name="Private ride", invite_code="RIDE-123")


def test_invited_account_is_hashed_and_joined_atomically() -> None:
    competition = _competition()
    with patch("apps.accounts.account_services.send_mail") as send_mail:
        with TestCase.captureOnCommitCallbacks(execute=True):
            player, temporary = create_invited_account(
                username="rider", email="rider@example.com", invite_code=competition.invite_code
            )
    user = player.user
    assert user.check_password(temporary)
    assert user.password != temporary
    assert player.must_change_password
    assert competition.memberships.filter(player=player).exists()
    assert CompetitionInviteRedemption.objects.filter(player=player).exists()
    send_mail.assert_called_once()
    assert temporary not in str(send_mail.call_args.kwargs.get("subject", ""))


def test_invalid_invite_does_not_create_an_account() -> None:
    competition = _competition()
    with pytest.raises(AccountError):
        create_invited_account(username="rider", email="rider@example.com", invite_code="invalid")
    with pytest.raises(AccountError, match="username"):
        create_invited_account(
            username="bad name", email="rider@example.com", invite_code=competition.invite_code
        )
    with pytest.raises(AccountError, match="email"):
        create_invited_account(
            username="rider", email="invalid", invite_code=competition.invite_code
        )
    create_invited_account(
        username="rider", email="rider@example.com", invite_code=competition.invite_code
    )
    with pytest.raises(AccountError, match="Unable to create"):
        create_invited_account(
            username="rider", email="other@example.com", invite_code=competition.invite_code
        )
    with pytest.raises(AccountError, match="Unable to create"):
        create_invited_account(
            username="other", email="rider@example.com", invite_code=competition.invite_code
        )
    assert get_user_model().objects.filter(username="rider").count() == 1


def test_login_accepts_username_or_email_and_unknown_is_generic() -> None:
    competition = _competition()
    player, temporary = create_invited_account(
        username="rider", email="rider@example.com", invite_code=competition.invite_code
    )
    assert authenticate_player(identifier="rider", password=temporary) == player
    player.user.set_password("permanent-password")
    player.user.save(update_fields=("password",))
    player.must_change_password = False
    player.save(update_fields=("must_change_password",))
    assert (
        authenticate_player(identifier="RIDER@EXAMPLE.COM", password="permanent-password") == player
    )
    assert authenticate_player(identifier="missing@example.com", password=temporary) is None


def test_xml_external_entities_are_rejected() -> None:
    hostile = b"<!DOCTYPE gpx [<!ENTITY xxe SYSTEM 'file:///etc/passwd'>]><gpx>&xxe;</gpx>"
    with pytest.raises(UploadError, match="valid XML"):
        parse_activity(hostile, "ride.gpx")


def test_archive_paths_and_nested_archives_are_rejected() -> None:
    traversal = io.BytesIO()
    with zipfile.ZipFile(traversal, "w") as archive:
        archive.writestr("../private.gpx", b"x")
    with pytest.raises(UploadError, match="unsafe path"):
        _safe_archive_members(traversal.getvalue())

    nested = io.BytesIO()
    with zipfile.ZipFile(nested, "w") as archive:
        archive.writestr("nested.zip", b"PK")
    with pytest.raises(UploadError, match="Nested archives"):
        _safe_archive_members(nested.getvalue())


def test_upload_parsers_accept_gpx_tcx_and_safe_zip_members() -> None:
    gpx = b"""<gpx><metadata><time>2026-09-27T06:00:00Z</time></metadata>
        <trk><trkseg><trkpt lat=\"50.1\" lon=\"14.4\"/>
        <trkpt lat=\"50.2\" lon=\"14.5\"/></trkseg></trk>
    </gpx>"""
    points, started, kind = parse_activity(gpx, "ride.gpx")
    assert points == [(14.4, 50.1), (14.5, 50.2)]
    assert started.year == 2026
    assert kind == "GPX"

    tcx = b"""<TrainingCenterDatabase><Activities><Activity><Track>
        <Trackpoint><Time>2026-09-27T06:00:00Z</Time>
        <Latitude><Degrees>50.1</Degrees></Latitude><Longitude><Degrees>14.4</Degrees></Longitude>
        </Trackpoint><Trackpoint><Latitude><Degrees>50.2</Degrees></Latitude>
        <Longitude><Degrees>14.5</Degrees></Longitude></Trackpoint></Track>
        </Activity></Activities></TrainingCenterDatabase>"""
    tcx_points, _, tcx_kind = parse_activity(tcx, "ride.tcx")
    assert tcx_points == [(14.4, 50.1), (14.5, 50.2)]
    assert tcx_kind == "TCX"

    fit_payload = bytearray([0x40, 0, 0, 20, 0, 3])
    fit_payload.extend(bytes((0, 4, 0x86, 1, 4, 0x86, 253, 4, 0x86)))
    for latitude, longitude in ((50.1, 14.4), (50.2, 14.5)):
        fit_payload.append(0)
        fit_payload.extend(struct.pack("<i", int(latitude * 2**31 / 180)))
        fit_payload.extend(struct.pack("<i", int(longitude * 2**31 / 180)))
        fit_payload.extend(struct.pack("<i", 0))
    fit = bytearray((14, 0x10, 0, 0))
    fit.extend(struct.pack("<I", len(fit_payload)))
    fit.extend(b".FIT\x00\x00")
    fit_points, _, fit_kind = parse_activity(bytes(fit + fit_payload), "ride.fit")
    assert fit_points[0][0] == pytest.approx(14.4, abs=0.001)
    assert fit_points[0][1] == pytest.approx(50.1, abs=0.001)
    assert fit_points[1][0] == pytest.approx(14.5, abs=0.001)
    assert fit_points[1][1] == pytest.approx(50.2, abs=0.001)
    assert fit_kind == "FIT"

    archive_data = io.BytesIO()
    with zipfile.ZipFile(archive_data, "w") as archive:
        archive.writestr("ride.gpx", gpx)
        archive.writestr("notes.txt", b"not an activity")
    members = _safe_archive_members(archive_data.getvalue())
    assert members == [("ride.gpx", gpx), ("notes.txt", b"")]
    with pytest.raises(UploadError, match="ZIP archive is invalid"):
        _safe_archive_members(b"not a zip")
    empty_archive = io.BytesIO()
    with zipfile.ZipFile(empty_archive, "w"):
        pass
    with pytest.raises(UploadError, match="no supported"):
        _safe_archive_members(empty_archive.getvalue())

    with pytest.raises(UploadError, match="supported"):
        parse_activity(b"", "ride.csv")
    bad_gpx = b'<gpx><trkpt lat="95" lon="14"/><trkpt lat="50" lon="14"/></gpx>'
    with pytest.raises(UploadError, match="invalid coordinates"):
        parse_activity(bad_gpx, "ride.gpx")


def test_create_batch_requires_attestation_and_expands_zip() -> None:
    competition = _competition()
    player, _ = create_invited_account(
        username="rider", email="rider@example.com", invite_code=competition.invite_code
    )
    archive_data = io.BytesIO()
    with zipfile.ZipFile(archive_data, "w") as archive:
        archive.writestr("ride.gpx", b"<gpx />")
        archive.writestr("notes.txt", b"not an activity")
    with pytest.raises(UploadError, match="attest"):
        create_batch(player, [("rides.zip", archive_data.getvalue())], attested=False)
    batch = create_batch(player, [("rides.zip", archive_data.getvalue())], attested=True)
    assert batch.total_files == 2
    assert batch.files.count() == 2
    assert batch.files.filter(original_name="notes.txt", content=b"").exists()


@override_settings(PLAYER_ACCOUNTS_ENABLED=True)
def test_upload_endpoints_do_not_disclose_other_players_batches() -> None:
    client = APIClient()
    response = client.get("/api/v1/game/account/uploads/00000000-0000-0000-0000-000000000000/")
    assert response.status_code == 401


@override_settings(PLAYER_ACCOUNTS_ENABLED=True)
def test_local_account_endpoints_cover_onboarding_login_password_and_reset() -> None:
    competition = _competition()
    client = APIClient()

    assert client.post("/api/v1/game/auth/local/onboard/", {}, format="json").status_code == 400
    response = client.post(
        "/api/v1/game/auth/local/onboard/",
        {
            "username": "rider",
            "email": "rider@example.com",
            "invite_code": competition.invite_code,
        },
        format="json",
    )
    assert response.status_code == 201

    assert (
        client.post(
            "/api/v1/game/auth/local/login/",
            {"identifier": "rider", "password": "wrong-password"},
            format="json",
        ).status_code
        == 401
    )
    response = client.post(
        "/api/v1/game/auth/local/login/",
        {"identifier": "rider", "password": "temporary-password"},
        format="json",
    )
    assert response.status_code == 401

    player = Player.objects.get(user__username="rider")
    player.user.set_password("temporary-password")
    player.user.save(update_fields=("password",))
    response = client.post(
        "/api/v1/game/auth/local/login/",
        {"identifier": "rider", "password": "temporary-password"},
        format="json",
    )
    assert response.status_code == 200
    assert client.post("/api/v1/game/account/password/", {}, format="json").status_code == 400
    response = client.post(
        "/api/v1/game/account/password/",
        {"password": "a-secure-password"},
        format="json",
    )
    assert response.status_code == 200
    assert client.get("/api/v1/game/account/github/link/").status_code == 200
    assert client.post("/api/v1/game/auth/local/logout/", {}, format="json").status_code == 204
    assert client.get("/api/v1/game/account/github/link/").status_code == 401

    with patch("apps.accounts.account_services.send_mail") as send_mail:
        response = client.post(
            "/api/v1/game/auth/local/reset/", {"email": "rider@example.com"}, format="json"
        )
    assert response.status_code == 200
    send_mail.assert_called_once()
    assert (
        client.post(
            "/api/v1/game/auth/local/reset/not-a-user/not-a-token/",
            {"password": "a-secure-password"},
            format="json",
        ).status_code
        == 400
    )


@override_settings(PLAYER_ACCOUNTS_ENABLED=True)
def test_upload_api_queues_batches_and_scopes_progress_to_session_player() -> None:
    competition = _competition()
    player, temporary = create_invited_account(
        username="rider", email="rider@example.com", invite_code=competition.invite_code
    )
    player.user.set_password(temporary)
    player.user.save(update_fields=("password",))
    player.must_change_password = False
    player.save(update_fields=("must_change_password",))
    client = APIClient()
    assert (
        client.post(
            "/api/v1/game/auth/local/login/",
            {"identifier": "rider", "password": temporary},
            format="json",
        ).status_code
        == 200
    )

    assert client.post("/api/v1/game/account/uploads/", {}, format="multipart").status_code == 400
    batch = ActivityUploadBatch.objects.create(player=player, total_files=1, attested=True)
    with (
        patch("apps.accounts.account_api.create_batch", return_value=batch),
        patch(
            "apps.accounts.account_api.process_activity_upload_batch_task.apply_async"
        ) as apply_async,
    ):
        response = client.post(
            "/api/v1/game/account/uploads/",
            {
                "attested": "true",
                "files": SimpleUploadedFile("ride.gpx", b"<gpx />", content_type="application/gpx"),
            },
            format="multipart",
        )
    assert response.status_code == 202
    apply_async.assert_called_once_with(args=(str(batch.pk),))
    assert client.get(f"/api/v1/game/account/uploads/{batch.pk}/").status_code == 200
    assert (
        client.get("/api/v1/game/account/uploads/00000000-0000-0000-0000-000000000000/").status_code
        == 404
    )
    assert (
        client.delete(
            "/api/v1/game/account/activities/00000000-0000-0000-0000-000000000000/"
        ).status_code
        == 404
    )
