"""
Celery tasks for the production pipeline.

ISE inventory is IoT-scoped and split into two cadences:

* refresh_ise_reference - DAILY. Resolve the IoT profiling-policy names in the
                          allow-list to profileIds, and rebuild the NAD ->
                          Location map. Slow-moving reference data, cached in
                          Redis for the hourly sync to consume.
* sync_iot_endpoints    - HOURLY. Fetch only the allow-listed IoT endpoints
                          (ISE server-side filter), enrich each with device type
                          (mfcAttributes) and site (session -> NAS -> NAD
                          Location), and upsert into IoTDevice. This is the
                          source of truth the eStreamer ingester stamps events
                          against.

Also:
* refresh_fmc_config - warm the FMC config caches the dashboard reads.
* rollup_hourly      - recompute HourlyAggregate from raw events.
* purge_retention    - drop raw events past their retention window.

Events themselves are written by the eStreamer ingester (a long-running
process, not a Celery task) - see dashboard/estreamer.
"""
from __future__ import annotations

import ipaddress
from concurrent.futures import ThreadPoolExecutor, as_completed

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


# Cache keys for the daily reference data.
_CK_PROFILE_MAP = "ise:iot_profile_map"      # {profileId: name}
_CK_NAD_LOCATION = "ise:nad_location_map"     # {nas_ip: site}
_REFERENCE_TTL = 60 * 60 * 30                 # 30h - survives a missed daily run


# --------------------------------------------------------------------------- #
# Daily reference data
# --------------------------------------------------------------------------- #
@shared_task(name="dashboard.tasks.refresh_ise_reference")
def refresh_ise_reference() -> dict:
    """Resolve IoT profile ids + rebuild the NAD->Location map, cached for the
    hourly sync. Runs daily (reference data changes rarely)."""
    from django.conf import settings
    from django.core.cache import cache
    from dashboard import services

    try:
        ise = services.get_ise_client()
    except Exception as exc:
        return {"error": str(exc)}

    result = {}
    # Profile name -> id resolution (only needed when NOT using logical profiles,
    # but we resolve anyway so device-type fallback can name a profileId).
    try:
        profile_map = ise.resolve_profile_ids(settings.ISE["IOT_PROFILES"])
        cache.set(_CK_PROFILE_MAP, profile_map, _REFERENCE_TTL)
        result["iot_profiles_resolved"] = len(profile_map)
    except Exception as exc:
        result["profile_error"] = str(exc)

    if settings.ISE["LOCATION_METHOD"] == "session":
        try:
            nad_map = ise.nad_location_map()
            cache.set(_CK_NAD_LOCATION, nad_map, _REFERENCE_TTL)
            result["nad_locations"] = len(nad_map)
        except Exception as exc:
            result["nad_error"] = str(exc)
    return result


