"""Tests for provider-neutral account onboarding and activity uploads."""

# mypy: disable-error-code="import-untyped"

from __future__ import annotations

import io
import os
import struct
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction as django_transaction
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.account_services import (
    AccountError,
    authenticate_player,
    create_invited_account,
)
from apps.accounts.models import (
    ActivityUpload,
    ActivityUploadBatch,
    ActivityUploadDeletion,
    Competition,
    CompetitionInviteRedemption,
    Player,
)
from apps.accounts.services import delete_player
from apps.accounts.upload_services import (
    UploadError,
    _fit_crc,
    _reconstruct_compressed_timestamp,
    _safe_archive_members,
    create_batch,
    parse_activity,
    process_batch,
)
from apps.accounts.upload_tasks import (
    cleanup_expired_activity_uploads_task,
    process_activity_upload_batch_task,
    reconcile_orphan_activity_uploads_task,
    retry_activity_upload_deletions_task,
)

pytestmark = pytest.mark.django_db


def _competition() -> Competition:
    owner_user = get_user_model().objects.create_user(username="owner", password="owner-pass")
    owner = Player.objects.create(user=owner_user, nickname="Owner")
    return Competition.objects.create(owner=owner, name="Private ride", invite_code="RIDE-123")


def _compressed_fit_fixture() -> bytes:
    """A CRC-valid FIT stream with omitted compressed timestamp bytes."""

    payload = bytearray()
    # Session definition: sport=cycling (enum 2).
    payload.extend(bytes((0x41, 0, 0, 18, 0, 1, 5, 1, 0x02)))
    payload.extend(bytes((0x01, 2)))
    # Record definition: signed coordinates, uint timestamp, and one
    # developer byte.  Compressed records omit field 253 from their payload.
    payload.extend(bytes((0x60, 0, 0, 20, 0, 3)))
    payload.extend(bytes((0, 4, 0x85, 1, 4, 0x85, 253, 4, 0x86)))
    payload.extend(bytes((1, 0, 1, 0)))

    def point(latitude: float, longitude: float, timestamp: int, header: int) -> None:
        payload.append(header)
        payload.extend(struct.pack("<i", int(latitude * 2**31 / 180)))
        payload.extend(struct.pack("<i", int(longitude * 2**31 / 180)))
        if not header & 0x80:
            payload.extend(struct.pack("<I", timestamp))
        payload.append(0x2A)  # developer field, also present on compressed records

    point(50.1, 14.4, 1000, 0)
    point(50.2, 14.5, 1000, 0x88)  # equal offset: no false rollover
    point(50.3, 14.6, 1031, 0x87)  # lower offset: one 32-second rollover
    header = bytearray((14, 0x10, 0, 0))
    header.extend(struct.pack("<I", len(payload)))
    header.extend(b".FIT\x00\x00")
    header[12:14] = struct.pack("<H", _fit_crc(bytes(header[:12])))
    result = header + payload
    result.extend(struct.pack("<H", _fit_crc(bytes(result))))
    return bytes(result)


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


def test_onboarding_mail_failure_rolls_back_account_membership_and_invite() -> None:
    competition = _competition()
    with patch(
        "apps.accounts.account_services.send_mail", side_effect=RuntimeError("mail backend down")
    ):
        with pytest.raises(RuntimeError, match="mail backend down"):
            create_invited_account(
                username="rider", email="rider@example.com", invite_code=competition.invite_code
            )
    assert not get_user_model().objects.filter(username="rider").exists()
    assert not competition.memberships.exists()
    assert not competition.invite_redemptions.exists()


def test_local_onboarding_is_csrf_protected_and_does_not_log_bootstrap_secret(
    caplog: Any,
) -> None:
    competition = _competition()
    client = Client(enforce_csrf_checks=True)
    with override_settings(PLAYER_ACCOUNTS_ENABLED=True):
        response = client.post(
            "/api/v1/game/auth/local/onboard/",
            {
                "username": "rider",
                "email": "rider@example.com",
                "invite_code": competition.invite_code,
            },
        )
    assert response.status_code == 403
    with patch("apps.accounts.account_services.send_mail") as send_mail:
        with TestCase.captureOnCommitCallbacks(execute=True):
            _, temporary = create_invited_account(
                username="rider", email="rider@example.com", invite_code=competition.invite_code
            )
    assert temporary not in caplog.text
    assert temporary not in send_mail.call_args.kwargs["subject"]


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


