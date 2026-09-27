from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0025_provider_account_security")]

    operations = [
        migrations.AddField(
            model_name="player",
            name="temporary_password_used",
            field=models.BooleanField(default=False),
        ),
    ]
