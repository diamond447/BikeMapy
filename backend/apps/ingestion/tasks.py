"""Background-task entry points.

Domain tasks will be added here as the ingestion pipeline is implemented. The
small health task provides a deterministic worker smoke test for local setup.
"""

from celery import shared_task  # type: ignore[import-untyped]


@shared_task(name="bikemapy.ingestion.worker_smoke")  # type: ignore[untyped-decorator]
def worker_smoke() -> str:
    """Return a value that confirms a Celery worker can execute tasks."""

    return "worker-ready"