def test_upload_request_file_count_and_expansion_limits_are_enforced(monkeypatch: Any) -> None:
    competition = _competition()
    player, _ = create_invited_account(
        username="rider", email="rider@example.com", invite_code=competition.invite_code
    )
    monkeypatch.setattr("apps.accounts.upload_services.MAX_ACTIVITY_COUNT", 1)
    with pytest.raises(UploadError, match="too many files"):
        create_batch(player, [("one.gpx", b"a"), ("two.gpx", b"b")], attested=True)

    monkeypatch.setattr("apps.accounts.upload_services.MAX_ACTIVITY_COUNT", 100)
    monkeypatch.setattr("apps.accounts.upload_services.MAX_FILE_BYTES", 3)
    with pytest.raises(UploadError, match="size limit"):
        create_batch(player, [("large.gpx", b"four")], attested=True)

    archive_data = io.BytesIO()
    with zipfile.ZipFile(archive_data, "w") as archive:
        archive.writestr("ride.gpx", b"four")
    monkeypatch.setattr("apps.accounts.upload_services.MAX_FILE_BYTES", 25 * 1024 * 1024)
    monkeypatch.setattr("apps.accounts.upload_services.MAX_EXPANDED_BYTES", 3)
    with pytest.raises(UploadError, match="safe limit"):
        create_batch(player, [("rides.zip", archive_data.getvalue())], attested=True)


def test_upload_parsers_accept_gpx_tcx_and_safe_zip_members() -> None:
    gpx = b"""<gpx><metadata><time>2026-09-27T06:00:00Z</time><type>cycling</type></metadata>
        <trk><trkseg><trkpt lat=\"50.1\" lon=\"14.4\"/>
        <trkpt lat=\"50.2\" lon=\"14.5\"/></trkseg></trk>
    </gpx>"""
    points, started, kind = parse_activity(gpx, "ride.gpx")
    assert points == [(14.4, 50.1), (14.5, 50.2)]
    assert started.year == 2026
    assert kind == "GPX"

    tcx = b"""<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">
        <Activities><Activity Sport="Biking"><Track>
        <Trackpoint><Time>2026-09-27T06:00:00Z</Time><Position>
        <LatitudeDegrees>50.1</LatitudeDegrees><LongitudeDegrees>14.4</LongitudeDegrees>
        </Position></Trackpoint><Trackpoint><Position><LatitudeDegrees>50.2</LatitudeDegrees>
        <LongitudeDegrees>14.5</LongitudeDegrees></Position></Trackpoint></Track>
        </Activity></Activities></TrainingCenterDatabase>"""
    tcx_points, _, tcx_kind = parse_activity(tcx, "ride.tcx")
    assert tcx_points == [(14.4, 50.1), (14.5, 50.2)]
    assert tcx_kind == "TCX"

    fit_payload = bytearray([0x41, 0, 0, 18, 0, 1, 5, 1, 0x02, 1, 2])
    fit_payload.extend(bytes((0x40, 0, 0, 20, 0, 3)))
    fit_payload.extend(bytes((0, 4, 0x86, 1, 4, 0x86, 253, 4, 0x86)))
    for latitude, longitude in ((50.1, 14.4), (50.2, 14.5)):
        fit_payload.append(0)
        fit_payload.extend(struct.pack("<i", int(latitude * 2**31 / 180)))
        fit_payload.extend(struct.pack("<i", int(longitude * 2**31 / 180)))
        fit_payload.extend(struct.pack("<i", 0))
    fit = bytearray((14, 0x10, 0, 0))
    fit.extend(struct.pack("<I", len(fit_payload)))
    fit.extend(b".FIT\x00\x00")
    fit[12:14] = struct.pack("<H", _fit_crc(bytes(fit[:12])))
    fit.extend(fit_payload)
    fit.extend(struct.pack("<H", _fit_crc(bytes(fit))))
    fit_points, _, fit_kind = parse_activity(bytes(fit), "ride.fit")
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
    bad_gpx = (
        b"<gpx><metadata><type>cycling</type></metadata>"
        b'<trkpt lat="95" lon="14"/><trkpt lat="50" lon="14"/></gpx>'
    )
    with pytest.raises(UploadError, match="invalid coordinates"):
        parse_activity(bad_gpx, "ride.gpx")
    with pytest.raises(UploadError, match="cycling"):
        parse_activity(b"<gpx><trk><trkpt lat='50' lon='14'/></trk></gpx>", "ride.gpx")
    with pytest.raises(UploadError, match="cycling"):
        parse_activity(
            b'<TrainingCenterDatabase><Activities><Activity Sport="Running"><Track>'
            b"<Trackpoint><Position><LatitudeDegrees>50</LatitudeDegrees>"
            b"<LongitudeDegrees>14</LongitudeDegrees></Position></Trackpoint>"
            b"<Trackpoint><Position><LatitudeDegrees>50.1</LatitudeDegrees>"
            b"<LongitudeDegrees>14.1</LongitudeDegrees></Position></Trackpoint>"
            b"</Track></Activity></Activities></TrainingCenterDatabase>",
            "ride.tcx",
        )


