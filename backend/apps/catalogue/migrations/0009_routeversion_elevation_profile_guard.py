from django.db import migrations


def _sqlite_guard_sql(*, include_elevation_profile: bool) -> str:
    profile_clause = (
        "\n                  OR NEW.elevation_profile IS NOT OLD.elevation_profile"
        if include_elevation_profile
        else ""
    )
    return f"""
        CREATE TRIGGER catalogue_route_version_guard
        BEFORE UPDATE ON catalogue_routeversion
        WHEN NEW.source_id IS NOT OLD.source_id
          OR NEW.version_number IS NOT OLD.version_number
          OR NEW.checksum IS NOT OLD.checksum
          OR NEW.normalized_geometry IS NOT OLD.normalized_geometry
          OR NEW.simplified_geometry IS NOT OLD.simplified_geometry
          OR NEW.distance_m IS NOT OLD.distance_m
          OR NEW.ascent_m IS NOT OLD.ascent_m
          OR NEW.descent_m IS NOT OLD.descent_m
          OR NEW.loop_status IS NOT OLD.loop_status
          OR NEW.technical_status IS NOT OLD.technical_status
          OR NEW.validation_error IS NOT OLD.validation_error
          OR NEW.created_at IS NOT OLD.created_at{profile_clause}
          OR (
              NEW.approved_at IS NOT OLD.approved_at
              AND NOT (
                  OLD.approved_at IS NULL
                  AND NEW.approved_at IS NOT NULL
              )
          )
          OR (
              NOT (
                  OLD.payload_removed_at IS NULL
                  AND NEW.payload_removed_at IS NOT NULL
                  AND OLD.original_gpx_storage_key != ''
                  AND NEW.original_gpx_storage_key = ''
                  AND NEW.payload_removal_reason != ''
              )
              AND (
                  NEW.original_gpx_storage_key IS NOT OLD.original_gpx_storage_key
                  OR NEW.payload_removed_at IS NOT OLD.payload_removed_at
                  OR NEW.payload_removal_reason IS NOT OLD.payload_removal_reason
              )
          )
        BEGIN SELECT RAISE(ABORT, 'historical route version content is immutable'); END;
    """


def _postgres_guard_sql(*, include_elevation_profile: bool) -> str:
    profile_clause = (
        "\n                   OR NEW.elevation_profile IS DISTINCT FROM OLD.elevation_profile"
        if include_elevation_profile
        else ""
    )
    return f"""
        CREATE OR REPLACE FUNCTION catalogue_route_version_guard()
        RETURNS trigger LANGUAGE plpgsql AS $guard$
        BEGIN
            IF NEW.source_id IS DISTINCT FROM OLD.source_id
               OR NEW.version_number IS DISTINCT FROM OLD.version_number
               OR NEW.checksum IS DISTINCT FROM OLD.checksum
               OR NEW.normalized_geometry IS DISTINCT FROM OLD.normalized_geometry
               OR NEW.simplified_geometry IS DISTINCT FROM OLD.simplified_geometry
               OR NEW.distance_m IS DISTINCT FROM OLD.distance_m
               OR NEW.ascent_m IS DISTINCT FROM OLD.ascent_m
               OR NEW.descent_m IS DISTINCT FROM OLD.descent_m
               OR NEW.loop_status IS DISTINCT FROM OLD.loop_status
               OR NEW.technical_status IS DISTINCT FROM OLD.technical_status
               OR NEW.validation_error IS DISTINCT FROM OLD.validation_error
               OR NEW.created_at IS DISTINCT FROM OLD.created_at{profile_clause}
               OR (
                   NEW.approved_at IS DISTINCT FROM OLD.approved_at
                   AND NOT (
                       OLD.approved_at IS NULL
                       AND NEW.approved_at IS NOT NULL
                   )
               )
            THEN
                RAISE EXCEPTION 'historical route version content is immutable';
            END IF;
            IF NOT (
                   OLD.payload_removed_at IS NULL
                   AND NEW.payload_removed_at IS NOT NULL
                   AND OLD.original_gpx_storage_key <> ''
                   AND NEW.original_gpx_storage_key = ''
                   AND NEW.payload_removal_reason <> ''
               )
               AND (
                   NEW.original_gpx_storage_key IS DISTINCT FROM OLD.original_gpx_storage_key
                   OR NEW.payload_removed_at IS DISTINCT FROM OLD.payload_removed_at
                   OR NEW.payload_removal_reason IS DISTINCT FROM OLD.payload_removal_reason
               )
            THEN
                RAISE EXCEPTION 'route version payload removal metadata is immutable '
                                'except removal';
            END IF;
            RETURN NEW;
        END;
        $guard$;
    """


def _set_database_guard(schema_editor, *, include_elevation_profile: bool) -> None:
    vendor = schema_editor.connection.vendor
    if vendor == "sqlite":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute("DROP TRIGGER IF EXISTS catalogue_route_version_guard")
            cursor.execute(_sqlite_guard_sql(include_elevation_profile=include_elevation_profile))
    elif vendor == "postgresql":
        schema_editor.execute(
            _postgres_guard_sql(include_elevation_profile=include_elevation_profile)
        )


def install_elevation_profile_guard(apps, schema_editor):
    _set_database_guard(schema_editor, include_elevation_profile=True)


def restore_previous_guard(apps, schema_editor):
    _set_database_guard(schema_editor, include_elevation_profile=False)


class Migration(migrations.Migration):
    dependencies = [("catalogue", "0008_routeversion_candidate_index")]

    operations = [
        migrations.RunPython(install_elevation_profile_guard, restore_previous_guard),
    ]
