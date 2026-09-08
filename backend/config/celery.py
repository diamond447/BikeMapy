import os

from celery import Celery  # type: ignore[import-untyped]
from celery.signals import (  # type: ignore[import-untyped]
    after_setup_logger,
    after_setup_task_logger,
)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
app = Celery("bikemapy")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


def _configure_json_logging(logger: object, **kwargs: object) -> None:
    del kwargs
    for handler in getattr(logger, "handlers", []):
        from .observability import JsonFormatter, PrivacyLogFilter

        handler.setFormatter(JsonFormatter())
        handler.addFilter(PrivacyLogFilter())


after_setup_logger.connect(_configure_json_logging)
after_setup_task_logger.connect(_configure_json_logging)
