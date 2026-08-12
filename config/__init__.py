# Load the Celery app when available. Guarded so the POC (no Celery installed)
# still runs manage.py / runserver normally.
try:
    from .celery import app as celery_app  # noqa: F401

    __all__ = ("celery_app",)
except Exception:  # celery not installed (dev/POC) or broker misconfigured
    pass
