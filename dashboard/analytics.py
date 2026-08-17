"""
Dashboard analytics over **real** ingested FMC events.

Every figure comes from the database: ``SecurityEvent`` rows written by the
eStreamer ingester (``dashboard/estreamer``), each stamped at ingest with the
device's ISE identity (device_type / site / in_ise) from the ``IoTDevice``
inventory. There is no synthetic/simulated data.

The analytics operate on event *dicts* (from ``event_store``) so the query
surface stays simple; heavy deployments can push the hot aggregations down to
``HourlyAggregate`` (populated by the rollup task).
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from dashboard import event_store

SEVERITY_ORDER = ["Critical", "High", "Medium", "Low", "Informational"]
EVENT_WINDOW_DAYS = 7


def _events():
    """All recent events as analytics-ready dicts (from the DB)."""
    return event_store.recent_events_as_dicts(days=EVENT_WINDOW_DAYS)


# --------------------------------------------------------------------------- #
# Filtering (site + device type + time window)
# --------------------------------------------------------------------------- #
SITES_ALL = "All"


def _apply_filters(events, site=None, device_type=None):
    out = events
    if site and site != SITES_ALL:
        out = [e for e in out if e.get("site") == site]
    if device_type and device_type != SITES_ALL:
        out = [e for e in out if e.get("device_type") == device_type]
    return out


def _window(hours):
    now = timezone.now()
    if hours <= 24:
        keys = [
            (now - timedelta(hours=h)).strftime("%Y-%m-%d %H:00")
            for h in range(hours - 1, -1, -1)
        ]
        return "hour", keys, (lambda e: e["hour"])
    days = max(1, hours // 24)
    keys = [
        (now - timedelta(days=d)).strftime("%Y-%m-%d") for d in range(days - 1, -1, -1)
    ]
    return "day", keys, (lambda e: e["timestamp"][:10])


def _scoped(events, hours=None, site=None, device_type=None):
    ev = _apply_filters(events, site, device_type)
    if hours:
        _, keys, keyfn = _window(hours)
        keyset = set(keys)
        ev = [e for e in ev if keyfn(e) in keyset]
    return ev


# --------------------------------------------------------------------------- #
# DB-side aggregation. At millions of events we must NOT pull rows into Python;
# these build a filtered queryset and let Postgres do the counting/summing so a
# dashboard widget costs one GROUP BY, not a full-table load.
# --------------------------------------------------------------------------- #
def _base_qs(hours=None, site=None, device_type=None):
    """Filtered SecurityEvent queryset (time window + site + device type)."""
    from dashboard.models import SecurityEvent

    window_h = hours if hours else EVENT_WINDOW_DAYS * 24
    qs = SecurityEvent.objects.filter(
        ts__gte=timezone.now() - timedelta(hours=window_h))
    if site and site != SITES_ALL:
        qs = qs.filter(site=site)
    if device_type and device_type != SITES_ALL:
        qs = qs.filter(device_type=device_type)
    return qs


# --------------------------------------------------------------------------- #
# Datasets + widgets
# --------------------------------------------------------------------------- #
def all_events(limit=2000):
    """Most-recent events (capped) for the table view. The detail/table API
    paginates; never return the full multi-million-row set to Python."""
    return [event_store._to_dict(e) for e in _base_qs().order_by("-ts")[:limit]]


def sites():
    """Distinct sites for the filter. Raw ISE location strings can differ only
    by trailing/leading spaces or casing, which SQL DISTINCT keeps as separate
    rows and makes the SAME site appear multiple times in the dropdown — so we
    dedupe on a normalized (trimmed, case-folded) key here."""
    seen, out = set(), []
    for s in _base_qs().values_list("site", flat=True).distinct():
        s = (s or "").strip()
        if not s:
            continue
        key = s.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return sorted(out)


_BLOCK = ("Blocked", "Would Block")


def devices_at_risk(hours=None, site=None, device_type=None):
    from django.contrib.postgres.aggregates import BoolOr, StringAgg
    from django.db.models import Count, Max, Q

    rows = (
        _base_qs(hours, site, device_type)
        .exclude(event_type="Connection")
        .values("device_mac")
        .annotate(
            event_count=Count("id"),
            blocked=Count("id", filter=Q(action__in=_BLOCK)),
            device_ip=Max("device_ip"),
            hostname=Max("hostname"),
            dev_type=Max("device_type"),
            site_=Max("site"),
            in_ise=BoolOr("in_ise"),
            threat_types=StringAgg("event_type", delimiter=", ", distinct=True),
            crit=Count("id", filter=Q(severity="Critical")),
            high=Count("id", filter=Q(severity="High")),
            med=Count("id", filter=Q(severity="Medium")),
            low=Count("id", filter=Q(severity="Low")),
            info=Count("id", filter=Q(severity="Informational")),
        )
    )
    out = []
    for r in rows:
        counts = {"Critical": r["crit"], "High": r["high"], "Medium": r["med"],
                  "Low": r["low"], "Informational": r["info"]}
        highest = next((s for s in SEVERITY_ORDER if counts[s]), "Informational")
        out.append({
            "device_mac": r["device_mac"],
            "device_ip": r["device_ip"] or "",
            "hostname": r["hostname"] or "",
            "device_type": r["dev_type"] or "",
            "site": r["site_"] or "",
            "event_count": r["event_count"],
            "blocked": r["blocked"],
            "highest_severity": highest,
            "threat_types": r["threat_types"] or "",
            "ise_correlated": "Yes" if r["in_ise"] else "No (FMC-only)",
        })
    out.sort(key=lambda r: (SEVERITY_ORDER.index(r["highest_severity"]),
                            -r["event_count"]))
    return out


def attack_severity(hours=None, site=None, device_type=None):
    from django.db.models import Count

    rows = (_base_qs(hours, site, device_type)
            .exclude(event_type="Connection")
            .values("severity").annotate(count=Count("id")))
    by = {r["severity"]: r["count"] for r in rows}
    return [{"severity": s, "count": by.get(s, 0)} for s in SEVERITY_ORDER]


def trend(hours=24, site=None, device_type=None):
    from django.db.models import Count, Q, Sum

    gran, keys, _ = _window(hours)
    # Group by the pre-computed hourly bucket column (UTC string, matches keys);
    # fold to day in Python for the day granularity (few buckets).
    grouped = (_base_qs(hours, site, device_type)
               .values("hour")
               .annotate(
                   bytes_=Sum("total_bytes"),
                   threats=Count("id", filter=~Q(event_type="Connection")),
                   blocked=Count("id", filter=Q(action__in=_BLOCK)),
                   allowed=Count("id", filter=~Q(action__in=_BLOCK)),
               ))
    agg = {k: {"traffic_mb": 0.0, "threats": 0, "blocked": 0, "allowed": 0}
           for k in keys}
    for g in grouped:
        key = (g["hour"] or "") if gran == "hour" else (g["hour"] or "")[:10]
        b = agg.get(key)
        if not b:
            continue
        b["traffic_mb"] += (g["bytes_"] or 0) / 1_000_000
        b["threats"] += g["threats"]
        b["blocked"] += g["blocked"]
        b["allowed"] += g["allowed"]
    points = []
    for k in keys:
        b = agg[k]
        points.append({
            "label": k,
            "traffic_mb": round(b["traffic_mb"], 1),
            "threats": b["threats"], "blocked": b["blocked"], "allowed": b["allowed"],
            "display": k[11:16] if gran == "hour" else k[5:],
        })
    return {"granularity": gran, "points": points}


def by_device_type(hours=None, site=None):
    from django.db.models import Count, Q, Sum

    rows = (_base_qs(hours, site)
            .values("device_type")
            .annotate(
                devices=Count("device_mac", distinct=True),
                events=Count("id"),
                threats=Count("id", filter=~Q(event_type="Connection")),
                critical=Count("id", filter=Q(severity="Critical")
                               & ~Q(event_type="Connection")),
                blocked=Count("id", filter=Q(action__in=_BLOCK)),
                bytes_=Sum("total_bytes"),
            ))
    out = []
    for r in rows:
        events = r["events"] or 0
        blocked = r["blocked"] or 0
        out.append({
            "device_type": r["device_type"] or "(unclassified)",
            "devices": r["devices"] or 0,
            "events": events,
            "threats": r["threats"] or 0,
            "critical": r["critical"] or 0,
            "blocked": blocked,
            "traffic_mb": round((r["bytes_"] or 0) / 1_000_000, 1),
            "pct_blocked": round(100 * blocked / events) if events else 0,
        })
    out.sort(key=lambda r: (-r["threats"], -r["traffic_mb"]))
    return out


def ise_type_counts():
    """Onboarded IoT-device count per device type, from the ISE inventory
    (IoTDevice) — the authoritative 'how many of this type exist', as opposed to
    by_device_type()'s 'how many were seen active in FMC events'."""
    from django.db.models import Count

    from dashboard.models import IoTDevice

    return {r["device_type"]: r["n"] for r in
            IoTDevice.objects.values("device_type").annotate(n=Count("id"))}


