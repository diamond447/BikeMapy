"""Logging configuration shared by Django, Gunicorn, and Celery."""

from .observability import JsonFormatter, PrivacyLogFilter

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"privacy": {"()": PrivacyLogFilter}},
    "formatters": {"json": {"()": JsonFormatter}},
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "filters": ["privacy"],
            "formatter": "json",
        }
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "celery": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
