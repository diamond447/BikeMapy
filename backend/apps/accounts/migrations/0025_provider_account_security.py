from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0024_player_must_change_password_and_more")]

    operations = [
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