# --------------------------------------------------------------------------- #
# Hourly IoT endpoint sync
# --------------------------------------------------------------------------- #
@shared_task(name="dashboard.tasks.sync_iot_endpoints")
def sync_iot_endpoints() -> dict:
    """Sync the IoT endpoint inventory (allow-listed profiles only) into
    IoTDevice, with device type + site. Source of truth for event enrichment."""
    from django.conf import settings
    from django.core.cache import cache
    from django.utils import timezone
    from dashboard import services
    from dashboard.models import IoTDevice

    cfg = settings.ISE
    try:
        ise = services.get_ise_client()
    except Exception as exc:
        return {"error": str(exc)}

    # Reference data (fall back to computing inline if the daily run hasn't fired).
    profile_map = cache.get(_CK_PROFILE_MAP)
    if profile_map is None:
        profile_map = ise.resolve_profile_ids(cfg["IOT_PROFILES"])
        cache.set(_CK_PROFILE_MAP, profile_map, _REFERENCE_TTL)
    nad_map = cache.get(_CK_NAD_LOCATION) or {}

    subnets = _parse_site_subnets(cfg["SITE_SUBNETS"])
    method = cfg["LOCATION_METHOD"]
    workers = cfg["SYNC_WORKERS"]
    profile_ids = list(profile_map.keys())

    # 1. Discover IoT endpoints. Open API (primary): deviceType/vendor/ipAddress
    #    inline. ERS (fallback): light refs then per-endpoint detail.
    if cfg["USE_OPENAPI"]:
        try:
            objs = ise.openapi_iot_endpoints(profile_ids)
        except Exception as exc:
            return {"error": f"Open API endpoint fetch failed: {exc}"}
        base_rows = [ise.map_openapi_endpoint(o, profile_map) for o in objs.values()]
    else:
        logical = cfg.get("IOT_LOGICAL_PROFILES") or []
        try:
            refs = (ise.iot_endpoint_refs(logical_profiles=logical) if logical
                    else ise.iot_endpoint_refs(profile_ids=profile_ids))
        except Exception as exc:
            return {"error": f"ERS endpoint fetch failed: {exc}"}
        base_rows = None
        _refs = list(refs.values())
    if cfg["USE_OPENAPI"] and not base_rows:
        return {"iot_endpoints": 0, "note": "no endpoints matched the allow-list"}
    if not cfg["USE_OPENAPI"] and not _refs:
        return {"iot_endpoints": 0, "note": "no endpoints matched the allow-list"}

    # 2. Per-endpoint: (ERS-path) enrich detail; optional ERS device-type
    #    backfill; resolve site. Parallelised.
    ers_enrich = cfg["ERS_ENRICH"]

    def build_openapi(row):
        # Open API deviceType is blank here, so backfill the device type from
        # ERS mfcAttributes (by id, falling back to MAC lookup).
        if ers_enrich and not row["device_type"]:
            try:
                full = (ise.endpoint_detail(row["endpoint_id"]) if row.get("endpoint_id")
                        else {}) or ise.endpoint_detail_by_mac(row["mac"])
                if not ise.mfc_device_type(full):
                    full = ise.endpoint_detail_by_mac(row["mac"]) or full
                row["device_type"] = ise.mfc_device_type(full) or row["device_type"]
            except Exception:
                pass
        _resolve_site(ise, row, method, nad_map, subnets)
        return row

    def build_ers(ref):
        row = ise.enrich_endpoint(ref, profile_map)
        _resolve_site(ise, row, method, nad_map, subnets)
        return row

    if cfg["USE_OPENAPI"]:
        work, fn = base_rows, build_openapi
    else:
        work, fn = _refs, build_ers

    rows = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for fut in as_completed([pool.submit(fn, w) for w in work]):
            try:
                rows.append(fut.result())
            except Exception:
                continue

    # 4. Upsert.
    now = timezone.now()
    n = 0
    seen = set()
    for r in rows:
        mac = (r.get("mac") or "").upper()
        if not mac:
            continue
        seen.add(mac)
        IoTDevice.objects.update_or_create(
            mac=mac,
            defaults={
                "device_type": r.get("device_type", ""),
                "site": r.get("site", ""),
                "ip": r.get("ip") or None,
                "ise_profile": r.get("endpoint_profile", ""),
                "ise_identity_group": r.get("logical_profile", ""),
                "correlation": "Matched",
                "ise_endpoint_mac": mac,
                "last_seen": now,
            },
        )
        n += 1

    services.ise_endpoint_count(use_cache=False)
    return {"iot_endpoints": n, "with_site": sum(1 for r in rows if r.get("site"))}


# --------------------------------------------------------------------------- #
# Location helpers (subnet method)
# --------------------------------------------------------------------------- #
def _resolve_site(ise, row, method, nad_map, subnets):
    """Fill row['site'] (and row['ip'] if empty) per the location method.
    subnet: uses the endpoint IP (Open API gives it inline; else a session
    lookup). session: MnT session -> NAS IP -> NAD Location map."""
    if method == "off":
        return
    if method == "subnet":
        if not row.get("ip"):
            row["ip"] = ise.session_by_mac(row["mac"]).get("framed_ip_address", "")
        row["site"] = _site_for_ip(row.get("ip", ""), subnets)
        return
    if method == "session":
        sess = ise.session_by_mac(row["mac"])
        row["ip"] = row.get("ip") or sess.get("framed_ip_address", "")
        # Match the NAD by IP, then by name (sessions report one or the other).
        for k in ("nas_ip_address", "nas_ip", "network_device_name",
                  "nas_identifier", "acs_server"):
            site = nad_map.get(sess.get(k, ""))
            if site:
                row["site"] = site
                break


def _parse_site_subnets(spec: str):
    """Parse 'Mumbai=10.59.0.0/16;PUNE=10.22.0.0/16' -> [(net, site), ...]."""
    out = []
    for part in (spec or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        site, cidr = part.split("=", 1)
        try:
            out.append((ipaddress.ip_network(cidr.strip(), strict=False), site.strip()))
        except ValueError:
            continue
    return out


def _site_for_ip(ip: str, subnets) -> str:
    if not ip:
        return ""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ""
    for net, site in subnets:
        if addr in net:
            return site
    return ""


# --------------------------------------------------------------------------- #
# FMC + maintenance
# --------------------------------------------------------------------------- #
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
