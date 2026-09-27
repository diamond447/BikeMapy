from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0010_importedactivity_updated_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="stravasyncjob",
            name="dispatch_lease_until",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="stravasyncjob",
            name="dispatch_token",
            field=models.CharField(blank=True, max_length=64),
        ),
    ]
