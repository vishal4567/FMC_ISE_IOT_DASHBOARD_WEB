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

# Database: PostgreSQL only. This is a production build - there is no SQLite
# fallback. POSTGRES_HOST must be set (see .env.prod / install_rhel9.sh).
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "iotdash"),
        "USER": os.environ.get("POSTGRES_USER", "iotdash"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
        "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": _env_int("DB_CONN_MAX_AGE", 60),
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
    # Daily: resolve IoT profile ids + rebuild the NAD->location map (slow-moving
    # reference data). 1440 = 24h.
    "ise_reference": _env_int("ISE_REFERENCE_MINUTES", 1440),
    # Hourly: sync the IoT endpoint inventory (device type + site) for the
    # allow-listed profiles only.
    "iot_sync": _env_int("IOT_SYNC_MINUTES", 60),
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
# Default IoT profiling policies to import (the org's CCTV / Access-Control /
# BMS logical-profile members). Override wholesale with ISE_IOT_PROFILES.
# All names below are verified present in the production ISE profiler catalogue
# (893 profiles). "EverFocus-Electronics-Corp" from the CCTV logical profile is
# NOT a profiler policy in this ISE, so it is omitted.
_DEFAULT_IOT_PROFILES = [
    # Wipro_CCTV
    "Axis-Device", "Axis-Network-Camera",
    "Mobotix-Camera", "MotorolaSolutions-Device", "Sony-Corporation-Devices",
    "SONY-TEKTRONIX-CORP-Devices", "VCS-Video-Communication-Devices",
    "Vivotek-Devices", "Vivotek-Devices-Camera",
    # Wipro-Access-Control
    "Access-Control", "Automated-Logic-Device", "Barco-Projection-System",
    "Bosch_Security_Systems", "ICOM-Device", "ICPDAS-ACS", "Spidernet_ACS",
    "WAVERIDER-ACS",
    # Wipro-BMS
    "Alteron-Devices", "Eliwell-Controls-Devices", "Texas-Instruments-Devices",
]

ISE = {
    "ENABLED": _env_bool("ISE_ENABLED", True),
    # Host only, e.g. "sandboxdnac.cisco.com" or "10.10.20.70" (no scheme).
    "HOST": os.environ.get("ISE_HOST", ""),
    "ERS_PORT": _env_int("ISE_ERS_PORT", 9060),
    "USERNAME": os.environ.get("ISE_USERNAME", ""),
    "PASSWORD": os.environ.get("ISE_PASSWORD", ""),
    "VERIFY_TLS": _env_bool("ISE_VERIFY_TLS", False),
    "TIMEOUT": _env_int("ISE_TIMEOUT", 30),
    "PAGE_SIZE": _env_int("ISE_PAGE_SIZE", 100),
    "MAX_PAGES": _env_int("ISE_MAX_PAGES", 200),
    # ---- IoT scoping -------------------------------------------------------
    # We import ONLY endpoints whose ISE profiling policy is in this allow-list
    # (the org's IoT device categories - e.g. the Wipro_CCTV / Access-Control /
    # BMS logical-profile members). Everything else (laptops, phones, servers)
    # is ignored. Comma-separated profiling-policy names; resolved to profileIds
    # by the daily reference task.
    "IOT_PROFILES": [
        p.strip()
        for p in os.environ.get("ISE_IOT_PROFILES", ",".join(_DEFAULT_IOT_PROFILES)).split(",")
        if p.strip()
    ],
    # Discovery path. Default is ERS: filter /endpoint by profileId, then read
    # each endpoint's mfcAttributes.mfcDeviceType for the device type (the ISE
    # Context Visibility "Device Type"). The Open API's deviceType field is NOT
    # populated in this deployment, so ERS is the reliable source.
    # Set True to discover via the Open API instead (still needs ERS_ENRICH for
    # the device type).
    "USE_OPENAPI": _env_bool("ISE_USE_OPENAPI", False),
    "OPENAPI_PAGE_SIZE": _env_int("ISE_OPENAPI_PAGE_SIZE", 500),
    # Open-API-path only: instead of a slow per-profile filter (ISE re-scans all
    # endpoints for each profileId), page through ALL endpoints once (cheap
    # unfiltered sequential paging) and keep those whose inline profileId is in
    # the allow-list. Better when the ISE profileId filter is slow. With this on,
    # use a LARGE page size and enough MAX_PAGES to cover the full endpoint count.
    "OPENAPI_SCAN_ALL": _env_bool("ISE_OPENAPI_SCAN_ALL", False),
    # Open-API-path only: backfill the (blank) device type from ERS
    # mfcAttributes. Ignored by the ERS path, which always reads mfcAttributes.
    "ERS_ENRICH": _env_bool("ISE_ERS_ENRICH", True),
    # ERS-path DISCOVERY BY IDENTITY GROUP (best at very large scale). Endpoint
    # group membership is indexed in ISE, so filter=groupId.EQ.<id> avoids the
    # full-table scan that profileId does. Set to the IoT endpoint identity group
    # names (e.g. Wipro_CCTV,BMS_Devices,Access-Control). Takes precedence over
    # logical/profileId discovery on the ERS path. Confirm it's fast with
    # `probe_apis` (ers_by_groupId timing) first.
    "IOT_GROUPS": [
        g.strip() for g in os.environ.get("ISE_IOT_GROUPS", "").split(",") if g.strip()
    ],
    # ERS-path only: filter by logicalProfileName instead of profileId. Both are
    # documented ERS endpoint filters; logical is fewer calls (3 vs 21). Blank ->
    # filter by profileId. Ignored when USE_OPENAPI is True.
    "IOT_LOGICAL_PROFILES": [
        p.strip()
        for p in os.environ.get("ISE_IOT_LOGICAL_PROFILES", "").split(",")
        if p.strip()
    ],
    # How the site/location per endpoint is derived:
    #   session - MnT session -> NAS IP/name -> NAD Location group. IP-independent
    #             (uses the switch/WLC, not the endpoint IP). This is the default.
    #   subnet  - map the endpoint IP to a site (only if IP is available)
    #   off     - don't resolve location
    "LOCATION_METHOD": os.environ.get("ISE_LOCATION_METHOD", "session").strip().lower(),
    # site=CIDR pairs for LOCATION_METHOD=subnet, e.g.
    #   "Mumbai=10.59.0.0/16;PUNE=10.22.0.0/16"
    "SITE_SUBNETS": os.environ.get("ISE_SITE_SUBNETS", ""),
    # Device-type custom attribute (last-resort fallback; primary source is
    # mfcAttributes.mfcDeviceType, then the profiling-policy name).
    "DEVICE_TYPE_ATTR": os.environ.get("ISE_DEVICE_TYPE_ATTR", "Device Type"),
    # Parallelism for per-endpoint detail / session lookups.
    "SYNC_WORKERS": _env_int("ISE_SYNC_WORKERS", 8),
}

# ---------------------------------------------------------------------------
# Cisco ISE Data Connect (read-only SQL over ISE's reporting DB)
#
# Recommended for bulk endpoint retrieval at large scale (millions of endpoints)
# where ERS/Open API filtering is too slow. When USE_FOR_DISCOVERY is on, the
# IoT sync discovers endpoints via one SQL query instead of REST paging.
# Confirm the schema/columns first with:  manage.py probe_dataconnect
# ---------------------------------------------------------------------------
DATACONNECT = {
    "ENABLED": _env_bool("ISE_DATACONNECT_ENABLED", False),
    "HOST": os.environ.get("ISE_DATACONNECT_HOST", ""),
    "PORT": _env_int("ISE_DATACONNECT_PORT", 2484),
    "SERVICE_NAME": os.environ.get("ISE_DATACONNECT_SERVICE", "cpm10"),
    "USER": os.environ.get("ISE_DATACONNECT_USER", "dataconnect"),
    "PASSWORD": os.environ.get("ISE_DATACONNECT_PASSWORD", ""),
    "VERIFY_TLS": _env_bool("ISE_DATACONNECT_VERIFY_TLS", False),
    "CA_CERT": os.environ.get("ISE_DATACONNECT_CA_CERT", ""),
    "TIMEOUT": _env_int("ISE_DATACONNECT_TIMEOUT", 60),
    # Use Data Connect as the IoT-endpoint discovery source for the sync.
    "USE_FOR_DISCOVERY": _env_bool("ISE_USE_DATACONNECT", False),
    # Schema mapping - defaults match the documented Data Connect views
    # (developer.cisco.com/docs/dataconnect/database-views). Endpoints:
    #   ENDPOINTS_DATA(MAC_ADDRESS, ENDPOINT_POLICY, IDENTITY_GROUP_ID, ENDPOINT_IP)
    "ENDPOINTS_VIEW": os.environ.get("ISE_DC_ENDPOINTS_VIEW", "endpoints_data"),
    "COL_MAC": os.environ.get("ISE_DC_COL_MAC", "mac_address"),
    "COL_PROFILE": os.environ.get("ISE_DC_COL_PROFILE", "endpoint_policy"),
    "COL_GROUP": os.environ.get("ISE_DC_COL_GROUP", "identity_group_id"),
    "COL_DEVICETYPE": os.environ.get("ISE_DC_COL_DEVICETYPE", ""),  # blank -> use profile
    "COL_IP": os.environ.get("ISE_DC_COL_IP", "endpoint_ip"),
    "COL_SITE": os.environ.get("ISE_DC_COL_SITE", ""),  # not in endpoints_data
    # Location via SQL from RADIUS_AUTHENTICATIONS(CALLING_STATION_ID -> LOCATION),
    # so we never walk NADs / call MnT. Set LOCATION empty to fall back to the
    # endpoint IP + SITE_SUBNETS instead.
    "LOCATION_VIEW": os.environ.get("ISE_DC_LOCATION_VIEW", "radius_authentications"),
    "COL_LOC_MAC": os.environ.get("ISE_DC_COL_LOC_MAC", "calling_station_id"),
    "COL_LOC_SITE": os.environ.get("ISE_DC_COL_LOC_SITE", "location"),
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
