from django.db import migrations, models

import apps.catalogue.fields


class Migration(migrations.Migration):
    dependencies = [("reference_routes", "0001_initial")]

    operations = [
        migrations.AddField(model_name="referencecollection", name="attribution_text", field=models.TextField(blank=True)),
        migrations.AddField(model_name="referencecollection", name="attribution_url", field=models.URLField(blank=True, max_length=1000)),
        migrations.AddField(model_name="referencecollection", name="contact_url", field=models.URLField(blank=True, max_length=1000)),
        migrations.AddField(model_name="referencecollection", name="derivative_offer_url", field=models.URLField(blank=True, max_length=1000)),
        migrations.AddField(model_name="referencecollection", name="licence_uri", field=models.URLField(blank=True, max_length=1000)),
        migrations.AddField(model_name="referencecollection", name="rightsholder", field=models.CharField(blank=True, max_length=255)),
        migrations.AddField(model_name="referenceimport", name="raw_response", field=models.BinaryField(default=bytes)),
        migrations.AddField(model_name="referenceimport", name="raw_response_sha256", field=models.CharField(blank=True, max_length=64)),
        migrations.AddField(model_name="referenceroute", name="publication_status", field=models.CharField(choices=[("pending", "Pending publication review"), ("approved", "Approved"), ("rejected", "Rejected")], default="pending", max_length=16)),
        migrations.AddField(model_name="referenceroute", name="reviewed_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="referenceroute", name="reviewed_by", field=models.CharField(blank=True, max_length=255)),
        migrations.AlterField(model_name="referencerouteversion", name="normalized_geometry", field=apps.catalogue.fields.RouteGeometryField(blank=True, null=True, spatial_index=True, srid=4326)),
        migrations.AlterField(model_name="referencerouteversion", name="validation_status", field=models.CharField(choices=[("pending_review", "Pending human review"), ("valid", "Valid"), ("invalid", "Invalid")], max_length=16)),
    ]