def test_fit_compressed_timestamps_skip_omitted_bytes_and_validate_crc() -> None:
    payload = _compressed_fit_fixture()
    points, started, kind = parse_activity(io.BytesIO(payload), "ride.fit")
    assert [round(point[0], 1) for point in points] == [14.4, 14.5, 14.6]
    assert [round(point[1], 1) for point in points] == [50.1, 50.2, 50.3]
    assert started == datetime.fromtimestamp(1000 + 631065600, tz=UTC)
    assert kind == "FIT"
    assert _reconstruct_compressed_timestamp(1000, 8) == 1000
    assert _reconstruct_compressed_timestamp(1000, 7) == 1031

    corrupted = bytearray(payload)
    corrupted[-3] ^= 0x01
    with pytest.raises(UploadError, match="checksum"):
        parse_activity(io.BytesIO(corrupted), "ride.fit")


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


def test_upload_storage_is_removed_when_row_save_fails(tmp_path: Path) -> None:
    """A storage write must not survive a failed ActivityUpload.save()."""

    competition = _competition()
    player, _ = create_invited_account(
        username="rider", email="rider@example.com", invite_code=competition.invite_code
    )
    with override_settings(ACTIVITY_UPLOAD_ROOT=tmp_path):
        with pytest.raises(RuntimeError, match="row save"):
            with patch.object(ActivityUpload, "save", side_effect=RuntimeError("row save")):
                create_batch(player, [("ride.gpx", io.BytesIO(b"<gpx />"))], attested=True)
        retry_activity_upload_deletions_task()
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


def test_upload_storage_is_removed_when_transaction_commit_fails(tmp_path: Path) -> None:
    """A commit failure after FileField.save() must clean the staged object."""

    competition = _competition()
    player, _ = create_invited_account(
        username="rider", email="rider@example.com", invite_code=competition.invite_code
    )
    real_atomic = django_transaction.atomic
    atomic_calls = 0

    @contextmanager
    def failing_atomic(*args: Any, **kwargs: Any) -> Iterator[None]:
        nonlocal atomic_calls
        atomic_calls += 1
        with real_atomic(*args, **kwargs):
            yield
        if atomic_calls == 1:
            raise RuntimeError("commit failure")

    with override_settings(ACTIVITY_UPLOAD_ROOT=tmp_path):
        with pytest.raises(RuntimeError, match="commit failure"):
            with patch("apps.accounts.upload_services.transaction.atomic", failing_atomic):
                create_batch(player, [("ride.gpx", io.BytesIO(b"<gpx />"))], attested=True)
        retry_activity_upload_deletions_task()
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


