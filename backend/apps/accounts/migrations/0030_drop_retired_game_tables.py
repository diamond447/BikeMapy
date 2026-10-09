"""Drop tables left behind by the retired private game module.

The Strava/competition models (``accounts`` migrations 0001-0029) and the
``reference_routes`` app were removed from the codebase; their source is kept
at the ``archive/strava-game`` tag. A fresh database never created these
tables, so every statement is idempotent. Run
``manage.py remove_stale_contenttypes --include-stale-apps`` afterwards to
clear their content types and permissions.
"""

from django.db import migrations

RETIRED_TABLES = (
    "reference_routes_routecompletionevidence",
    "reference_routes_routecompletionmonthly",
    "reference_routes_routecompletionjob",
    "reference_routes_routecompletion",
    "reference_routes_referencerecomputation",
    "reference_routes_referencealterationoffer",
    "reference_routes_referencerouteversion",
    "reference_routes_referenceroute",
    "reference_routes_referenceimport",
    "reference_routes_referencecollection",
    "accounts_capturefaceowner",
    "accounts_captureface",
    "accounts_captureplayerarea",
    "accounts_capturecalculation",
    "accounts_competitionresult",
    "accounts_competitionrecomputation",
    "accounts_competitionsharingconsentaudit",
    "accounts_competitioninviteredemption",
    "accounts_competitionmembership",
    "accounts_stravawebhookevent",
    "accounts_stravaquotareservation",
    "accounts_stravaquotastate",
    "accounts_stravasyncjob",
    "accounts_stravasyncstate",
    "accounts_activityupload",
    "accounts_activityuploadbatch",
    "accounts_activityuploaddeletion",
    "accounts_importedactivity",
    "accounts_playercredential",
    "accounts_playeridentityguard",
    "accounts_playerdeletiontombstone",
    "accounts_revocationjob",
    "accounts_oauthstate",
    "accounts_player",
    "accounts_competition",
)
# Case-insensitive unique indexes that accounts.0025 added to auth_user.
RETIRED_INDEXES = ("accounts_user_username_ci", "accounts_user_email_ci")


def drop_retired_tables(apps, schema_editor):  # type: ignore[no-untyped-def]
    del apps
    connection = schema_editor.connection
    cascade = " CASCADE" if connection.vendor == "postgresql" else ""
    for table in RETIRED_TABLES:
        schema_editor.execute(f"DROP TABLE IF EXISTS {connection.ops.quote_name(table)}{cascade}")
    for index in RETIRED_INDEXES:
        schema_editor.execute(f"DROP INDEX IF EXISTS {connection.ops.quote_name(index)}")
    # Forget the removed migration files so `showmigrations` stays accurate.
    schema_editor.execute(
        "DELETE FROM django_migrations WHERE app = %s OR (app = %s AND name < %s)",
        ("reference_routes", "accounts", "0030"),
    )


class Migration(migrations.Migration):
    dependencies: list[tuple[str, str]] = []

    operations = [migrations.RunPython(drop_retired_tables, migrations.RunPython.noop)]
