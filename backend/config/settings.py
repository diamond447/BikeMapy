"""Django settings for local development and the production baseline."""

import os
from pathlib import Path
from typing import Any

from django.core.exceptions import ImproperlyConfigured

from .logging import LOGGING as LOGGING_CONFIG_VALUE
from .observability import init_sentry

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int = 0) -> int:
    """Parse optional integer settings while treating blank env values as unset."""

    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be an integer") from exc


LOCAL_DEVELOPMENT_SECRET_KEY = "local-development-key-do-not-use-in-production"
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", LOCAL_DEVELOPMENT_SECRET_KEY)
DEPLOYMENT_MODE = os.getenv("BIKEMAPY_DEPLOYMENT_MODE", "local").lower()


def validate_production_secret_key(secret_key: str | None) -> None:
    """Reject absent, placeholder, and obviously weak signing keys in production."""

    if not secret_key or not secret_key.strip():
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY must be explicitly configured in production deployments"
        )

    normalized = secret_key.strip().lower()
    placeholder_markers = (
        "change-me",
        "changeme",
        "do-not-use",
        "example",
        "local-development",
        "placeholder",
        "replace-with",
        "your-",
    )
    if len(secret_key.strip()) < 50 or len(set(secret_key.strip())) < 12:
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY must be at least 50 characters and contain sufficient variation"
        )
    if normalized.startswith("django-insecure-"):
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY must not use Django's insecure generated-key prefix"
        )
    if any(marker in normalized for marker in placeholder_markers):
        raise ImproperlyConfigured("DJANGO_SECRET_KEY must not contain a placeholder value")


if DEPLOYMENT_MODE == "production":
    validate_production_secret_key(os.getenv("DJANGO_SECRET_KEY"))
if DEPLOYMENT_MODE == "production" and "DJANGO_DEBUG" not in os.environ:
    raise ImproperlyConfigured(
        "DJANGO_DEBUG must be explicitly set to false in production deployments"
    )
DEBUG = env_bool("DJANGO_DEBUG", True)
if DEPLOYMENT_MODE == "production" and DEBUG:
    raise ImproperlyConfigured("DJANGO_DEBUG must be false in production deployments")
DATABASE_ENGINE = os.getenv("DJANGO_DATABASE_ENGINE", "django.contrib.gis.db.backends.postgis")
ALLOWED_HOSTS = [
    host for host in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if host
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.gis",
    "django.contrib.postgres",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.sites",
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",
    "drf_spectacular",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.github",
    "apps.catalogue",
    "apps.ingestion",
    "apps.moderation",
    "apps.reports",
    "apps.accounts",
    "apps.api",
    "apps.analytics",
]
if DATABASE_ENGINE == "django.db.backends.sqlite3":
    # Host-side checks run without the native GeoDjango libraries.
    INSTALLED_APPS.remove("django.contrib.gis")

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "config.middleware.TrustedProxyClientIdentityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "config.middleware.PreviewReadOnlyMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
if not DEBUG:
    MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")

ROOT_URLCONF = "config.urls"
SITE_ID = 1
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]
LOGIN_REDIRECT_URL = "/admin/"
SOCIALACCOUNT_ADAPTER = "apps.accounts.adapters.OwnerSocialAccountAdapter"
ACCOUNT_ADAPTER = "apps.accounts.adapters.OwnerAccountAdapter"
SOCIALACCOUNT_STORE_TOKENS = False
SOCIALACCOUNT_EMAIL_AUTHENTICATION = False
ACCOUNT_EMAIL_VERIFICATION = "none"
SOCIALACCOUNT_PROVIDERS: dict[str, dict[str, Any]] = {
    "github": {
        "SCOPE": ["read:user"],
    }
}
GITHUB_OAUTH_CLIENT_ID = os.getenv("GITHUB_OAUTH_CLIENT_ID", "")
GITHUB_OAUTH_CLIENT_SECRET = os.getenv("GITHUB_OAUTH_CLIENT_SECRET", "")
if GITHUB_OAUTH_CLIENT_ID and GITHUB_OAUTH_CLIENT_SECRET:
    SOCIALACCOUNT_PROVIDERS["github"]["APP"] = {
        "client_id": GITHUB_OAUTH_CLIENT_ID,
        "secret": GITHUB_OAUTH_CLIENT_SECRET,
    }

