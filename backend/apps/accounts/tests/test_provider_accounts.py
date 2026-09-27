from __future__ import annotations

# mypy: disable-error-code="import-untyped"

import io
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
from apps.accounts.upload_services import UploadError, _safe_archive_members, parse_activity

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
    _competition()
    with pytest.raises(AccountError):
        create_invited_account(username="rider", email="rider@example.com", invite_code="invalid")
    assert not get_user_model().objects.filter(username="rider").exists()


def test_login_accepts_username_or_email_and_unknown_is_generic() -> None:
    competition = _competition()
    player, temporary = create_invited_account(
        username="rider", email="rider@example.com", invite_code=competition.invite_code
    )
    assert authenticate_player(identifier="rider", password=temporary) == player
    assert authenticate_player(identifier="RIDER@EXAMPLE.COM", password=temporary) == player
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
