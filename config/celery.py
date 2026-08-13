"""
Celery application + beat schedule for the production pipeline.
Imported lazily by ``config/__init__.py``.
"""
import os

from celery import Celery
from celery.schedules import schedule

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("iotdash")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks(["dashboard"])


@app.on_after_finalize.connect
def setup_periodic_tasks(sender, **kwargs):
    from django.conf import settings

    mins = settings.CELERY_BEAT_SCHEDULE_MINUTES
    sender.add_periodic_task(schedule(60 * mins["ise_reference"]),
                             app.signature("dashboard.tasks.refresh_ise_reference"),
                             name="refresh ISE reference (daily)")
    sender.add_periodic_task(schedule(60 * mins["iot_sync"]),
                             app.signature("dashboard.tasks.sync_iot_endpoints"),
                             name="sync IoT endpoints (hourly)")
    sender.add_periodic_task(schedule(60 * mins["config_poll"]),
                             app.signature("dashboard.tasks.refresh_fmc_config"),
                             name="refresh FMC config")
    sender.add_periodic_task(schedule(60 * mins["rollup"]),
                             app.signature("dashboard.tasks.rollup_hourly"),
                             name="hourly rollup")
    sender.add_periodic_task(schedule(60 * mins["purge"]),
                             app.signature("dashboard.tasks.purge_retention"),
                             name="purge retention")