# Numeric GitHub account IDs, which cannot be renamed like logins.
GITHUB_OWNER_IDS = frozenset(
    value.strip()
    for value in os.getenv("GITHUB_OWNER_IDS", "").split(",")
    if value.strip().isdigit()
)
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

DATABASES: dict[str, dict[str, Any]] = {
    "default": {
        "ENGINE": DATABASE_ENGINE,
        "NAME": os.getenv("POSTGRES_DB", "bikemapy"),
        "USER": os.getenv("POSTGRES_USER", "bikemapy"),
        "PASSWORD": os.getenv("POSTGRES_PASSWORD", "bikemapy-local-only"),
        "HOST": os.getenv("POSTGRES_HOST", "db"),
        "PORT": os.getenv("POSTGRES_PORT", "5432"),
        # Persistent connections only in production: idle ones left by
        # threaded tests would block dropping the test database.
        "CONN_MAX_AGE": env_int(
            "DJANGO_CONN_MAX_AGE", 60 if DEPLOYMENT_MODE == "production" else 0
        ),
        "CONN_HEALTH_CHECKS": True,
    }
}
if DATABASE_ENGINE == "django.db.backends.sqlite3":
    DATABASES["default"] = {"ENGINE": DATABASE_ENGINE, "NAME": str(BASE_DIR / "test.sqlite3")}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Europe/Prague"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
ROOT_STORAGE = BASE_DIR / "storage"
MEDIA_ROOT = Path(os.getenv("DJANGO_MEDIA_ROOT", str(ROOT_STORAGE / "media")))
MEDIA_URL = "/media/"
if not DEBUG:
    STORAGES = {
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(MEDIA_ROOT), "base_url": MEDIA_URL},
        },
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
        },
    }
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGGING_CONFIG = "logging.config.dictConfig"
LOGGING = LOGGING_CONFIG_VALUE

CORS_ALLOWED_ORIGINS = [
    origin
    for origin in os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:5173").split(",")
    if origin
]
READ_ONLY_PREVIEW_ORIGIN_REGEX = os.getenv(
    "READ_ONLY_PREVIEW_ORIGIN_REGEX",
    r"\Ahttps://([a-z0-9-]+\.)+bikemapy\.pages\.dev\Z",
)
# Previews may read; PreviewReadOnlyMiddleware rejects their writes.
CORS_ALLOWED_ORIGIN_REGEXES = [READ_ONLY_PREVIEW_ORIGIN_REGEX]
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = env_bool("USE_X_FORWARDED_HOST", False)

SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_SSL_REDIRECT = not DEBUG
SECURE_HSTS_SECONDS = 31536000 if not DEBUG else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = not DEBUG


def validate_production_security_settings() -> None:
    """Fail startup when production transport protections are weakened."""

    if DEBUG:
        return

    invalid = [
        name
        for name, valid in (
            ("SESSION_COOKIE_SECURE", SESSION_COOKIE_SECURE),
            ("CSRF_COOKIE_SECURE", CSRF_COOKIE_SECURE),
            ("SECURE_SSL_REDIRECT", SECURE_SSL_REDIRECT),
            ("SECURE_HSTS_SECONDS", SECURE_HSTS_SECONDS >= 31536000),
            ("SECURE_HSTS_INCLUDE_SUBDOMAINS", SECURE_HSTS_INCLUDE_SUBDOMAINS),
            ("SECURE_HSTS_PRELOAD", SECURE_HSTS_PRELOAD),
            (
                "SECURE_PROXY_SSL_HEADER",
                SECURE_PROXY_SSL_HEADER == ("HTTP_X_FORWARDED_PROTO", "https"),
            ),
        )
        if not valid
    ]
    if invalid:
        names = ", ".join(invalid)
        raise ImproperlyConfigured(
            f"Production transport security settings are not enabled: {names}"
        )


validate_production_security_settings()

REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "DEFAULT_THROTTLE_CLASSES": [
        "apps.api.throttling.ApiAnonRateThrottle",
        "apps.api.throttling.ApiUserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": os.getenv("API_ANON_RATE", "120/minute"),
        "user": os.getenv("API_USER_RATE", "600/minute"),
    },
}
# One global bucket, so no cache key is ever derived from a client IP.
ANALYTICS_EVENT_RATE = os.getenv("ANALYTICS_EVENT_RATE", "600/minute")
SPECTACULAR_SETTINGS = {
    "TITLE": "BikeMapy API",
    "DESCRIPTION": "Public, versioned read API for BikeMapy.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", CELERY_BROKER_URL)
