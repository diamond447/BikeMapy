from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("reference_routes", "0002_reference_route_import_hardening")]

    operations = [
        migrations.AddField(model_name="referencecollection", name="permission_granted", field=models.BooleanField(default=False)),
        migrations.AddField(model_name="referencerouteversion", name="attribution_metadata", field=models.JSONField(default=dict)),
    ]
