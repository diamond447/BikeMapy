from __future__ import annotations

import os
import subprocess
import sys
from io import StringIO
from unittest.mock import patch

import pytest
from django.conf import settings
from django.core.cache import caches
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from config.celery import app

pytestmark = pytest.mark.django_db


def test_disabled_background_jobs_use_database_cache_and_inline_tasks() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from config import settings; "
                "cache = settings.CACHES['default']; "
                "assert cache['BACKEND'] == 'django.core.cache.backends.db.DatabaseCache'; "
                "assert cache['LOCATION'] == settings.DATABASE_CACHE_TABLE; "
                "assert cache['OPTIONS']['MAX_ENTRIES'] >= 10000; "
                "assert settings.CELERY_TASK_ALWAYS_EAGER is True"
            ),
        ],
        env={
            **os.environ,
            "BACKGROUND_JOBS_ENABLED": "false",
            # Lean mode must ignore a leftover Redis configuration.
            "DJANGO_CACHE_URL": "redis://redis:6379/1",
            "CELERY_TASK_ALWAYS_EAGER": "false",
            "DJANGO_DATABASE_ENGINE": "django.db.backends.sqlite3",
            "PYTHONPATH": "backend",
        },
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_migrated_database_provides_the_lean_cache_table() -> None:
    lean_cache = {
        "default": {
            "BACKEND": "django.core.cache.backends.db.DatabaseCache",
            "LOCATION": settings.DATABASE_CACHE_TABLE,
        }
    }
    with override_settings(CACHES=lean_cache):
        cache = caches.create_connection("default")
        cache.set("health:ready", "ok", timeout=10)
        assert cache.get("health:ready") == "ok"


def test_run_periodic_tasks_runs_the_beat_schedule_without_the_crawl() -> None:
    out = StringIO()
    call_command("run_periodic_tasks", stdout=out)

    lines = out.getvalue().splitlines()
    scheduled = {entry["task"] for entry in settings.CELERY_BEAT_SCHEDULE.values()}
    crawl = "bikemapy.ingestion.incremental_bikeforum_crawl"
    assert {line.removesuffix(": ok") for line in lines} == scheduled - {crawl}


def test_run_periodic_tasks_reports_failed_tasks() -> None:
    app.loader.import_default_modules()
    task = app.tasks["bikemapy.reports.retain_closed_reports"]
    err = StringIO()
    with patch.object(task, "run", side_effect=RuntimeError("boom")):
        with pytest.raises(CommandError, match="1 periodic task"):
            call_command("run_periodic_tasks", stdout=StringIO(), stderr=err)
    assert "bikemapy.reports.retain_closed_reports: failed (RuntimeError)" in err.getvalue()
