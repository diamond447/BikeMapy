import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from uuid import UUID

import pytest
from django.conf import settings


def test_restore_failure_path_restores_database_and_gpx_pair() -> None:
    script = (Path(__file__).parents[3] / "deploy" / "restore.sh").read_text()
    assert "trap restore_previous_state ERR" in script
    assert "db-before-restore-${BACKUP_ID}-${RESTORE_ATTEMPT_ID}.dump" in script
    assert "gpx-before-restore-${BACKUP_ID}-${RESTORE_ATTEMPT_ID}.tar.gz" in script
    assert "restore_previous_database" in script
    assert "restore_previous_gpx" in script
    assert "rollback_db_ready=1" in script
    assert "rollback_gpx_ready=1" in script
    failure_handler = script[
        script.index("restore_previous_state()") : script.index("trap restore_previous_state")
    ]
    assert failure_handler.index("stop backend worker beat") < failure_handler.index(
        "restore_previous_database"
    )
    assert script.index("before_db_file=") < script.index('"/backup/db-${BACKUP_ID}.dump"')


def test_backup_and_restore_share_the_volume_relative_gpx_layout() -> None:
    root = Path(__file__).parents[3]
    backup = (root / "deploy" / "backup.sh").read_text()
    restore = (root / "deploy" / "restore.sh").read_text()
    drill = (root / "deploy" / "restore-drill.sh").read_text()
    manifest = (root / "deploy" / "write-backup-manifest.sh").read_text()
    validator = (root / "deploy" / "validate-gpx-archive.sh").read_text()
    archive_validator = (root / "scripts" / "validate_gpx_archive.py").read_text()
    workflow = (root / ".github" / "workflows" / "restore-drill.yml").read_text()

    assert 'tar czf "/backup/gpx-${BACKUP_ID}.tar.gz.part" -C /data .' in backup
    assert 'tar xzf "/backup/gpx-\'"$BACKUP_ID"\'.tar.gz" -C /data' in restore
    assert "media/" in backup
    assert "media/" in restore
    assert "validate-gpx-archive.sh" in drill
    assert "media/" in archive_validator
    assert "validate_gpx_archive.py" in validator
    assert "tarfile" in archive_validator
    assert "write-backup-manifest.sh" in backup
    assert "write-backup-manifest.sh" in workflow
    assert "database_sha256" in manifest
    assert "gpx_sha256" in manifest


def test_restore_drill_uses_full_schema_disposable_volume_and_application_checks() -> None:
    root = Path(__file__).parents[3]
    drill = (root / "deploy" / "restore-drill.sh").read_text()
    query = (root / "scripts" / "query_restore_gpx_references.py").read_text()
    workflow = (root / ".github" / "workflows" / "restore-drill.yml").read_text()
    seed = (root / "scripts" / "seed_restore_drill.py").read_text()

    assert 'docker volume create "$GPX_VOLUME"' in drill
    assert 'docker volume rm "$GPX_VOLUME"' in drill
    assert "original_gpx_storage_key" in query
    assert "payload_removed_at__isnull=True" in query
    assert "current_approved_version" in query
    assert "query_restore_gpx_references.py" in drill
    assert "validate_restore_gpx_references.py" in drill
    assert "json.dump" in query
    assert "field-separator" not in drill
    assert "restored_gpx_sha" in drill
    assert "DRILL_GPX_SHA" in drill
    assert "/health/ready/" in drill
    assert "/api/v1/routes/" in drill
    assert "/gpx/" in drill
    assert "python backend/manage.py migrate --noinput" in workflow
    assert "python scripts/seed_restore_drill.py" in workflow
    assert "restore-drill-fixture.gpx" in workflow
    assert "restore-drill-history.gpx" in workflow
    assert "GPX_SHA256" in seed
    assert "original_gpx_storage_key=storage_key" in seed


