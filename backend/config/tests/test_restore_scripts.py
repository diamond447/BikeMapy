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
