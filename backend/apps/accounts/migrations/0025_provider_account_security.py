from django.db import migrations, models


def collapse_legacy_redemptions(apps, schema_editor):
    Redemption = apps.get_model("accounts", "CompetitionInviteRedemption")
    seen = set()
    duplicate_ids = []
    for redemption in Redemption.objects.order_by("competition_id", "code_digest", "pk"):
        key = (redemption.competition_id, redemption.code_digest)
        if key in seen:
            duplicate_ids.append(redemption.pk)
        else:
            seen.add(key)
    if duplicate_ids:
        Redemption.objects.filter(pk__in=duplicate_ids).delete()


def reverse_legacy_redemptions(apps, schema_editor):
    # The removed rows are historical duplicates and cannot be reconstructed
    # without retaining a second audit table; the unique index rollback is
    # still safe and complete.
    return None


class Migration(migrations.Migration):
    dependencies = [("accounts", "0024_player_must_change_password_and_more")]

    operations = [
        migrations.RunPython(collapse_legacy_redemptions, reverse_legacy_redemptions),
        migrations.AddConstraint(
            model_name="competitioninviteredemption",
            constraint=models.UniqueConstraint(
                fields=("competition", "code_digest"),
                name="accounts_invite_redemption_code_unique",
            ),
        ),
        # The project uses Django's swappable default User model.  Functional
        # indexes enforce the same case-insensitive identity rules as the
        # account service, including concurrent registrations.
        migrations.RunSQL(
            sql=(
                "CREATE UNIQUE INDEX IF NOT EXISTS accounts_user_username_ci "
                "ON auth_user (LOWER(username));"
            ),
            reverse_sql="DROP INDEX IF EXISTS accounts_user_username_ci;",
        ),
        migrations.RunSQL(
            sql=(
                "CREATE UNIQUE INDEX IF NOT EXISTS accounts_user_email_ci "
                "ON auth_user (LOWER(email)) WHERE email <> '';"
            ),
            reverse_sql="DROP INDEX IF EXISTS accounts_user_email_ci;",
        ),
    ]
