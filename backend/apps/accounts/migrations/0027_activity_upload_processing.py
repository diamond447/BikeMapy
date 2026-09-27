from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0026_temporary_password_used")]

    operations = [
        migrations.AlterField(
            model_name="activityupload",
            name="status",
            field=models.CharField(
                choices=[
                    ("queued", "Queued"),
                    ("processing", "Processing"),
                    ("accepted", "Accepted"),
                    ("duplicate", "Duplicate"),
                    ("unsupported", "Unsupported"),
                    ("failed", "Failed"),
                ],
                default="queued",
                max_length=16,
            ),
        )
    ]
