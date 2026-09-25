from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone
import uuid
import apps.catalogue.fields


class Migration(migrations.Migration):
    initial = True
    dependencies = [("catalogue", "0007_alter_moderationdecision_action")]
    operations = [
        migrations.CreateModel(
            name="ReferenceCollection",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("slug", models.SlugField(max_length=120, unique=True)),
                ("name", models.CharField(max_length=255)),
                ("source_kind", models.CharField(choices=[("osm_numbered", "OpenStreetMap numbered cycling routes"), ("via_czechia", "Via Czechia")], max_length=32)),
                ("source_url", models.URLField(max_length=1000)),
                ("attribution", models.TextField()),
                ("licence", models.CharField(max_length=255)),
                ("active", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["name", "pk"], "indexes": [models.Index(fields=["source_kind", "active"], name="ref_coll_kind_active_idx")]},
        ),
        migrations.CreateModel(
            name="ReferenceImport",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("checksum", models.CharField(max_length=64)),
                ("endpoint", models.URLField(max_length=1000)),
                ("query_text", models.TextField(blank=True)),
                ("retrieved_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("source_timestamp", models.DateTimeField(blank=True, null=True)),
                ("response_metadata", models.JSONField(blank=True, default=dict)),
                ("raw_payload", models.JSONField(default=dict)),
                ("status", models.CharField(choices=[("discovered", "Discovered"), ("valid", "Valid"), ("invalid", "Invalid"), ("blocked", "Blocked"), ("failed", "Failed")], default="discovered", max_length=16)),
                ("diagnostics", models.JSONField(blank=True, default=list)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("collection", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="imports", to="reference_routes.referencecollection")),
            ],
            options={"ordering": ["-retrieved_at", "-pk"], "indexes": [models.Index(fields=["collection", "-retrieved_at"], name="ref_import_collection_date_idx"), models.Index(fields=["status"], name="ref_import_status_idx")]},
        ),
        migrations.CreateModel(
            name="ReferenceRoute",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("source_identifier", models.CharField(max_length=255)),
                ("route_number", models.CharField(blank=True, max_length=32)),
                ("title", models.CharField(max_length=500)),
                ("operator", models.CharField(blank=True, max_length=255)),
                ("network", models.CharField(blank=True, max_length=32)),
                ("route_type", models.CharField(choices=[("route", "Route"), ("stage", "Stage")], default="route", max_length=16)),
                ("source_tags", models.JSONField(blank=True, default=dict)),
                ("active", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("collection", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="routes", to="reference_routes.referencecollection")),
                ("parent", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="stages", to="reference_routes.referenceroute")),
            ],
            options={"ordering": ["route_number", "title", "id"], "indexes": [models.Index(fields=["collection", "active", "route_number"], name="ref_route_browse_idx"), models.Index(fields=["parent", "active"], name="ref_route_parent_idx")]},
        ),
        migrations.CreateModel(
            name="ReferenceRouteVersion",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("version_number", models.PositiveIntegerField()),
                ("source_version_identifier", models.CharField(blank=True, max_length=255)),
                ("checksum", models.CharField(max_length=64)),
                ("source_geometry", models.JSONField(default=dict)),
                ("normalized_geometry", apps.catalogue.fields.RouteGeometryField(srid=4326, spatial_index=True)),
                ("provenance", models.JSONField(default=dict)),
                ("attribution", models.TextField()),
                ("validation_status", models.CharField(choices=[("valid", "Valid"), ("invalid", "Invalid")], max_length=16)),
                ("diagnostics", models.JSONField(blank=True, default=list)),
                ("active", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("route", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="versions", to="reference_routes.referenceroute")),
                ("source_import", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="route_versions", to="reference_routes.referenceimport")),
            ],
            options={"ordering": ["route_id", "version_number"], "indexes": [models.Index(fields=["route", "active"], name="ref_version_route_active_idx"), models.Index(fields=["validation_status"], name="ref_version_status_idx")]},
        ),
        migrations.AddField(model_name="referenceroute", name="current_version", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="current_for_routes", to="reference_routes.referencerouteversion")),
        migrations.CreateModel(
            name="ReferenceRecomputation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("reason", models.CharField(max_length=255)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("running", "Running"), ("complete", "Complete"), ("failed", "Failed")], default="pending", max_length=16)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("scheduled_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("error", models.TextField(blank=True)),
                ("route_version", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="recomputations", to="reference_routes.referencerouteversion")),
            ],
            options={"ordering": ["created_at", "pk"], "indexes": [models.Index(fields=["status", "created_at"], name="ref_recompute_queue_idx")]},
        ),
        migrations.AddConstraint(model_name="referenceimport", constraint=models.UniqueConstraint(fields=("collection", "checksum"), name="ref_import_collection_checksum_unique")),
        migrations.AddConstraint(model_name="referenceroute", constraint=models.UniqueConstraint(fields=("collection", "source_identifier"), name="ref_route_collection_source_unique")),
        migrations.AddConstraint(model_name="referenceroute", constraint=models.CheckConstraint(condition=models.Q(("parent__isnull", False), ("route_type", "stage"), _connector="AND") | models.Q(("route_type", "route")), name="ref_stage_has_parent")),
        migrations.AddConstraint(model_name="referencerouteversion", constraint=models.UniqueConstraint(fields=("route", "version_number"), name="ref_version_route_number_unique")),
        migrations.AddConstraint(model_name="referencerouteversion", constraint=models.UniqueConstraint(fields=("route", "checksum"), name="ref_version_route_checksum_unique")),
        migrations.AddConstraint(model_name="referencerouteversion", constraint=models.CheckConstraint(condition=models.Q(("version_number__gt", 0)), name="ref_version_positive_number")),
    ]