def test_processing_claim_and_replay_preserve_accepted_result() -> None:
    competition = _competition()
    player, _ = create_invited_account(
        username="rider", email="rider@example.com", invite_code=competition.invite_code
    )
    gpx = (
        b"<gpx><metadata><type>cycling</type></metadata><trk>"
        b"<trkpt lat='50' lon='14'/><trkpt lat='50.1' lon='14.1'/></trk></gpx>"
    )
    batch = create_batch(player, [("ride.gpx", gpx)], attested=True)
    process_batch(batch.pk)
    process_batch(batch.pk)
    batch.refresh_from_db()
    result = batch.files.get()
    assert batch.status == ActivityUploadBatch.Status.COMPLETED, result.error_detail
    assert batch.accepted_files == 1
    assert batch.duplicate_files == 0
    assert result.status == "accepted", result.error_detail


def test_expired_processing_upload_finalizes_parent_batch(tmp_path: Path) -> None:
    competition = _competition()
    player, _ = create_invited_account(
        username="rider", email="rider@example.com", invite_code=competition.invite_code
    )
    with override_settings(ACTIVITY_UPLOAD_ROOT=tmp_path):
        batch = create_batch(player, [("ride.gpx", b"stale")], attested=True)
    upload = batch.files.get()
    stored_name = upload.content_path.name
    assert stored_name
    stored_path = tmp_path / stored_name
    assert stored_path.is_file()
    ActivityUploadBatch.objects.filter(pk=batch.pk).update(
        status=ActivityUploadBatch.Status.RUNNING
    )
    upload.status = "processing"
    upload.created_at = timezone.now() - timedelta(days=2)
    upload.save(update_fields=("status", "created_at"))
    with override_settings(ACTIVITY_UPLOAD_ROOT=tmp_path):
        cleanup_expired_activity_uploads_task()
    batch.refresh_from_db()
    upload.refresh_from_db()
    assert batch.status == ActivityUploadBatch.Status.FAILED
    assert upload.content is None
    assert not stored_path.exists()


def test_transport_accepts_supported_payload_above_one_megabyte(tmp_path: Path) -> None:
    competition = _competition()
    player, _ = create_invited_account(
        username="rider", email="rider@example.com", invite_code=competition.invite_code
    )
    with override_settings(ACTIVITY_UPLOAD_ROOT=tmp_path):
        batch = create_batch(player, [("ride.gpx", b"x" * (1024 * 1024 + 1))], attested=True)
    assert batch.total_files == 1
    assert batch.files.get().size_bytes == 1024 * 1024 + 1


def test_application_rejects_aggregate_request_over_transport_limit(
    monkeypatch: Any,
) -> None:
    competition = _competition()
    player, _ = create_invited_account(
        username="rider", email="rider@example.com", invite_code=competition.invite_code
    )
    monkeypatch.setattr("apps.accounts.upload_services.MAX_UPLOAD_REQUEST_BYTES", 1024)
    with pytest.raises(UploadError, match="request exceeds"):
        create_batch(player, [("ride.gpx", b"x" * 1025)], attested=True)


@pytest.mark.parametrize("status", [ActivityUpload.Status.QUEUED, ActivityUpload.Status.PROCESSING])
def test_account_deletion_removes_queued_and_processing_payloads(
    tmp_path: Path, status: str
) -> None:
    competition = _competition()
    player, _ = create_invited_account(
        username="rider", email="rider@example.com", invite_code=competition.invite_code
    )
    with override_settings(ACTIVITY_UPLOAD_ROOT=tmp_path):
        batch = create_batch(player, [("ride.gpx", b"private payload")], attested=True)
        upload = batch.files.get()
        upload.status = status
        upload.save(update_fields=("status",))
        storage_key = upload.content_path.name
        assert storage_key is not None
        delete_player(player)
        assert ActivityUploadDeletion.objects.filter(
            storage_key=storage_key, status=ActivityUploadDeletion.Status.PENDING
        ).exists()
        retry_activity_upload_deletions_task()
        assert not (tmp_path / storage_key).exists()
        assert ActivityUploadDeletion.objects.get(storage_key=storage_key).status == (
            ActivityUploadDeletion.Status.DELETED
        )