def insecure_transfers(limit=1000):
    return [event_store._to_dict(e) for e in _base_qs()
            .filter(insecure_protocol=True).order_by("-ts")[:limit]]


def outside_zone(limit=1000):
    return [event_store._to_dict(e) for e in _base_qs()
            .filter(zone_violation=True).order_by("-ts")[:limit]]


def summary(hours=None, site=None, device_type=None):
    from django.db.models import Count, Q

    agg = _base_qs(hours, site, device_type).aggregate(
        total_events=Count("id"),
        threat_events=Count("id", filter=~Q(event_type="Connection")),
        blocked=Count("id", filter=Q(action__in=_BLOCK)),
        devices_at_risk=Count("device_mac", distinct=True,
                              filter=~Q(event_type="Connection")),
        critical=Count("id", filter=Q(severity="Critical")
                       & ~Q(event_type="Connection")),
    )
    return {k: (v or 0) for k, v in agg.items()}


# --------------------------------------------------------------------------- #
# ISE <-> FMC correlation (REAL): FMC-seen devices vs the ISE inventory
# --------------------------------------------------------------------------- #
def device_inventory():
    from django.contrib.postgres.aggregates import BoolOr
    from django.db.models import Count, Max

    rows = (_base_qs()
            .values("device_mac")
            .annotate(event_count=Count("id"), ip=Max("device_ip"),
                      device_type=Max("device_type"), hostname=Max("hostname"),
                      site=Max("site"), in_ise=BoolOr("in_ise")))
    return [{"mac": r["device_mac"], "ip": r["ip"] or "",
             "device_type": r["device_type"] or "", "hostname": r["hostname"] or "",
             "site": r["site"] or "", "in_ise": bool(r["in_ise"]),
             "event_count": r["event_count"]} for r in rows]


