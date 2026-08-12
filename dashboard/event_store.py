"""
Database-backed event store. Bridges the ``SecurityEvent`` model and the event
*dicts* the dashboard analytics consume, so the DB backend is a drop-in for the
in-memory sample feed. Also holds the hourly-rollup and retention-purge logic
used by the Celery tasks.
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone


# Fields carried on both the model and the analytics dict.
_FIELDS = [
    "event_type", "severity", "impact", "device_mac", "device_type", "hostname",
    "site", "location", "in_ise", "mapped_ise_mac", "dest_country", "application",
    "protocol", "port", "insecure_protocol", "bytes_sent", "bytes_received",
    "total_bytes", "action", "rule_matched", "ips_policy", "src_zone", "dst_zone",
    "firewall", "threat_name", "threat_category", "classtype", "zone_violation",
]


def _to_dict(ev) -> dict:
    d = {f: getattr(ev, f) for f in _FIELDS}
    d["device_ip"] = ev.device_ip or ""
    d["source_ip"] = ev.source_ip or ""
    d["dest_ip"] = ev.dest_ip or ""
    d["timestamp"] = ev.ts.strftime("%Y-%m-%d %H:%M:%S")
    d["hour"] = ev.hour
    d["_ts"] = ev.ts
    return d


def recent_events_as_dicts(days: int = 7) -> list:
    """Events from the last ``days`` days as analytics-ready dicts."""
    from dashboard.models import SecurityEvent

    cutoff = timezone.now() - timedelta(days=days)
    qs = SecurityEvent.objects.filter(ts__gte=cutoff).order_by("-ts")
    return [_to_dict(e) for e in qs.iterator()]


# --------------------------------------------------------------------------- #
# ISE identity enrichment (stamped onto each event at ingest)
# --------------------------------------------------------------------------- #
def ise_identity_map() -> dict:
    """{MAC -> IoTDevice} from the ISE-derived inventory, for event enrichment."""
    from dashboard.models import IoTDevice

    return {d.mac: d for d in IoTDevice.objects.all()}


def enrich_with_ise(event: dict, ise_map: dict) -> dict:
    """Stamp ISE identity (device_type / site / in_ise) onto an event in place,
    keyed by the device MAC. Unknown MACs are flagged FMC-only (in_ise=False)."""
    ise = ise_map.get((event.get("device_mac") or "").upper())
    if ise:
        event["in_ise"] = True
        event["device_type"] = event.get("device_type") or ise.device_type
        event["site"] = event.get("site") or ise.site
        if not event.get("hostname"):
            event["hostname"] = ise.hostname
    else:
        event["in_ise"] = False
        event.setdefault("device_type", "")
        event.setdefault("site", "")
    return event


def bulk_ingest(event_dicts: list, batch_size: int = 1000) -> int:
    """Persist a batch of event dicts (as produced by the analytics or an
    eNcore parser) into ``SecurityEvent``. Returns the count written."""
    from dashboard.models import SecurityEvent

    objs = []
    for e in event_dicts:
        objs.append(
            SecurityEvent(
                ts=e.get("_ts") or timezone.now(),
                hour=e.get("hour", ""),
                event_type=e.get("event_type", ""),
                severity=e.get("severity", ""),
                impact=e.get("impact", 0),
                device_mac=e.get("device_mac", ""),
                device_ip=_ip(e.get("device_ip")),
                device_type=e.get("device_type", ""),
                hostname=e.get("hostname", ""),
                site=e.get("site", ""),
                location=e.get("location", ""),
                in_ise=bool(e.get("in_ise")),
                mapped_ise_mac=e.get("mapped_ise_mac", ""),
                source_ip=_ip(e.get("source_ip")),
                dest_ip=_ip(e.get("dest_ip")),
                dest_country=e.get("dest_country", ""),
                application=e.get("application", ""),
                protocol=e.get("protocol", ""),
                port=e.get("port") or None,
                insecure_protocol=bool(e.get("insecure_protocol")),
                bytes_sent=e.get("bytes_sent", 0),
                bytes_received=e.get("bytes_received", 0),
                total_bytes=e.get("total_bytes", 0),
                action=e.get("action", ""),
                rule_matched=e.get("rule_matched", ""),
                ips_policy=e.get("ips_policy", ""),
                src_zone=e.get("src_zone", ""),
                dst_zone=e.get("dst_zone", ""),
                firewall=e.get("firewall", ""),
                threat_name=e.get("threat_name", ""),
                threat_category=e.get("threat_category", ""),
                classtype=e.get("classtype", ""),
                zone_violation=bool(e.get("zone_violation")),
            )
        )
    SecurityEvent.objects.bulk_create(objs, batch_size=batch_size)
    return len(objs)


def _ip(value):
    value = (value or "").strip() if isinstance(value, str) else value
    return value or None


# --------------------------------------------------------------------------- #
# Rollups & retention (called by Celery tasks)
# --------------------------------------------------------------------------- #
def rollup_hourly(hours_back: int = 26) -> int:
    """Recompute HourlyAggregate for the recent window from raw events."""
    from django.db.models import Count, Sum, Q
    from dashboard.models import HourlyAggregate, SecurityEvent

    cutoff = timezone.now() - timedelta(hours=hours_back)
    rows = (
        SecurityEvent.objects.filter(ts__gte=cutoff)
        .values("hour", "site", "device_type")
        .annotate(
            events=Count("id"),
            threats=Count("id", filter=~Q(event_type="Connection")),
            critical=Count("id", filter=Q(severity="Critical") & ~Q(event_type="Connection")),
            blocked=Count("id", filter=Q(action__in=["Blocked", "Would Block"])),
            allowed=Count("id", filter=~Q(action__in=["Blocked", "Would Block"])),
            total_bytes=Sum("total_bytes"),
            devices=Count("device_mac", distinct=True),
        )
    )
    n = 0
    for r in rows:
        HourlyAggregate.objects.update_or_create(
            hour=r["hour"], site=r["site"], device_type=r["device_type"],
            defaults={
                "events": r["events"], "threats": r["threats"],
                "critical": r["critical"], "blocked": r["blocked"],
                "allowed": r["allowed"], "total_bytes": r["total_bytes"] or 0,
                "devices": r["devices"],
            },
        )
        n += 1
    return n


def purge_old(threat_days: int, connection_days: int) -> dict:
    """Delete raw events past their retention window (aggregates are kept)."""
    from django.db.models import Q
    from dashboard.models import SecurityEvent

    now = timezone.now()
    conn_deleted = SecurityEvent.objects.filter(
        event_type="Connection", ts__lt=now - timedelta(days=connection_days)
    ).delete()[0]
    threat_deleted = SecurityEvent.objects.filter(
        ~Q(event_type="Connection"), ts__lt=now - timedelta(days=threat_days)
    ).delete()[0]
    return {"connection_deleted": conn_deleted, "threat_deleted": threat_deleted}