DJANGO_CACHE_URL = os.getenv("DJANGO_CACHE_URL", "")
# False in lean mode (deploy/compose.lean.yml): no Redis, worker or beat.
BACKGROUND_JOBS_ENABLED = env_bool("BACKGROUND_JOBS_ENABLED", True)
DATABASE_CACHE_TABLE = "bikemapy_cache"
if not BACKGROUND_JOBS_ENABLED:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.db.DatabaseCache",
            "LOCATION": DATABASE_CACHE_TABLE,
            # Django's default of 300 entries would evict throttle buckets.
            "OPTIONS": {"MAX_ENTRIES": 20_000},
        }
    }
elif DJANGO_CACHE_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": DJANGO_CACHE_URL,
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "bikemapy",
        }
    }
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 60 * 10
CELERY_TASK_ALWAYS_EAGER = not BACKGROUND_JOBS_ENABLED or env_bool(
    "CELERY_TASK_ALWAYS_EAGER", False
)
CELERY_WORKER_HIJACK_ROOT_LOGGER = False
CELERY_WORKER_REDIRECT_STDOUTS = True
CELERY_BEAT_SCHEDULE = {
    "bikeforum-incremental-daily": {
        "task": "bikemapy.ingestion.incremental_bikeforum_crawl",
        "schedule": 86400,
    },
    "reconcile-route-extractions": {
        "task": "bikemapy.ingestion.reconcile_extractions",
        "schedule": 900,
    },
    "reconcile-orphan-gpx": {
        "task": "bikemapy.ingestion.reconcile_orphan_gpx",
        "schedule": 900,
    },
    "retry-route-payload-deletions": {
        "task": "bikemapy.ingestion.retry_payload_deletions",
        "schedule": 900,
    },
    "retain-closed-reports": {
        "task": "bikemapy.reports.retain_closed_reports",
        "schedule": 86400,
    },
    "cleanup-crawler-response-cache": {
        "task": "bikemapy.ingestion.cleanup_crawl_response_cache",
        "schedule": 3600,
    },
}

CRAWLER_FRESHNESS_MAX_AGE = int(os.getenv("CRAWLER_FRESHNESS_MAX_AGE", str(36 * 3600)))

REPORT_TURNSTILE_SECRET_KEY = os.getenv("REPORT_TURNSTILE_SECRET_KEY", "")
REPORT_TURNSTILE_VERIFY_URL = os.getenv(
    "REPORT_TURNSTILE_VERIFY_URL", "https://challenges.cloudflare.com/turnstile/v0/siteverify"
)
REPORT_TURNSTILE_TIMEOUT = float(os.getenv("REPORT_TURNSTILE_TIMEOUT", "5"))
REPORT_RATE_LIMIT_HMAC_SECRET = os.getenv("REPORT_RATE_LIMIT_HMAC_SECRET", "")
RATE_LIMIT_HMAC_SECRET = os.getenv("RATE_LIMIT_HMAC_SECRET", "")
REPORT_RATE_LIMIT_HOURLY = int(os.getenv("REPORT_RATE_LIMIT_HOURLY", "3"))
REPORT_RATE_LIMIT_DAILY = int(os.getenv("REPORT_RATE_LIMIT_DAILY", "10"))
REPORT_CLIENT_IP_MODE = os.getenv("REPORT_CLIENT_IP_MODE", "direct").lower()
REPORT_TRUSTED_PROXY_CIDRS = tuple(
    value.strip()
    for value in os.getenv("REPORT_TRUSTED_PROXY_CIDRS", "").split(",")
    if value.strip()
)
REPORT_DUPLICATE_WINDOW = int(os.getenv("REPORT_DUPLICATE_WINDOW", str(24 * 3600)))
REPORT_EMAIL_RETENTION = int(os.getenv("REPORT_EMAIL_RETENTION", str(90 * 86400)))
REPORT_DETAILS_RETENTION = int(os.getenv("REPORT_DETAILS_RETENTION", str(365 * 86400)))

