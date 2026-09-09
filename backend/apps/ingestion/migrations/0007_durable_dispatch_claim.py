from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ingestion", "0006_extraction_dispatch_metadata"),
    ]

    operations = [
        migrations.AlterField(
            model_name="extractionattempt",
            name="status",
            field=models.CharField(
                choices=[
                    ("queued", "Queued"),
                    ("processing", "Processing"),
                    ("succeeded", "Succeeded"),
                    ("failed", "Failed"),
                    ("blocked", "Blocked by activation gate"),
                    ("superseded", "Superseded"),
                ],
                default="queued",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="extractionattempt",
            name="dispatch_claim_token",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="extractionattempt",
            name="dispatch_claimed_until",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
