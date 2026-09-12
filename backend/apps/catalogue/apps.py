from django.apps import AppConfig


class CatalogueConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.catalogue"
    label = "catalogue"

    def ready(self) -> None:
        from . import signals  # noqa: F401
