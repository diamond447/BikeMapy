import os
import subprocess
import sys
import tarfile
from pathlib import Path


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
    workflow = (root / ".github" / "workflows" / "restore-drill.yml").read_text()

    assert 'tar czf "/backup/gpx-${BACKUP_ID}.tar.gz.part" -C /data .' in backup
    assert 'tar xzf "/backup/gpx-\'"$BACKUP_ID"\'.tar.gz" -C /data' in restore
    assert "media/" in backup
    assert "media/" in restore
    assert "validate-gpx-archive.sh" in drill
    assert "media/" in validator
    assert "write-backup-manifest.sh" in backup
    assert "write-backup-manifest.sh" in workflow
    assert "database_sha256" in manifest
    assert "gpx_sha256" in manifest


def test_restore_drill_uses_full_schema_disposable_volume_and_application_checks() -> None:
    root = Path(__file__).parents[3]
    drill = (root / "deploy" / "restore-drill.sh").read_text()
    workflow = (root / ".github" / "workflows" / "restore-drill.yml").read_text()
    seed = (root / "scripts" / "seed_restore_drill.py").read_text()

    assert 'docker volume create "$GPX_VOLUME"' in drill
    assert 'docker volume rm "$GPX_VOLUME"' in drill
    assert "original_gpx_storage_key" in drill
    assert "restored_gpx_sha" in drill
    assert "DRILL_GPX_SHA" in drill
    assert "/health/ready/" in drill
    assert "/api/v1/routes/" in drill
    assert "/gpx/" in drill
    assert "python backend/manage.py migrate --noinput" in workflow
    assert "python scripts/seed_restore_drill.py" in workflow
    assert "restore-drill-fixture.gpx" in workflow
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


def test_gpx_archive_validation_rejects_corrupt_and_legacy_archives(tmp_path: Path) -> None:
    root = Path(__file__).parents[3]
    validator = root / "deploy" / "validate-gpx-archive.sh"
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        """#!/usr/bin/env bash
set -Eeuo pipefail
while (($#)); do
  case "$1" in
    -v) archive="${2%%:*}"; shift 2 ;;
    sh) shift; test "$1" = -c; command="$2"; break ;;
    *) shift ;;
  esac
done
command="${command//\\/backup\\/input.tar.gz/$archive}"
bash -c "$command"
"""
    )
    fake_docker.chmod(0o755)
    environment = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}

    valid = tmp_path / "valid.tar.gz"
    with tarfile.open(valid, "w:gz") as archive:
        payload = tmp_path / "sample.gpx"
        payload.write_text("sample gpx payload\n")
        archive.add(payload, arcname="media/gpx/routes/sample.gpx")

    legacy = tmp_path / "legacy.tar.gz"
    with tarfile.open(legacy, "w:gz") as archive:
        archive.add(payload, arcname="data/sample.gpx")

    corrupt = tmp_path / "corrupt.tar.gz"
    corrupt.write_bytes(b"not a tar archive")

    def validate(archive: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(validator), str(archive)],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

    assert validate(valid).returncode == 0
    assert validate(legacy).returncode != 0
    assert "outside media/" in validate(legacy).stderr
    assert validate(corrupt).returncode != 0
