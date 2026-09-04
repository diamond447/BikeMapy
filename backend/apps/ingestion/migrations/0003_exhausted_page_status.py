from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("ingestion", "0002_frontier_leases")]

    operations = [
        migrations.AlterField(
            model_name="crawlpagework",
            name="status",
            field=models.CharField(
                choices=[
                    ("queued", "Queued"),
                    ("processing", "Processing"),
                    ("succeeded", "Succeeded"),
                    ("failed", "Failed"),
                    ("exhausted", "Retry limit exhausted"),
                ],
                default="queued",
                max_length=20,
            ),
        ),
    ]