BIKEFORUM_USER_AGENT = os.getenv(
    "BIKEFORUM_USER_AGENT", "BikeMapyBot/1.0 (+https://github.com/diamond447/BikeMapy)"
)
BIKEFORUM_TIMEOUT = float(os.getenv("BIKEFORUM_TIMEOUT", "15"))
BIKEFORUM_RATE_LIMIT = float(os.getenv("BIKEFORUM_RATE_LIMIT", "2"))
BIKEFORUM_RETRIES = int(os.getenv("BIKEFORUM_RETRIES", "2"))
BIKEFORUM_BACKOFF = float(os.getenv("BIKEFORUM_BACKOFF", "1"))
BIKEFORUM_CACHE_TTL = float(os.getenv("BIKEFORUM_CACHE_TTL", "3600"))
# Raw forum HTML can contain usernames, so it is cleared after at most a day.
BIKEFORUM_CACHE_BODY_RETENTION_SECONDS = int(
    os.getenv("BIKEFORUM_CACHE_BODY_RETENTION_SECONDS", str(24 * 3600))
)
CRAWLER_CACHE_BODY_RETENTION_MAX_SECONDS = 24 * 3600
if not 1 <= BIKEFORUM_CACHE_BODY_RETENTION_SECONDS <= CRAWLER_CACHE_BODY_RETENTION_MAX_SECONDS:
    raise ValueError(
        "BIKEFORUM_CACHE_BODY_RETENTION_SECONDS must be between 1 and "
        f"{CRAWLER_CACHE_BODY_RETENTION_MAX_SECONDS} seconds"
    )
BIKEFORUM_CACHE_CLEANUP_BATCH_SIZE = int(os.getenv("BIKEFORUM_CACHE_CLEANUP_BATCH_SIZE", "500"))
BIKEFORUM_MAX_PAGES = int(os.getenv("BIKEFORUM_MAX_PAGES", "100"))
BIKEFORUM_MAX_BYTES = int(os.getenv("BIKEFORUM_MAX_BYTES", str(5 * 1024 * 1024)))
BIKEFORUM_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("BIKEFORUM_ALLOWED_ORIGINS", "https://www.bike-forum.cz").split(",")
    if origin.strip()
]
# The launch rehearsal turns this off so its fake source URLs never reach Mapy.
BIKEFORUM_CHECK_SOURCES = env_bool("BIKEFORUM_CHECK_SOURCES", True)
BIKEFORUM_DNS_CHECK = env_bool("BIKEFORUM_DNS_CHECK", True)
BIKEFORUM_LEASE_SECONDS = int(os.getenv("BIKEFORUM_LEASE_SECONDS", "600"))
BIKEFORUM_PAGE_ATTEMPTS = int(os.getenv("BIKEFORUM_PAGE_ATTEMPTS", "3"))
BIKEFORUM_INCREMENTAL_URL = os.getenv(
    "BIKEFORUM_INCREMENTAL_URL", "https://www.bike-forum.cz/forum/"
)

GPX_TIMEOUT = float(os.getenv("GPX_TIMEOUT", "30"))
GPX_RETRIES = int(os.getenv("GPX_RETRIES", "3"))
GPX_BACKOFF = float(os.getenv("GPX_BACKOFF", "0.5"))
GPX_MAX_BYTES = int(os.getenv("GPX_MAX_BYTES", str(10 * 1024 * 1024)))
GPX_MAX_POINTS = int(os.getenv("GPX_MAX_POINTS", "200000"))
GPX_DNS_CHECK = env_bool("GPX_DNS_CHECK", True)
GPX_MAX_ATTEMPTS = int(os.getenv("GPX_MAX_ATTEMPTS", "3"))
GPX_DISPATCH_TIMEOUT = int(os.getenv("GPX_DISPATCH_TIMEOUT", "900"))
GPX_PROCESSING_TIMEOUT = int(os.getenv("GPX_PROCESSING_TIMEOUT", "1800"))
# After the last attempt an orphan cleanup is kept as exhausted, not retried.
GPX_ORPHAN_CLEANUP_MAX_ATTEMPTS = int(os.getenv("GPX_ORPHAN_CLEANUP_MAX_ATTEMPTS", "5"))
GPX_ORPHAN_CLEANUP_RETRY_BASE_SECONDS = int(
    os.getenv("GPX_ORPHAN_CLEANUP_RETRY_BASE_SECONDS", "60")
)
GPX_ORPHAN_CLEANUP_RETRY_MAX_SECONDS = int(
    os.getenv("GPX_ORPHAN_CLEANUP_RETRY_MAX_SECONDS", "3600")
)
GPX_ORPHAN_CLEANUP_LEASE_SECONDS = int(os.getenv("GPX_ORPHAN_CLEANUP_LEASE_SECONDS", "900"))
# Set behind Nginx to hand downloads off with X-Accel-Redirect.
GPX_INTERNAL_REDIRECT = env_bool("GPX_INTERNAL_REDIRECT", False)

