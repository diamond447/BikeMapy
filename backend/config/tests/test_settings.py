from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_blank_webhook_subscription_id_allows_django_startup() -> None:
    root = Path(__file__).parents[3]
    environment = os.environ.copy()
    environment.update(
        {
            "DJANGO_DATABASE_ENGINE": "django.db.backends.sqlite3",
            "STRAVA_WEBHOOK_SUBSCRIPTION_ID": "",
        }
    )
    result = subprocess.run(
        [sys.executable, "backend/manage.py", "check"],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
