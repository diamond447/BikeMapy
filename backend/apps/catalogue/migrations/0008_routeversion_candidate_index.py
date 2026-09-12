from django.db import migrations, models

INDEX_NAME = "cat_ver_status_distance_idx"


def create_candidate_length_index(apps, schema_editor):
    """Build the production index without holding a table write lock."""

    if schema_editor.connection.vendor == "postgresql":
        # A failed CONCURRENTLY build leaves an invalid index shell behind;
        # IF NOT EXISTS would incorrectly treat that shell as complete.
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(
                "SELECT indexrelid::regclass::text, indisvalid "
                "FROM pg_index JOIN pg_class ON pg_class.oid = indexrelid "
                "WHERE pg_class.relname = %s",
                [INDEX_NAME],
            )
            existing = cursor.fetchone()
        if existing and not existing[1]:
            schema_editor.execute(f"DROP INDEX CONCURRENTLY {INDEX_NAME}")
        elif existing:
            return
        schema_editor.execute(
            f"CREATE INDEX CONCURRENTLY {INDEX_NAME} "
            "ON catalogue_routeversion (technical_status, distance_m)"
        )
    else:
        schema_editor.execute(
            f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} "
            "ON catalogue_routeversion (technical_status, distance_m)"
        )


def drop_candidate_length_index(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}")
    else:
        schema_editor.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("catalogue", "0007_alter_moderationdecision_action")]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(
                    create_candidate_length_index,
                    reverse_code=drop_candidate_length_index,
                )
            ],
            state_operations=[
                migrations.AddIndex(
                    model_name="routeversion",
                    index=models.Index(
                        fields=["technical_status", "distance_m"],
                        name=INDEX_NAME,
                    ),
                ),
            ],
        )
    ]