# Must match the retention configured in the Sentry project (privacy notice).
SENTRY_DSN = os.getenv("SENTRY_DSN", "")
SENTRY_ENVIRONMENT = os.getenv("SENTRY_ENVIRONMENT", "production")
SENTRY_TRACES_SAMPLE_RATE = float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0"))
SENTRY_RETENTION_DAYS = int(os.getenv("SENTRY_RETENTION_DAYS", "30"))
if SENTRY_RETENTION_DAYS != 30:
    raise ValueError("SENTRY_RETENTION_DAYS must remain exactly 30 days")
init_sentry()

ROUTE_SIMILARITY_SAMPLE_POINTS = int(os.getenv("ROUTE_SIMILARITY_SAMPLE_POINTS", "64"))
ROUTE_SIMILARITY_GPS_TOLERANCE_M = float(os.getenv("ROUTE_SIMILARITY_GPS_TOLERANCE_M", "30"))
ROUTE_DUPLICATE_MAX_LENGTH_DELTA = float(os.getenv("ROUTE_DUPLICATE_MAX_LENGTH_DELTA", "0.10"))
ROUTE_DUPLICATE_MAX_MEAN_DISTANCE_M = float(os.getenv("ROUTE_DUPLICATE_MAX_MEAN_DISTANCE_M", "25"))
ROUTE_DUPLICATE_MAX_MAX_DISTANCE_M = float(os.getenv("ROUTE_DUPLICATE_MAX_MAX_DISTANCE_M", "100"))
ROUTE_VARIANT_MIN_SCORE = float(os.getenv("ROUTE_VARIANT_MIN_SCORE", "0.55"))
ROUTE_DEDUPLICATION_CANDIDATE_PAGE_SIZE = int(
    os.getenv("ROUTE_DEDUPLICATION_CANDIDATE_PAGE_SIZE", "500")
)
ROUTE_DEDUPLICATION_BATCH_SIZE = int(os.getenv("ROUTE_DEDUPLICATION_BATCH_SIZE", "100"))

SPATIAL_BROWSE_ZOOMS = os.getenv("SPATIAL_BROWSE_ZOOMS", "6,8,10,12,14")
# Browser and CDN cache lifetime; the server-side cache is invalidated by epoch.
SPATIAL_HTTP_MAX_AGE = env_int("SPATIAL_HTTP_MAX_AGE", 60)
SPATIAL_HEATMAP_ZOOMS = os.getenv("SPATIAL_HEATMAP_ZOOMS", "3,4,5,6,7,8")
SPATIAL_MAX_ROUTES_PER_QUERY = int(os.getenv("SPATIAL_MAX_ROUTES_PER_QUERY", "500"))
SPATIAL_MAX_CELLS_PER_QUERY = int(os.getenv("SPATIAL_MAX_CELLS_PER_QUERY", "10000"))
SPATIAL_HARD_MAX_ROUTES_PER_QUERY = int(os.getenv("SPATIAL_HARD_MAX_ROUTES_PER_QUERY", "500"))
SPATIAL_HARD_MAX_CELLS_PER_QUERY = int(os.getenv("SPATIAL_HARD_MAX_CELLS_PER_QUERY", "10000"))
SPATIAL_MAX_CANDIDATE_SCAN = int(os.getenv("SPATIAL_MAX_CANDIDATE_SCAN", "5000"))
SPATIAL_MAX_HEATMAP_CANDIDATE_CELLS = int(
    os.getenv("SPATIAL_MAX_HEATMAP_CANDIDATE_CELLS", "100000")
)
SPATIAL_MAX_VIEWPORT_WIDTH_DEGREES = float(os.getenv("SPATIAL_MAX_VIEWPORT_WIDTH_DEGREES", "90"))
SPATIAL_MAX_VIEWPORT_HEIGHT_DEGREES = float(os.getenv("SPATIAL_MAX_VIEWPORT_HEIGHT_DEGREES", "90"))
SPATIAL_QUERY_CACHE_TTL = int(os.getenv("SPATIAL_QUERY_CACHE_TTL", "60"))
