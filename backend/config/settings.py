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
CELERY_BEAT_SCHEDULE = {
    "bikeforum-incremental-daily": {
        "task": "bikemapy.ingestion.incremental_bikeforum_crawl",
        "schedule": 86400,
    },
    "retry-route-payload-deletions": {
        "task": "bikemapy.ingestion.retry_payload_deletions",
        "schedule": 900,
    },
}

# Crawl defaults are intentionally conservative.  A deployment can tune them
# without changing code, while the identifiable UA remains explicit.
BIKEFORUM_USER_AGENT = os.getenv(
    "BIKEFORUM_USER_AGENT", "BikeMapyBot/1.0 (+https://github.com/diamond447/BikeMapy)"
)
BIKEFORUM_TIMEOUT = float(os.getenv("BIKEFORUM_TIMEOUT", "15"))
BIKEFORUM_RATE_LIMIT = float(os.getenv("BIKEFORUM_RATE_LIMIT", "2"))
BIKEFORUM_RETRIES = int(os.getenv("BIKEFORUM_RETRIES", "2"))
BIKEFORUM_BACKOFF = float(os.getenv("BIKEFORUM_BACKOFF", "1"))
BIKEFORUM_CACHE_TTL = float(os.getenv("BIKEFORUM_CACHE_TTL", "3600"))
BIKEFORUM_MAX_PAGES = int(os.getenv("BIKEFORUM_MAX_PAGES", "100"))
BIKEFORUM_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("BIKEFORUM_ALLOWED_ORIGINS", "https://www.bike-forum.cz").split(",")
    if origin.strip()
]
BIKEFORUM_DNS_CHECK = env_bool("BIKEFORUM_DNS_CHECK", True)
BIKEFORUM_LEASE_SECONDS = int(os.getenv("BIKEFORUM_LEASE_SECONDS", "600"))
BIKEFORUM_PAGE_ATTEMPTS = int(os.getenv("BIKEFORUM_PAGE_ATTEMPTS", "3"))
BIKEFORUM_INCREMENTAL_URL = os.getenv(
    "BIKEFORUM_INCREMENTAL_URL", "https://www.bike-forum.cz/forum/"
)

# GPX imports are bounded independently of the forum crawler.  The exporter
# follows only same-origin Mapy redirects; DNS validation adds SSRF defence.
GPX_TIMEOUT = float(os.getenv("GPX_TIMEOUT", "30"))
GPX_RETRIES = int(os.getenv("GPX_RETRIES", "3"))
GPX_BACKOFF = float(os.getenv("GPX_BACKOFF", "0.5"))
GPX_MAX_BYTES = int(os.getenv("GPX_MAX_BYTES", str(10 * 1024 * 1024)))
GPX_MAX_POINTS = int(os.getenv("GPX_MAX_POINTS", "200000"))
GPX_DNS_CHECK = env_bool("GPX_DNS_CHECK", True)

# Spatial duplicate detection is intentionally precision-oriented.  Keep the
# values configurable so benchmark results can tune policy without a schema
# change or code deployment.
ROUTE_SIMILARITY_SAMPLE_POINTS = int(os.getenv("ROUTE_SIMILARITY_SAMPLE_POINTS", "64"))
ROUTE_SIMILARITY_GPS_TOLERANCE_M = float(os.getenv("ROUTE_SIMILARITY_GPS_TOLERANCE_M", "30"))
ROUTE_DUPLICATE_MAX_LENGTH_DELTA = float(os.getenv("ROUTE_DUPLICATE_MAX_LENGTH_DELTA", "0.10"))
ROUTE_DUPLICATE_MAX_MEAN_DISTANCE_M = float(os.getenv("ROUTE_DUPLICATE_MAX_MEAN_DISTANCE_M", "25"))
ROUTE_DUPLICATE_MAX_MAX_DISTANCE_M = float(os.getenv("ROUTE_DUPLICATE_MAX_MAX_DISTANCE_M", "100"))
ROUTE_VARIANT_MIN_SCORE = float(os.getenv("ROUTE_VARIANT_MIN_SCORE", "0.55"))