def test_restore_gpx_validator_accepts_fixture_and_rejects_semantically_invalid_payload(
    tmp_path: Path,
) -> None:
    root = Path(__file__).parents[3]
    validator = root / "scripts" / "validate_restore_gpx.py"
    fixture = root / "deploy" / "restore-drill-fixture.gpx"
    invalid = tmp_path / "invalid.gpx"
    invalid.write_text(
        '<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1"><metadata /></gpx>'
    )
    environment = {**os.environ, "DJANGO_DATABASE_ENGINE": "django.db.backends.sqlite3"}

    valid_result = subprocess.run(
        [sys.executable, str(validator), str(fixture)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    invalid_result = subprocess.run(
        [sys.executable, str(validator), str(invalid)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert valid_result.returncode == 0
    assert "valid" in valid_result.stdout
    assert invalid_result.returncode == 1
    assert "no direct route points" in invalid_result.stderr


def test_restore_gpx_references_validates_all_rows(tmp_path: Path) -> None:
    root = Path(__file__).parents[3]
    validator = root / "scripts" / "validate_restore_gpx_references.py"
    storage_root = tmp_path / "media"
    first = storage_root / "gpx/routes/first.gpx"
    second = storage_root / "gpx/routes/second.gpx"
    first.parent.mkdir(parents=True)
    first.write_bytes((root / "deploy/restore-drill-fixture.gpx").read_bytes())
    second.write_bytes((root / "deploy/restore-drill-history.gpx").read_bytes())
    rows = [
        ["1", "gpx/routes/first.gpx", _sha256(first), "first-route"],
        ["2", "gpx/routes/second.gpx", _sha256(second), "second-route"],
    ]

    result = _run_reference_validator(validator, storage_root, rows)

    assert result.returncode == 0
    assert "version 1" in result.stdout
    assert "version 2" in result.stdout


@pytest.mark.parametrize(
    ("case", "expected_error"),
    [
        ("missing", "is missing"),
        ("checksum", "checksum mismatch"),
        ("semantic", "no direct route points"),
    ],
)
def test_restore_gpx_references_rejects_inconsistent_non_first_rows(
    tmp_path: Path, case: str, expected_error: str
) -> None:
    root = Path(__file__).parents[3]
    validator = root / "scripts" / "validate_restore_gpx_references.py"
    storage_root = tmp_path / "media"
    first = storage_root / "gpx/routes/first.gpx"
    second = storage_root / "gpx/routes/second.gpx"
    first.parent.mkdir(parents=True)
    first.write_bytes((root / "deploy/restore-drill-fixture.gpx").read_bytes())
    if case == "checksum":
        second.write_bytes((root / "deploy/restore-drill-history.gpx").read_bytes())
        expected_checksum = "0" * 64
    elif case == "semantic":
        second.write_text(
            '<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1"><metadata /></gpx>'
        )
        expected_checksum = _sha256(second)
    else:
        expected_checksum = "0" * 64
    rows = [
        ["1", "gpx/routes/first.gpx", _sha256(first), "first-route"],
        ["2", "gpx/routes/second.gpx", expected_checksum, "second-route"],
    ]

    result = _run_reference_validator(validator, storage_root, rows)

    assert result.returncode == 1
    assert expected_error in result.stderr


def test_restore_gpx_reference_json_framing_preserves_tabs_and_newlines(tmp_path: Path) -> None:
    root = Path(__file__).parents[3]
    validator = root / "scripts" / "validate_restore_gpx_references.py"
    storage_root = tmp_path / "media"
    key = "gpx/routes/control\tcharacter\nname.gpx"
    payload = storage_root / Path(*key.split("/"))
    payload.parent.mkdir(parents=True)
    payload.write_bytes((root / "deploy/restore-drill-history.gpx").read_bytes())

    result = _run_reference_validator(
        validator,
        storage_root,
        [["1", key, _sha256(payload), "route-with-controls"]],
    )

    assert result.returncode == 0
    assert "valid" in result.stdout


@pytest.mark.django_db
@pytest.mark.skipif(
    not settings.DATABASES["default"]["ENGINE"].endswith("sqlite3"),
    reason="The query regression uses the isolated SQLite backend in the backend CI job",
)
def test_restore_reference_query_returns_all_rows_and_current_approved_version() -> None:
    root = Path(__file__).parents[3]
    spec = importlib.util.spec_from_file_location(
        "query_restore_gpx_references", root / "scripts/query_restore_gpx_references.py"
    )
    assert spec is not None and spec.loader is not None
    query = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(query)

    from apps.catalogue.models import ProcessingStatus, Route, RouteSource, RouteVersion

    route = Route.objects.create(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        display_title="Restore query regression",
    )
    source = RouteSource.objects.create(
        route=route, mapy_url="https://mapy.com/s/restore-query-regression"
    )
    historical = RouteVersion.objects.create(
        source=source,
        version_number=1,
        checksum="historical-checksum",
        original_gpx_storage_key="gpx/routes/historical\tkey.gpx",
        technical_status=ProcessingStatus.VALID,
    )
    approved = RouteVersion.objects.create(
        source=source,
        version_number=2,
        checksum="approved-checksum",
        original_gpx_storage_key="gpx/routes/approved\nkey.gpx",
        technical_status=ProcessingStatus.VALID,
    )
    route.current_approved_version = approved
    route.save(update_fields=["current_approved_version", "updated_at"])
    second_route = Route.objects.create(
        id=UUID("00000000-0000-0000-0000-000000000002"),
        display_title="Restore query second route",
    )
    second_source = RouteSource.objects.create(
        route=second_route, mapy_url="https://mapy.com/s/restore-query-second"
    )
    second_approved = RouteVersion.objects.create(
        source=second_source,
        version_number=1,
        checksum="second-approved-checksum",
        original_gpx_storage_key="gpx/routes/second-approved.gpx",
        technical_status=ProcessingStatus.VALID,
    )
    second_route.current_approved_version = second_approved
    second_route.save(update_fields=["current_approved_version", "updated_at"])

    references = query.live_references()
    approved_references = query.approved_references()
    representative = query.representative_approved_reference()

    assert [row[0] for row in references] == [
        str(historical.pk),
        str(approved.pk),
        str(second_approved.pk),
    ]
    assert approved_references == [
        [str(route.pk), "gpx/routes/approved\nkey.gpx", "approved-checksum"],
        [str(second_route.pk), "gpx/routes/second-approved.gpx", "second-approved-checksum"],
    ]
    assert representative == approved_references[0]


@pytest.mark.django_db
@pytest.mark.skipif(
    not settings.DATABASES["default"]["ENGINE"].endswith("sqlite3"),
    reason="The query-to-validator bridge uses the isolated SQLite backend in the backend CI job",
)
def test_restore_query_cli_json_bridges_to_validator(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = Path(__file__).parents[3]
    query_path = root / "scripts/query_restore_gpx_references.py"
    validator = root / "scripts/validate_restore_gpx_references.py"
    spec = importlib.util.spec_from_file_location("query_restore_gpx_references_bridge", query_path)
    assert spec is not None and spec.loader is not None
    query = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(query)

    from apps.catalogue.models import ProcessingStatus, Route, RouteSource, RouteVersion

    key = "gpx/routes/bridge\tname\npart.gpx"
    payload = tmp_path / Path(*key.split("/"))
    payload.parent.mkdir(parents=True)
    payload.write_bytes((root / "deploy/restore-drill-fixture.gpx").read_bytes())
    route = Route.objects.create(display_title="Restore query bridge")
    source = RouteSource.objects.create(
        route=route, mapy_url="https://mapy.com/s/restore-query-bridge"
    )
    RouteVersion.objects.create(
        source=source,
        version_number=1,
        checksum=_sha256(payload),
        original_gpx_storage_key=key,
        technical_status=ProcessingStatus.VALID,
    )

    assert query.main([str(query_path)]) == 0
    query_json = capsys.readouterr().out
    environment = {**os.environ, "DJANGO_DATABASE_ENGINE": "django.db.backends.sqlite3"}
    result = subprocess.run(
        [sys.executable, str(validator), "--storage-root", str(tmp_path)],
        check=False,
        input=query_json,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0
    assert "valid" in result.stdout


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_reference_validator(
    validator: Path, storage_root: Path, rows: list[list[str]]
) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "DJANGO_DATABASE_ENGINE": "django.db.backends.sqlite3"}
    return subprocess.run(
        [sys.executable, str(validator), "--storage-root", str(storage_root)],
        check=False,
        input=json.dumps(rows),
        capture_output=True,
        text=True,
        env=environment,
    )


def test_gpx_archive_validation_rejects_corrupt_and_legacy_archives(tmp_path: Path) -> None:
    root = Path(__file__).parents[3]
    validator = root / "deploy" / "validate-gpx-archive.sh"

    valid = tmp_path / "valid.tar.gz"
    with tarfile.open(valid, "w:gz") as archive:
        payload = tmp_path / "sample.gpx"
        payload.write_text("sample gpx payload\n")
        archive.add(payload, arcname="media/gpx/routes/sample.gpx")

        control_payload = tmp_path / "control.gpx"
        control_payload.write_text("control gpx payload\n")
        archive.add(control_payload, arcname="media/gpx/routes/control\tname\npart.gpx")

    legacy = tmp_path / "legacy.tar.gz"
    with tarfile.open(legacy, "w:gz") as archive:
        archive.add(payload, arcname="data/sample.gpx")

    corrupt = tmp_path / "corrupt.tar.gz"
    corrupt.write_bytes(b"not a tar archive")

    symlink = tmp_path / "symlink.tar.gz"
    with tarfile.open(symlink, "w:gz") as archive:
        link = tarfile.TarInfo("media/gpx/routes/link.gpx")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)

    traversal = tmp_path / "traversal.tar.gz"
    with tarfile.open(traversal, "w:gz") as archive:
        archive.add(payload, arcname="media/../outside.gpx")

    def validate(archive: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(validator), str(archive)],
            check=False,
            capture_output=True,
            text=True,
        )

    assert validate(valid).returncode == 0
    assert validate(legacy).returncode != 0
    assert "outside media/" in validate(legacy).stderr
    assert validate(corrupt).returncode != 0
    assert validate(symlink).returncode != 0
    assert validate(traversal).returncode != 0
