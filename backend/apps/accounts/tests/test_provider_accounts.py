from __future__ import annotations

import io
import zipfile
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.account_services import AccountError, authenticate_player, create_invited_account
from apps.accounts.models import Competition, CompetitionInviteRedemption, Player
from apps.accounts.upload_services import UploadError, _safe_archive_members, parse_activity

pytestmark = pytest.mark.django_db


def _competition() -> Competition:
    owner_user = get_user_model().objects.create_user(username="owner", password="owner-pass")
    owner = Player.objects.create(user=owner_user, nickname="Owner")
    return Competition.objects.create(owner=owner, name="Private ride", invite_code="RIDE-123")


def test_invited_account_is_hashed_and_joined_atomically() -> None:
    competition = _competition()
    with patch("apps.accounts.account_services.send_mail") as send_mail:
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
    client = Client()
    response = client.get("/api/v1/game/account/uploads/00000000-0000-0000-0000-000000000000/")
    assert response.status_code == 401
