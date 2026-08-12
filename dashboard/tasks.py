"""
Celery tasks for the production pipeline:

* poll_ise_inventory - pull ISE endpoints (identity + profile) into IoTDevice,
                       so ingested events can be stamped with device identity
                       and correlation is real.
* refresh_fmc_config - warm the FMC config caches the dashboard reads.
* rollup_hourly      - recompute HourlyAggregate from raw events.
* purge_retention    - drop raw events past their retention window.

Events themselves are written by the eStreamer ingester (a long-running
process, not a Celery task) - see dashboard/estreamer.
"""
from __future__ import annotations

try:
    from celery import shared_task
except Exception:  # allow import/use without celery installed
    def shared_task(*dargs, **dkwargs):
        def wrap(fn):
            fn.delay = fn
            return fn
        if dargs and callable(dargs[0]):
            return wrap(dargs[0])
        return wrap


@shared_task(name="dashboard.tasks.poll_ise_inventory")
def poll_ise_inventory() -> dict:
    """Refresh the IoTDevice inventory from ISE (endpoints + identity group +
    profiler profile). This is the source of truth the ingester uses to stamp
    each event with device_type / site / in_ise."""
    from django.utils import timezone
    from dashboard import services
    from dashboard.models import IoTDevice

    try:
        endpoints = services.get_ise_client().get_endpoints(detail=True)
    except Exception as exc:  # ISE unreachable / auth - leave inventory as-is
        return {"error": str(exc)}

    now = timezone.now()
    n = 0
    for e in endpoints:
        mac = (e.get("mac") or "").upper()
        if not mac:
            continue
        IoTDevice.objects.update_or_create(
            mac=mac,
            defaults={
                "ise_identity_group": e.get("identity_group", ""),
                "ise_profile": e.get("endpoint_profile", ""),
                "device_type": _device_type_from(e),
                "site": e.get("site", ""),
                "correlation": "Matched",
                "ise_endpoint_mac": mac,
                "last_seen": now,
            },
        )
        n += 1
    services.ise_endpoint_count(use_cache=False)
    return {"ise_devices": n}


def _device_type_from(endpoint: dict) -> str:
    """Derive a device *type* from ISE identity. Preference order:
    1. the org's "Device Type" custom attribute (ISE Context Visibility column),
    2. the profiler profile (e.g. 'Axis-Device', 'Cisco-IP-Phone'),
    3. the identity group."""
    attr = (endpoint.get("device_type_attr") or "").strip()
    if attr:
        return attr
    prof = (endpoint.get("endpoint_profile") or "").strip()
    if prof and prof.lower() not in ("unknown / unprofiled", "(detail not fetched)"):
        return prof
    return endpoint.get("identity_group", "") or ""


@shared_task(name="dashboard.tasks.refresh_fmc_config")
def refresh_fmc_config() -> dict:
    from dashboard import services

    keys = ["fmc-devices", "fmc-security-zones", "fmc-access-rules",
            "fmc-intrusion-policies", "fmc-file-policies", "fmc-network-objects"]
    for k in keys:
        services.fetch_dataset(k, use_cache=False)
    services.connection_status(use_cache=False)
    return {"refreshed": keys}


@shared_task(name="dashboard.tasks.rollup_hourly")
def rollup_hourly() -> dict:
    from dashboard import event_store

    return {"aggregates": event_store.rollup_hourly()}


@shared_task(name="dashboard.tasks.purge_retention")
def purge_retention() -> dict:
    from django.conf import settings
    from dashboard import event_store

    return event_store.purge_old(
        threat_days=settings.RETENTION_THREAT_DAYS,
        connection_days=settings.RETENTION_CONNECTION_DAYS,
    )
