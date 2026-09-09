import os
import subprocess
import sys
from pathlib import Path


def test_production_storage_keeps_media_default_and_whitenoise_static(tmp_path: Path) -> None:
    code = """
import django
from django.conf import settings
from django.core.files.base import ContentFile

django.setup()
from django.core.files.storage import default_storage, storages

assert settings.DEBUG is False
assert settings.STORAGES["default"]["BACKEND"] == "django.core.files.storage.FileSystemStorage"
assert settings.STORAGES["default"]["OPTIONS"]["location"] == str(settings.MEDIA_ROOT)
assert (
    settings.STORAGES["staticfiles"]["BACKEND"]
    == "whitenoise.storage.CompressedManifestStaticFilesStorage"
)
assert default_storage.__class__.__name__ == "FileSystemStorage"
saved_name = default_storage.save("smoke.gpx", ContentFile(b"<gpx />"))
with default_storage.open(saved_name) as stored_file:
    assert stored_file.read() == b"<gpx />"
default_storage.delete(saved_name)
assert not default_storage.exists(saved_name)
assert storages["staticfiles"].__class__.__name__ == "CompressedManifestStaticFilesStorage"
"""
    environment = os.environ.copy()
    environment.update(
        {
            "DJANGO_DEBUG": "false",
            "DJANGO_DATABASE_ENGINE": "django.db.backends.sqlite3",
            "DJANGO_MEDIA_ROOT": str(tmp_path),
            "PYTHONPATH": os.pathsep.join(
                value
                for value in (
                    str(Path(__file__).resolve().parents[1]),
                    environment.get("PYTHONPATH", ""),
                )
                if value
            ),
        }
    )
    subprocess.run([sys.executable, "-c", code], check=True, env=environment)
