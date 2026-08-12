"""
Django settings for the FMC + ISE IoT Security Dashboard.

Configuration (Cisco credentials, TLS options, cache timeouts) is read from
environment variables, optionally loaded from a local `.env` file. See
`.env.example` for the full list of supported variables.
"""
from pathlib import Path
import os

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Load env files via python-dotenv (robust with special characters in secrets —
# no fragile `source` needed for manual `manage.py` commands). Prefer .env.prod,
# then .env. `override=False` so real environment variables (e.g. injected by
# systemd EnvironmentFile) always win. Point DOTENV_FILE at another path to
# override. Both files are optional.
for _envfile in (os.environ.get("DOTENV_FILE", ""), ".env.prod", ".env"):
    if not _envfile:
        continue
    _path = Path(_envfile) if os.path.isabs(_envfile) else BASE_DIR / _envfile
    if _path.exists():
        load_dotenv(_path, override=False)


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Core Django
# ---------------------------------------------------------------------------
SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY", "dev-insecure-change-me-in-production"
)
DEBUG = _env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if h.strip()
]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "dashboard",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # WhiteNoise serves collected static files in production (added below only
    # if the package is installed, so the dev app runs without it).
    "django.middleware.common.CommonMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

try:  # optional prod dependency
    import whitenoise  # noqa: F401

    MIDDLEWARE.insert(
        1, "whitenoise.middleware.WhiteNoiseMiddleware"
    )
    _HAS_WHITENOISE = True
except ImportError:
    _HAS_WHITENOISE = False

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "dashboard.context_processors.device_search",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# Database: PostgreSQL when configured (production), else SQLite (dev/POC).
if os.environ.get("POSTGRES_HOST"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("POSTGRES_DB", "iotdash"),
            "USER": os.environ.get("POSTGRES_USER", "iotdash"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
            "HOST": os.environ.get("POSTGRES_HOST"),
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
            "CONN_MAX_AGE": _env_int("DB_CONN_MAX_AGE", 60),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("DJANGO_TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
# Manifest/compressed storage only in production - it needs collectstatic to
# have run, which would break the dev runserver's {% static %} resolution.
if _HAS_WHITENOISE and not DEBUG:
    STORAGES = {
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
        },
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Cache: Redis when REDIS_URL is set (production, shared across workers), else
# in-process memory (dev/POC).
_CACHE_TIMEOUT = _env_int("DASHBOARD_CACHE_SECONDS", 300)
REDIS_URL = os.environ.get("REDIS_URL", "")
if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
            "TIMEOUT": _CACHE_TIMEOUT,
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "TIMEOUT": _CACHE_TIMEOUT,
        }
    }

# ---------------------------------------------------------------------------
# Event pipeline (ingest -> store -> query). Events come only from the DB,
# written by the eStreamer ingester. Retention enforced by the purge task.
# ---------------------------------------------------------------------------
RETENTION_THREAT_DAYS = _env_int("RETENTION_THREAT_DAYS", 90)
RETENTION_CONNECTION_DAYS = _env_int("RETENTION_CONNECTION_DAYS", 14)

# Days of raw events the dashboard queries for its 7-day widgets.
EVENT_WINDOW_DAYS = _env_int("EVENT_WINDOW_DAYS", 7)

# ---------------------------------------------------------------------------
# Celery (background polling, hourly rollups, retention purge)
# ---------------------------------------------------------------------------
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", REDIS_URL or "memory://")
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", REDIS_URL or None)
CELERY_TASK_ALWAYS_EAGER = _env_bool("CELERY_TASK_ALWAYS_EAGER", False)
CELERY_TIMEZONE = TIME_ZONE
CELERY_BEAT_SCHEDULE_MINUTES = {
    "ise_poll": _env_int("POLL_ISE_MINUTES", 15),
    "config_poll": _env_int("POLL_CONFIG_MINUTES", 15),
    "rollup": _env_int("ROLLUP_MINUTES", 60),
    "purge": _env_int("PURGE_MINUTES", 720),
}

# Behind a reverse proxy (nginx) terminating TLS.
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    CSRF_TRUSTED_ORIGINS = [
        o.strip()
        for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
        if o.strip()
    ]

# ---------------------------------------------------------------------------
# Cisco ISE configuration
# ---------------------------------------------------------------------------
ISE = {
    "ENABLED": _env_bool("ISE_ENABLED", True),
    # Host only, e.g. "sandboxdnac.cisco.com" or "10.10.20.70" (no scheme).
    "HOST": os.environ.get("ISE_HOST", ""),
    "ERS_PORT": _env_int("ISE_ERS_PORT", 9060),
    "USERNAME": os.environ.get("ISE_USERNAME", ""),
    "PASSWORD": os.environ.get("ISE_PASSWORD", ""),
    "VERIFY_TLS": _env_bool("ISE_VERIFY_TLS", False),
    "TIMEOUT": _env_int("ISE_TIMEOUT", 30),
    # How many endpoints to pull full attribute detail for (per-endpoint GETs).
    "DETAIL_LIMIT": _env_int("ISE_DETAIL_LIMIT", 50),
    "PAGE_SIZE": _env_int("ISE_PAGE_SIZE", 100),
    "MAX_PAGES": _env_int("ISE_MAX_PAGES", 20),
    # Names of the ISE endpoint custom attributes that carry site/location and
    # device type (the "Location" / "Device Type" columns in ISE Context
    # Visibility). Override if your org named them differently.
    "SITE_ATTR": os.environ.get("ISE_SITE_ATTR", "Location"),
    "DEVICE_TYPE_ATTR": os.environ.get("ISE_DEVICE_TYPE_ATTR", "Device Type"),
}

# ---------------------------------------------------------------------------
# Cisco FMC configuration
# ---------------------------------------------------------------------------
FMC = {
    "ENABLED": _env_bool("FMC_ENABLED", True),
    "HOST": os.environ.get("FMC_HOST", ""),
    "PORT": _env_int("FMC_PORT", 443),
    "USERNAME": os.environ.get("FMC_USERNAME", ""),
    "PASSWORD": os.environ.get("FMC_PASSWORD", ""),
    "VERIFY_TLS": _env_bool("FMC_VERIFY_TLS", False),
    "TIMEOUT": _env_int("FMC_TIMEOUT", 30),
    # Leave blank to auto-select the Global domain from the auth response.
    "DOMAIN_UUID": os.environ.get("FMC_DOMAIN_UUID", ""),
    "PAGE_LIMIT": _env_int("FMC_PAGE_LIMIT", 100),
    "MAX_PAGES": _env_int("FMC_MAX_PAGES", 20),
}

# Silence the InsecureRequestWarning noise when VERIFY_TLS is off (sandbox use).
if not ISE["VERIFY_TLS"] or not FMC["VERIFY_TLS"]:
    try:
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:  # pragma: no cover - urllib3 always present with requests
        pass
