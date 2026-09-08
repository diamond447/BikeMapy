"""Gunicorn logger that uses the same privacy-safe JSON formatter as Django."""

from gunicorn.glogging import Logger  # type: ignore[import-untyped]

from .observability import JsonFormatter, PrivacyLogFilter


class JsonGunicornLogger(Logger):  # type: ignore[misc]
    """Keep startup, access, and worker error records structured."""

    def setup(self, cfg: object) -> None:
        super().setup(cfg)
        formatter = JsonFormatter()
        for logger in (self.error_log, self.access_log):
            for handler in logger.handlers:
                handler.setFormatter(formatter)
                handler.addFilter(PrivacyLogFilter())