def test_failed_storage_deletion_is_retryable_and_operator_visible(tmp_path: Path) -> None:
    storage_key = "private/activity_uploads/orphan.bin"
    payload_path = tmp_path / storage_key
    payload_path.parent.mkdir(parents=True)
    payload_path.write_bytes(b"synthetic private bytes")
    deletion = ActivityUploadDeletion.objects.create(storage_key=storage_key)
    with override_settings(ACTIVITY_UPLOAD_ROOT=tmp_path):
        with patch.object(ActivityUpload.content_path.field.storage, "delete", side_effect=OSError):
            assert retry_activity_upload_deletions_task() == {
                "deleted": 0,
                "failed": 1,
                "purged": 0,
            }
        deletion.refresh_from_db()
        assert deletion.status == ActivityUploadDeletion.Status.FAILED
        assert deletion.attempts == 1
        assert deletion.last_error == "OSError"
        ActivityUploadDeletion.objects.filter(pk=deletion.pk).update(next_attempt_at=timezone.now())
        assert retry_activity_upload_deletions_task() == {
            "deleted": 1,
            "failed": 0,
            "purged": 0,
        }
    assert not payload_path.exists()


def test_orphan_reconciliation_queues_only_old_unreferenced_objects(tmp_path: Path) -> None:
    orphan = tmp_path / "private/activity_uploads/orphan.bin"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"synthetic")
    old = timezone.now() - timedelta(hours=2)
    os.utime(orphan, (old.timestamp(), old.timestamp()))
    recent = tmp_path / "private/activity_uploads/recent.bin"
    recent.write_bytes(b"recent")
    with override_settings(ACTIVITY_UPLOAD_ROOT=tmp_path):
        assert reconcile_orphan_activity_uploads_task() == {"queued": 1}
    assert ActivityUploadDeletion.objects.filter(
        storage_key="private/activity_uploads/orphan.bin"
    ).exists()
    assert not ActivityUploadDeletion.objects.filter(
        storage_key="private/activity_uploads/recent.bin"
    ).exists()


def test_worker_race_with_account_deletion_does_not_recreate_activity(tmp_path: Path) -> None:
    competition = _competition()
    player, _ = create_invited_account(
        username="rider", email="rider@example.com", invite_code=competition.invite_code
    )
    with override_settings(ACTIVITY_UPLOAD_ROOT=tmp_path):
        batch = create_batch(player, [("ride.gpx", b"private")], attested=True)

        def delete_while_parsing(
            *_args: Any, **_kwargs: Any
        ) -> tuple[list[tuple[float, float]], datetime, str]:
            delete_player(player)
            return [(14.0, 50.0), (14.1, 50.1)], datetime(2026, 9, 28, tzinfo=UTC), "GPX"

        with patch(
            "apps.accounts.upload_services.parse_activity", side_effect=delete_while_parsing
        ):
            result = process_activity_upload_batch_task(str(batch.pk))
        assert result["status"] == "deleted"
        assert not Player.objects.filter(pk=player.pk).exists()
        assert not ActivityUploadBatch.objects.filter(pk=batch.pk).exists()


def test_restore_command_discards_transient_payloads_and_marks_unfinished_failed(
    tmp_path: Path,
) -> None:
    from django.core.management import call_command

    competition = _competition()
    player, _ = create_invited_account(
        username="rider", email="rider@example.com", invite_code=competition.invite_code
    )
    with override_settings(ACTIVITY_UPLOAD_ROOT=tmp_path):
        batch = create_batch(player, [("ride.gpx", b"private")], attested=True)
        upload = batch.files.get()
        upload.status = ActivityUpload.Status.PROCESSING
        upload.save(update_fields=("status",))
        storage_key = upload.content_path.name
        assert storage_key is not None
        call_command("discard_restored_activity_uploads")
        upload.refresh_from_db()
        assert upload.status == ActivityUpload.Status.FAILED
        assert upload.error_code == "restore_payload_discarded"
        assert not upload.content_path
        retry_activity_upload_deletions_task()
        assert not (tmp_path / storage_key).exists()


