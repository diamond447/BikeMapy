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
GAME_ENABLED = env_bool("GAME_ENABLED", False)
REFERENCE_ROUTE_AUTHORIZER = os.getenv(
    "REFERENCE_ROUTE_AUTHORIZER",
    "apps.api.reference_authorization.default_reference_route_authorizer",
)
REFERENCE_ROUTE_DERIVATIVE_OFFER_URL = os.getenv(
    "REFERENCE_ROUTE_DERIVATIVE_OFFER_URL",
    "",
)
# Synthetic source snapshots are useful in local tests, but must be explicitly
# enabled and can never satisfy the production publication gate.
REFERENCE_ROUTE_ALLOW_TEST_IMPORTS = env_bool("REFERENCE_ROUTE_ALLOW_TEST_IMPORTS", False)
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
    "apps.reference_routes",
]
if DATABASE_ENGINE == "django.db.backends.sqlite3":
    # Host-side smoke checks can run without native GeoDjango libraries. The
    # Compose and production defaults still load the full PostGIS stack.
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
    # Static files are served by the application only in the production image;
    # local development keeps Django's runserver static-file behavior.
    MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")

ROOT_URLCONF = "config.urls"
SITE_ID = 1
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]
LOGIN_REDIRECT_URL = "/admin/"
SOCIALACCOUNT_ADAPTER = "apps.accounts.adapters.OwnerSocialAccountAdapter"
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

# Authorization uses GitHub's immutable numeric account ID.  Keep this empty
# by default so a deployment must explicitly opt in to owner administration.
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
# GET requests from dynamic Pages previews need CORS, while the middleware
# above rejects every state-changing request from the same origin pattern.
CORS_ALLOWED_ORIGIN_REGEXES = [READ_ONLY_PREVIEW_ORIGIN_REGEX]
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = env_bool("USE_X_FORWARDED_HOST", False)

# The public edge terminates TLS and proxy_params preserves that scheme for
# Django. Production therefore redirects any request that reaches Django
# without the HTTPS scheme and emits a one-year HSTS policy. Secure cookies
# must never be relaxed in production: an insecure first request must not be
# able to establish an owner session or CSRF cookie over cleartext transport.
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
    # The catalogue is intentionally public and read-only, but an unbounded
    # client must not be able to consume all API capacity.  Deployments can
    # tune these values without changing the application.
    "DEFAULT_THROTTLE_CLASSES": [
        "apps.api.throttling.ApiAnonRateThrottle",
        "apps.api.throttling.ApiUserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": os.getenv("API_ANON_RATE", "120/minute"),
        "user": os.getenv("API_USER_RATE", "600/minute"),
    },
}
# Analytics is deliberately protected by one coarse, non-identifying bucket;
# unlike the generic API throttle it never derives a cache key from an IP.
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
if DJANGO_CACHE_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": DJANGO_CACHE_URL,
        }
    }
else:
    # Host-side checks intentionally remain self-contained. Compose and
    # production should set DJANGO_CACHE_URL to the shared Redis database.
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "bikemapy",
        }
    }
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 60 * 10
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_TASK_ALWAYS_EAGER", False)
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

# Health monitoring treats a daily crawl as stale after this configurable
# window. The separate endpoint is intended for an external uptime check.
CRAWLER_FRESHNESS_MAX_AGE = int(os.getenv("CRAWLER_FRESHNESS_MAX_AGE", str(36 * 3600)))

