from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("ingestion", "0007_durable_dispatch_claim")]

    operations = [
        migrations.AlterField(
            model_name="orphanpayloadcleanup",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("failed", "Failed"),
                    ("completed", "Completed"),
                    ("exhausted", "Retry limit exhausted"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="orphanpayloadcleanup",
            name="claim_token",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="orphanpayloadcleanup",
            name="claimed_until",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="orphanpayloadcleanup",
            name="next_retry_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name="orphanpayloadcleanup",
            index=models.Index(
                fields=["status", "next_retry_at", "created_at"],
                name="ing_orphan_reconcile_idx",
            ),
        ),
    ]
