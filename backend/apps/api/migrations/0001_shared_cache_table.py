"""Create the PostgreSQL cache table used by lean single-host deployments.

The table is created in every environment so switching a deployment to
``BACKGROUND_JOBS_ENABLED=false`` never requires a separate setup step.
"""

from django.core.management import call_command
from django.db import migrations

CACHE_TABLE = "bikemapy_cache"


def create_cache_table(apps, schema_editor):  # type: ignore[no-untyped-def]
    del apps
    call_command(
        "createcachetable",
        CACHE_TABLE,
        database=schema_editor.connection.alias,
        verbosity=0,
    )


def drop_cache_table(apps, schema_editor):  # type: ignore[no-untyped-def]
    del apps
    table = schema_editor.connection.ops.quote_name(CACHE_TABLE)
    schema_editor.execute(f"DROP TABLE IF EXISTS {table}")


class Migration(migrations.Migration):
    dependencies: list[tuple[str, str]] = []

    operations = [migrations.RunPython(create_cache_table, drop_cache_table)]