# Anonymous report protections and privacy retention.  The secret is never
# written to a report; only HMAC-derived cache identifiers are used.
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
# Raw forum HTML is retained only for short-lived replay after a crawl. The
# URL, final URL, status, validators, checksum, and fetch time remain useful
# crawler metadata after the scheduled cleanup clears the body.
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
# Do not let an origin serve an arbitrarily large HTML document to the parser.
BIKEFORUM_MAX_BYTES = int(os.getenv("BIKEFORUM_MAX_BYTES", str(5 * 1024 * 1024)))
BIKEFORUM_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("BIKEFORUM_ALLOWED_ORIGINS", "https://www.bike-forum.cz").split(",")
    if origin.strip()
]
# The disposable launch harness sets this false because its synthetic source
# URLs must never trigger requests to real Mapy hosts. Production must retain
# the default and run the bounded source availability checks.
BIKEFORUM_CHECK_SOURCES = env_bool("BIKEFORUM_CHECK_SOURCES", True)
# Real-source crawling remains off until the provider and operator decisions
# recorded in docs/legal-review.md have been completed.
BIKEFORUM_CRAWL_ENABLED = env_bool("BIKEFORUM_CRAWL_ENABLED", False)
BIKEFORUM_PROVIDER_AUTHORIZED = env_bool("BIKEFORUM_PROVIDER_AUTHORIZED", False)
BIKEFORUM_OPERATOR_APPROVED = env_bool("BIKEFORUM_OPERATOR_APPROVED", False)
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
# Legal/terms review is an explicit deployment gate. Keep downloads off by
# default even when an imported payload remains in local storage.
GPX_REDISTRIBUTION_APPROVED = env_bool("GPX_REDISTRIBUTION_APPROVED", False)
# Extraction activation is deliberately independent from redistribution.  All
# three gates must be enabled before a discovered source can reach Mapy.
GPX_EXTRACTION_ENABLED = env_bool("GPX_EXTRACTION_ENABLED", False)
GPX_PROVIDER_AUTHORIZED = env_bool("GPX_PROVIDER_AUTHORIZED", False)
GPX_LEGAL_APPROVED = env_bool("GPX_LEGAL_APPROVED", False)
GPX_MAX_ATTEMPTS = int(os.getenv("GPX_MAX_ATTEMPTS", "3"))
GPX_DISPATCH_TIMEOUT = int(os.getenv("GPX_DISPATCH_TIMEOUT", "900"))
GPX_PROCESSING_TIMEOUT = int(os.getenv("GPX_PROCESSING_TIMEOUT", "1800"))
# Orphan payload cleanup is retried with bounded exponential backoff.  A
# cleanup that remains unavailable after the limit is retained as exhausted
# operational evidence and is never retried automatically again.
GPX_ORPHAN_CLEANUP_MAX_ATTEMPTS = int(os.getenv("GPX_ORPHAN_CLEANUP_MAX_ATTEMPTS", "5"))
GPX_ORPHAN_CLEANUP_RETRY_BASE_SECONDS = int(
    os.getenv("GPX_ORPHAN_CLEANUP_RETRY_BASE_SECONDS", "60")
)
GPX_ORPHAN_CLEANUP_RETRY_MAX_SECONDS = int(
    os.getenv("GPX_ORPHAN_CLEANUP_RETRY_MAX_SECONDS", "3600")
)
GPX_ORPHAN_CLEANUP_LEASE_SECONDS = int(os.getenv("GPX_ORPHAN_CLEANUP_LEASE_SECONDS", "900"))
# Direct Django deployments keep the streaming fallback. The production
# Nginx stack enables the internal X-Accel-Redirect handoff.
GPX_INTERNAL_REDIRECT = env_bool("GPX_INTERNAL_REDIRECT", False)

# Sentry stores events according to the project retention setting. Keep the
# application-side contract explicit and document the required 30-day Sentry
# project policy in the operations runbook.
SENTRY_DSN = os.getenv("SENTRY_DSN", "")
SENTRY_ENVIRONMENT = os.getenv("SENTRY_ENVIRONMENT", "production")
SENTRY_TRACES_SAMPLE_RATE = float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0"))
SENTRY_RETENTION_DAYS = int(os.getenv("SENTRY_RETENTION_DAYS", "30"))
if SENTRY_RETENTION_DAYS != 30:
    raise ValueError("SENTRY_RETENTION_DAYS must remain exactly 30 days")
init_sentry()

# Spatial duplicate detection is intentionally precision-oriented.  Keep the
# values configurable so benchmark results can tune policy without a schema
# change or code deployment.
ROUTE_SIMILARITY_SAMPLE_POINTS = int(os.getenv("ROUTE_SIMILARITY_SAMPLE_POINTS", "64"))
ROUTE_SIMILARITY_GPS_TOLERANCE_M = float(os.getenv("ROUTE_SIMILARITY_GPS_TOLERANCE_M", "30"))
ROUTE_DUPLICATE_MAX_LENGTH_DELTA = float(os.getenv("ROUTE_DUPLICATE_MAX_LENGTH_DELTA", "0.10"))
ROUTE_DUPLICATE_MAX_MEAN_DISTANCE_M = float(os.getenv("ROUTE_DUPLICATE_MAX_MEAN_DISTANCE_M", "25"))
ROUTE_DUPLICATE_MAX_MAX_DISTANCE_M = float(os.getenv("ROUTE_DUPLICATE_MAX_MAX_DISTANCE_M", "100"))
ROUTE_VARIANT_MIN_SCORE = float(os.getenv("ROUTE_VARIANT_MIN_SCORE", "0.55"))
# Similarity keyset-pages candidate IDs and fetches geometries in these
# batches. The page size bounds memory, while every page is eventually scored.
ROUTE_DEDUPLICATION_CANDIDATE_PAGE_SIZE = int(
    os.getenv("ROUTE_DEDUPLICATION_CANDIDATE_PAGE_SIZE", "500")
)
ROUTE_DEDUPLICATION_BATCH_SIZE = int(os.getenv("ROUTE_DEDUPLICATION_BATCH_SIZE", "100"))

# Spatial browse products.  The heatmap is deliberately limited to low zooms;
# route lines take over at closer zooms.  Every public query has an explicit
# bound so a large viewport cannot accidentally become a catalogue dump.
SPATIAL_BROWSE_ZOOMS = os.getenv("SPATIAL_BROWSE_ZOOMS", "6,8,10,12,14,16,18")
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
