from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0007_competitionrecomputation_attempts_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="competitionrecomputation",
            name="lease_token",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="competitionrecomputation",
            name="lease_until",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="competitionrecomputation",
            name="dispatch_token",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="competitionrecomputation",
            name="dispatch_lease_until",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