@override_settings(PLAYER_ACCOUNTS_ENABLED=True)
def test_upload_endpoints_do_not_disclose_other_players_batches() -> None:
    client = APIClient()
    response = client.get("/api/v1/game/account/uploads/00000000-0000-0000-0000-000000000000/")
    assert response.status_code == 401


@override_settings(PLAYER_ACCOUNTS_ENABLED=True)
def test_authenticated_player_cannot_read_another_players_batch(tmp_path: Path) -> None:
    competition = _competition()
    owner, _ = create_invited_account(
        username="owner-rider", email="owner@example.com", invite_code=competition.invite_code
    )
    other_user = get_user_model().objects.create_user(
        username="other-rider", email="other@example.com", password="password"
    )
    other = Player.objects.create(user=other_user)
    with override_settings(ACTIVITY_UPLOAD_ROOT=tmp_path):
        batch = create_batch(owner, [("ride.gpx", b"private")], attested=True)
    client = APIClient()
    session = client.session
    session["player_id"] = other.pk
    session["player_session_epoch"] = other.session_epoch
    session.save()
    response = client.get(f"/api/v1/game/account/uploads/{batch.pk}/")
    assert response.status_code == 404


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
    assert client.post("/api/v1/game/account/github/link/", {}, format="json").status_code == 200
    assert client.post("/api/v1/game/auth/logout/", {}, format="json").status_code == 204
    assert client.post("/api/v1/game/account/github/link/", {}, format="json").status_code == 401

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
def test_github_link_and_local_logout_are_csrf_protected_and_flush_auth_state() -> None:
    user = get_user_model().objects.create_user(username="rider", password="password")
    player = Player.objects.create(user=user, must_change_password=False)
    client = Client(enforce_csrf_checks=True)
    session = client.session
    session["player_id"] = player.pk
    session["player_session_epoch"] = player.session_epoch
    session["account_invite_code"] = "RIDE-123"
    session["account_authentication_methods"] = [{"method": "player", "provider": "github"}]
    session.save()

    # Link intent creation is a mutation and must not be reachable through GET.
    response = client.get("/api/v1/game/account/github/link/")
    assert response.status_code == 405
    assert "github_link_intent" not in client.session

    response = client.post("/api/v1/game/account/github/link/", {}, content_type="application/json")
    assert response.status_code == 403
    assert "github_link_intent" not in client.session

    session_response = client.get("/api/v1/game/auth/session/")
    assert session_response.status_code == 200
    csrf_token = client.cookies["csrftoken"].value
    response = client.post(
        "/api/v1/game/account/github/link/",
        {},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=csrf_token,
    )
    assert response.status_code == 200
    linked_session = client.session
    assert linked_session["_auth_user_id"] == str(user.pk)
    assert linked_session["player_id"] == player.pk
    assert linked_session["github_link_intent"]
    assert linked_session["github_link_player_id"] == str(player.pk)
    assert linked_session["github_link_epoch"] == player.session_epoch

    csrf_token = response["X-CSRFToken"]
    response = client.post(
        "/api/v1/game/auth/logout/",
        {},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=csrf_token,
    )
    assert response.status_code == 204
    logged_out_session = client.session
    assert "_auth_user_id" not in logged_out_session
    assert "player_id" not in logged_out_session
    assert "player_session_epoch" not in logged_out_session
    assert "account_invite_code" not in logged_out_session
    assert "account_authentication_methods" not in logged_out_session
    assert "github_link_intent" not in logged_out_session
    assert "github_link_player_id" not in logged_out_session
    assert "github_link_epoch" not in logged_out_session


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
