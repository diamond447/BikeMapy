import fcntl
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import time
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


def test_restore_runtime_failure_restores_transient_upload_and_gpx_volumes(
    tmp_path: Path,
) -> None:
    root = Path(__file__).parents[3]
    backup = tmp_path / "backup"
    backup.mkdir()
    gpx_volume = tmp_path / "gpx-volume"
    upload_volume = tmp_path / "upload-volume"
    gpx_file = gpx_volume / "media/gpx/routes/route.gpx"
    upload_file = upload_volume / "upload.bin"
    gpx_file.parent.mkdir(parents=True)
    upload_volume.mkdir(parents=True)
    gpx_file.write_bytes(b"original durable GPX")
    upload_file.write_bytes(b"original transient upload")

    with tarfile.open(backup / "gpx-target.tar.gz", "w:gz") as archive:
        target = tmp_path / "target.gpx"
        target.write_bytes(b"restored durable GPX")
        archive.add(target, arcname="media/gpx/routes/route.gpx")
    db_dump = backup / "db-target.dump"
    db_dump.write_bytes(b"target database")
    manifest = backup / "manifest-target.json"
    manifest.write_text(
        json.dumps(
            {
                "database_sha256": _sha256(db_dump),
                "gpx_sha256": _sha256(backup / "gpx-target.tar.gz"),
            }
        )
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        """#!/usr/bin/env python3
import os
import subprocess
import sys
from pathlib import Path

args = sys.argv[1:]
if args[:1] == ["volume"]:
    rollback_path = Path(os.environ["FAKE_ROLLBACK_PATH"])
    if args[1] == "create":
        rollback_path.mkdir(parents=True, exist_ok=True)
        raise SystemExit(0)
    if args[1] == "inspect":
        raise SystemExit(0 if rollback_path.exists() else 1)
    if args[1] == "rm":
        import shutil
        shutil.rmtree(rollback_path, ignore_errors=True)
        raise SystemExit(0)
    if args[1] == "ls":
        if rollback_path.exists():
            print(os.environ["ACTIVITY_UPLOAD_ROLLBACK_VOLUME"])
        raise SystemExit(0)
if args[0] != "run":
    raise SystemExit("unexpected docker invocation: " + repr(args))
mounts = {}
index = 1
while index < len(args):
    if args[index] == "--rm":
        index += 1
    elif args[index] == "-v":
        volume, mount = args[index + 1].split(":", 1)
        mount_path = mount.removesuffix(":ro")
        key = {
            os.environ["GPX_VOLUME"]: "FAKE_GPX_PATH",
            os.environ["ACTIVITY_UPLOAD_VOLUME"]: "FAKE_UPLOAD_PATH",
            os.environ["ACTIVITY_UPLOAD_ROLLBACK_VOLUME"]: "FAKE_ROLLBACK_PATH",
        }.get(volume)
        mounts[mount_path] = os.environ[key] if key else volume
        index += 2
    else:
        break
image = args[index]
command = args[index + 1:]
for mount_path, host_path in sorted(mounts.items(), key=lambda item: -len(item[0])):
    command = [part.replace(mount_path, host_path) for part in command]
if not command:
    raise SystemExit("missing container command for " + image)
raise SystemExit(subprocess.run(command, check=False).returncode)
"""
    )
    docker.chmod(0o755)

    compose = bin_dir / "fake-compose"
    compose.write_text(
        """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

args = sys.argv[1:]
if args[0] == "exec" and "pg_dump" in args:
    target = next(part.split("=", 1)[1] for part in args if part.startswith("--file="))
    Path(os.environ["BACKUP_DIR"], Path(target).name).write_bytes(b"pre-restore database")
elif args[0] == "up":
    raise SystemExit(17)
raise SystemExit(0)
"""
    )
    compose.chmod(0o755)

    environment = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "BACKUP_DIR": str(backup),
        "COMPOSE": str(compose),
        "ACTIVITY_UPLOAD_ROLLBACK_LOCK_FILE": str(tmp_path / "restore-upload.lock"),
        "GPX_VOLUME": "test-gpx",
        "ACTIVITY_UPLOAD_VOLUME": "test-uploads",
        "ACTIVITY_UPLOAD_ROLLBACK_VOLUME": "bikemapy-restore-upload-test-attempt",
        "FAKE_GPX_PATH": str(gpx_volume),
        "FAKE_UPLOAD_PATH": str(upload_volume),
        "FAKE_ROLLBACK_PATH": str(tmp_path / "rollback-volume"),
        "RESTORE_ATTEMPT_ID": "test-attempt",
    }
    result = subprocess.run(
        ["bash", str(root / "deploy/restore.sh"), "target"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode != 0
    assert "Restore failed" in result.stderr
    assert gpx_file.read_bytes() == b"original durable GPX"
    assert upload_file.read_bytes() == b"original transient upload"


def test_abandoned_upload_rollback_volume_expires_with_retry_and_restore_lock(
    tmp_path: Path,
) -> None:
    root = Path(__file__).parents[3]
    state_path = tmp_path / "docker-volumes.json"
    stale_volume = "bikemapy-restore-upload-crashed-attempt"
    state_path.write_text(
        json.dumps(
            {
                "volumes": {stale_volume: {"created_at": int(time.time()) - 7200}},
                "rm_failures": 1,
            }
        )
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["FAKE_DOCKER_STATE"])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
if args[1] == "ls":
    print("\\n".join(state["volumes"]))
elif args[1] == "inspect":
    metadata = state["volumes"].get(args[-1])
    if metadata is None:
        raise SystemExit(1)
    print(metadata["created_at"])
elif args[1] == "rm":
    if state["rm_failures"]:
        state["rm_failures"] -= 1
        state_path.write_text(json.dumps(state))
        print("simulated busy volume", file=sys.stderr)
        raise SystemExit(1)
    state["volumes"].pop(args[-1], None)
    state_path.write_text(json.dumps(state))
else:
    raise SystemExit("unexpected docker invocation: " + repr(args))
"""
    )
    docker.chmod(0o755)
    backup_dir = tmp_path / "backup"
    lock_path = tmp_path / "restore-upload.lock"
    environment = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "BACKUP_DIR": str(backup_dir),
        "FAKE_DOCKER_STATE": str(state_path),
        "ACTIVITY_UPLOAD_ROLLBACK_TTL_SECONDS": "3600",
        "ACTIVITY_UPLOAD_ROLLBACK_LOCK_FILE": str(lock_path),
    }
    helper = root / "deploy/reconcile-restore-upload-rollbacks.sh"
    first = subprocess.run(
        ["bash", str(helper)], check=False, capture_output=True, text=True, env=environment
    )
    assert first.returncode == 1
    assert "could not remove expired" in first.stderr
    assert stale_volume in json.loads(state_path.read_text())["volumes"]

    with lock_path.open("w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        retry = subprocess.Popen(
            ["bash", str(helper)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        time.sleep(0.2)
        assert retry.poll() is None
        assert stale_volume in json.loads(state_path.read_text())["volumes"]
        fcntl.flock(lock_file, fcntl.LOCK_UN)
    retry_stdout, retry_stderr = retry.communicate(timeout=5)
    assert retry.returncode == 0, retry_stderr
    assert stale_volume not in json.loads(state_path.read_text())["volumes"]
    assert "Expiring abandoned" in retry_stdout


def test_backup_and_restore_share_the_volume_relative_gpx_layout() -> None:
    root = Path(__file__).parents[3]
    backup = (root / "deploy" / "backup.sh").read_text()
    restore = (root / "deploy" / "restore.sh").read_text()
    drill = (root / "deploy" / "restore-drill.sh").read_text()
    manifest = (root / "deploy" / "write-backup-manifest.sh").read_text()
    validator = (root / "deploy" / "validate-gpx-archive.sh").read_text()
    archive_validator = (root / "scripts" / "validate_gpx_archive.py").read_text()
    workflow = (root / ".github" / "workflows" / "restore-drill.yml").read_text()

    assert 'tar czf "/backup/gpx-${BACKUP_ID}.tar.gz.part"' in backup
    assert "-C /data ." in backup
    assert 'tar xzf "/backup/gpx-\'"$BACKUP_ID"\'.tar.gz"' in restore
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
    assert "--exclude=./media/private/activity_uploads" in backup
    assert "discard_restored_activity_uploads" in restore
    assert "ACTIVITY_UPLOAD_VOLUME" in restore


def test_upload_proxy_and_compose_keep_transport_and_storage_boundaries_explicit() -> None:
    root = Path(__file__).parents[3]
    nginx = (root / "deploy" / "nginx.conf").read_text()
    compose = (root / "deploy" / "compose.production.yml").read_text()
    assert "client_max_body_size 95m;" in nginx
    assert "client_body_timeout 60s;" in nginx
    assert "activity_upload_data:/app/private-uploads" in compose
    assert "DJANGO_ACTIVITY_UPLOAD_ROOT: /app/private-uploads" in compose
    # The durable backup volume is GPX-only and the edge proxy never mounts
    # transient raw activity payloads.
    proxy = compose.split("\n  proxy:\n", 1)[1].split("\nvolumes:\n", 1)[0]
    assert "activity_upload_data" not in proxy
    database = compose.split("\n  db:\n", 1)[1].split("\n  redis:\n", 1)[0]
    assert "activity_upload_data" not in database


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


def test_old_gpx_archive_validates_but_restore_skips_legacy_transient_upload(
    tmp_path: Path,
) -> None:
    root = Path(__file__).parents[3]
    validator = root / "scripts" / "validate_gpx_archive.py"
    archive_path = tmp_path / "legacy-private-upload.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        payload = b"synthetic private upload"
        member = tarfile.TarInfo("./media/private/activity_uploads/upload.bin")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
        durable_payload = b"durable route"
        route_member = tarfile.TarInfo("./media/gpx/routes/route.gpx")
        route_member.size = len(durable_payload)
        archive.addfile(route_member, io.BytesIO(durable_payload))
    result = subprocess.run(
        [sys.executable, str(validator), str(archive_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    restored_root = tmp_path / "restored"
    restored_root.mkdir()
    extraction = subprocess.run(
        [
            "tar",
            "xzf",
            str(archive_path),
            "--exclude=./media/private/activity_uploads",
            "-C",
            str(restored_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert extraction.returncode == 0, extraction.stderr
    assert (restored_root / "media/gpx/routes/route.gpx").read_bytes() == durable_payload
    assert not (restored_root / "media/private/activity_uploads/upload.bin").exists()


def test_backup_tar_exclusion_keeps_raw_uploads_out_of_created_archive(tmp_path: Path) -> None:
    storage_root = tmp_path / "storage"
    durable = storage_root / "media/gpx/routes/route.gpx"
    transient = storage_root / "media/private/activity_uploads/raw.bin"
    durable.parent.mkdir(parents=True)
    transient.parent.mkdir(parents=True)
    durable.write_bytes(b"durable route")
    transient.write_bytes(b"transient raw activity")
    archive_path = tmp_path / "gpx.tar.gz"
    result = subprocess.run(
        [
            "tar",
            "czf",
            str(archive_path),
            "--exclude=./media/private/activity_uploads",
            "-C",
            str(storage_root),
            ".",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    with tarfile.open(archive_path, "r:gz") as archive:
        names = archive.getnames()
    assert any(name.endswith("media/gpx/routes/route.gpx") for name in names)
    assert not any("activity_uploads" in name for name in names)


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
    second_payload = tmp_path / "gpx/routes/bridge-second.gpx"
    second_payload.parent.mkdir(parents=True, exist_ok=True)
    second_payload.write_bytes((root / "deploy/restore-drill-history.gpx").read_bytes())
    second_route = Route.objects.create(display_title="Restore query bridge second")
    second_source = RouteSource.objects.create(
        route=second_route, mapy_url="https://mapy.com/s/restore-query-bridge-second"
    )
    RouteVersion.objects.create(
        source=second_source,
        version_number=1,
        checksum=_sha256(second_payload),
        original_gpx_storage_key="gpx/routes/bridge-second.gpx",
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
    assert result.stdout.count("valid") == 2


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
    truncated_archives: list[Path] = []
    valid_bytes = valid.read_bytes()
    for removed_bytes in (8, 20):
        truncated = tmp_path / f"truncated-{removed_bytes}.tar.gz"
        truncated.write_bytes(valid_bytes[:-removed_bytes])
        truncated_archives.append(truncated)

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
    assert all(validate(archive).returncode != 0 for archive in truncated_archives)
    assert validate(symlink).returncode != 0
    assert validate(traversal).returncode != 0
