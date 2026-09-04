"""Django settings for local development and the production baseline."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes", "on"}


SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "local-development-key-do-not-use-in-production")
DEBUG = env_bool("DJANGO_DEBUG", True)
DATABASE_ENGINE = os.getenv("DJANGO_DATABASE_ENGINE", "django.contrib.gis.db.backends.postgis")
ALLOWED_HOSTS = [
    host for host in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if host
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.gis",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",
    "drf_spectacular",
    "apps.catalogue",
    "apps.ingestion",
    "apps.moderation",
    "apps.reports",
    "apps.accounts",
    "apps.api",
]
if DATABASE_ENGINE == "django.db.backends.sqlite3":
    # Host-side smoke checks can run without native GeoDjango libraries. The
    # Compose and production defaults still load the full PostGIS stack.
    INSTALLED_APPS.remove("django.contrib.gis")

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "backend" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]
WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": DATABASE_ENGINE,
        "NAME": os.getenv("POSTGRES_DB", "bikemapy"),
        "USER": os.getenv("POSTGRES_USER", "bikemapy"),
        "PASSWORD": os.getenv("POSTGRES_PASSWORD", "bikemapy-local-only"),
        "HOST": os.getenv("POSTGRES_HOST", "db"),
        "PORT": os.getenv("POSTGRES_PORT", "5432"),
    }
}
if DATABASE_ENGINE == "django.db.backends.sqlite3":
    DATABASES["default"] = {"ENGINE": DATABASE_ENGINE, "NAME": str(BASE_DIR / "test.sqlite3")}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Europe/Prague"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
ROOT_STORAGE = BASE_DIR / "storage"
MEDIA_ROOT = Path(os.getenv("DJANGO_MEDIA_ROOT", str(ROOT_STORAGE / "media")))
MEDIA_URL = "/media/"

CORS_ALLOWED_ORIGINS = [
    origin
    for origin in os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:5173").split(",")
    if origin
]
REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
}
SPECTACULAR_SETTINGS = {
    "TITLE": "BikeMapy API",
    "DESCRIPTION": "Public, versioned read API for BikeMapy.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", CELERY_BROKER_URL)
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 60 * 10
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_TASK_ALWAYS_EAGER", False)