def correlate_to_ise():
    """Map every device seen in FMC events to the ISE endpoint inventory by MAC.

    Matched  = the MAC is enrolled in ISE (identity group + profile shown).
    Unmatched (FMC-only) = seen by FMC but NOT in ISE - i.e. a shadow / unmanaged
    device, which is itself a useful security finding.
    """
    from dashboard.models import IoTDevice

    ise_map = {d.mac: d for d in IoTDevice.objects.all()}
    rows = []
    for d in device_inventory():
        ise = ise_map.get(d["mac"])
        matched = ise is not None
        rows.append(
            {
                "fmc_mac": d["mac"],
                "fmc_ip": d["ip"],
                "device_type": (ise.device_type if matched else d["device_type"]) or "",
                "fmc_hostname": d["hostname"],
                "site": (ise.site if matched else d["site"]) or "",
                "fmc_events": d["event_count"],
                "correlation": "Matched" if matched else "Unmatched (FMC-only)",
                "ise_endpoint_mac": ise.mac if matched else "",
                "ise_identity_group": ise.ise_identity_group if matched else "",
                "ise_profile": ise.ise_profile if matched else "",
            }
        )
    rows.sort(key=lambda r: (r["correlation"] != "Matched", -r["fmc_events"]))
    return rows


def correlation_summary():
    """Distinct FMC-seen devices vs the ISE inventory — counted in the DB, no
    per-event scan."""
    from dashboard.models import IoTDevice

    fmc_macs = {m.upper() for m in _base_qs()
                .values_list("device_mac", flat=True).distinct() if m}
    ise_macs = {m.upper() for m in
                IoTDevice.objects.values_list("mac", flat=True) if m}
    total = len(fmc_macs)
    matched = len(fmc_macs & ise_macs)
    return {
        "total": total,
        "matched": matched,
        "unmatched": total - matched,
        "match_rate": round(100 * matched / total) if total else 0,
    }


