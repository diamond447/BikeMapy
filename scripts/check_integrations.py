#!/usr/bin/env python3
"""Verify the real PostGIS connection and Redis-delivered Celery task path."""

import django


def main() -> None:
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()

    from django.db import connection

    from apps.ingestion.tasks import worker_smoke

    with connection.cursor() as cursor:
        cursor.execute("SELECT PostGIS_Version()")
        postgis_version = cursor.fetchone()[0]
    if not postgis_version:
        raise RuntimeError("PostGIS extension is not enabled")
    print(f"PostGIS ready: {postgis_version}")

    result = worker_smoke.delay()
    if result.get(timeout=30, propagate=True) != "worker-ready":
        raise RuntimeError("Celery worker returned an unexpected smoke-test result")
    print("Celery/Redis ready: worker-ready")


if __name__ == "__main__":
    main()