# --------------------------------------------------------------------------- #
# Device 360
# --------------------------------------------------------------------------- #
def device_360(mac):
    from collections import Counter
    from dashboard.models import IoTDevice

    from dashboard.models import SecurityEvent

    cutoff = timezone.now() - timedelta(days=EVENT_WINDOW_DAYS)
    events = [event_store._to_dict(e) for e in SecurityEvent.objects
              .filter(device_mac=mac, ts__gte=cutoff).order_by("ts")]
    if not events:
        return {"found": False, "mac": mac}

    threats = [e for e in events if e["event_type"] != "Connection"]
    ev_sorted = sorted(events, key=lambda e: e["_ts"])
    first = ev_sorted[0]

    ise = IoTDevice.objects.filter(mac=mac).first()
    identity = {
        "mac": mac, "ip": first.get("device_ip", ""),
        "hostname": (ise.hostname if ise else first.get("hostname", "")),
        "device_type": (ise.device_type if ise else first.get("device_type", "")),
        "site": (ise.site if ise else first.get("site", "")),
        "location": first.get("location", ""),
    }
    ise_info = {
        "correlation": "Matched" if ise else "Unmatched (FMC-only)",
        "ise_mac": ise.mac if ise else "",
        "identity_group": (ise.ise_identity_group if ise else "") or "—",
        "profile": (ise.ise_profile if ise else "") or "—",
        "quarantined": bool(ise and "block" in (ise.ise_identity_group or "").lower()),
    }

    sev_counts = {s: 0 for s in SEVERITY_ORDER}
    for e in threats:
        sev_counts[e["severity"]] += 1
    highest = next((s for s in SEVERITY_ORDER if sev_counts[s] > 0), "Informational")

    now = timezone.now()
    day_keys = [(now - timedelta(days=d)).strftime("%Y-%m-%d") for d in range(6, -1, -1)]
    daily = {d: {"day": d, "threats": 0, "traffic_mb": 0.0} for d in day_keys}
    for e in events:
        day = e["timestamp"][:10]
        if day in daily:
            if e["event_type"] != "Connection":
                daily[day]["threats"] += 1
            daily[day]["traffic_mb"] += (e.get("total_bytes") or 0) / 1_000_000
    for d in daily.values():
        d["traffic_mb"] = round(d["traffic_mb"], 1)

    sent = sum(e.get("bytes_sent") or 0 for e in events)
    recv = sum(e.get("bytes_received") or 0 for e in events)
    insecure_apps = sorted({e["application"] for e in events if e.get("insecure_protocol")})

    risk = {
        "highest_severity": highest, "total_events": len(events),
        "threat_events": len(threats),
        "blocked": sum(1 for e in events if e["action"] in ("Blocked", "Would Block")),
        "first_seen": first["timestamp"], "last_seen": ev_sorted[-1]["timestamp"],
        "threat_types": ", ".join(sorted({e["event_type"] for e in threats})),
    }
    policy = {
        "rules": sorted({e.get("rule_matched", "") for e in events if e.get("rule_matched")}),
        "ips": sorted({e.get("ips_policy", "") for e in events if e.get("ips_policy")}),
        "zones": sorted({e.get("src_zone", "") for e in events if e.get("src_zone")}
                        | {e.get("dst_zone", "") for e in events if e.get("dst_zone")}),
        "firewalls": sorted({e.get("firewall", "") for e in events if e.get("firewall")}),
    }
    flags = {
        "insecure_protocol": bool(insecure_apps),
        "insecure_apps": ", ".join(insecure_apps),
        "zone_violation": any(e.get("zone_violation") for e in events),
    }

    recs = []
    if sev_counts["Critical"] or sev_counts["High"]:
        recs.append(f"Investigate: {sev_counts['Critical']} critical / "
                    f"{sev_counts['High']} high-severity threat events on this device.")
    if flags["insecure_protocol"]:
        recs.append(f"Device communicates over insecure protocol(s): "
                    f"{flags['insecure_apps']} - enforce a secure alternative.")
    if flags["zone_violation"]:
        recs.append("Blocked cross-zone traffic seen - verify network segmentation.")
    if not ise:
        recs.append("Not enrolled in ISE (FMC-only) - onboard this device for identity.")
    recs.append("Optionally quarantine via ISE ANC (requires a write-capable ISE account).")

    return {
        "found": True, "identity": identity, "ise": ise_info, "risk": risk,
        "severity": [{"severity": s, "count": sev_counts[s]} for s in SEVERITY_ORDER],
        "daily": list(daily.values()), "events": events,
        "top_countries": Counter(e.get("dest_country") for e in events if e.get("dest_country")).most_common(5),
        "top_apps": Counter(e.get("application") for e in events if e.get("application")).most_common(5),
        "traffic": {"sent_mb": round(sent / 1_000_000, 1),
                    "recv_mb": round(recv / 1_000_000, 1),
                    "total_mb": round((sent + recv) / 1_000_000, 1)},
        "policy": policy, "flags": flags, "recommendations": recs,
    }
