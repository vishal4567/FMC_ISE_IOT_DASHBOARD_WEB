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
# Datasets + widgets
# --------------------------------------------------------------------------- #
def all_events():
    return _events()


def sites():
    return sorted({e.get("site") for e in _events() if e.get("site")})


def devices_at_risk(hours=None, site=None, device_type=None):
    agg = {}
    for e in _scoped(_events(), hours, site, device_type):
        if e["event_type"] == "Connection":
            continue
        key = e["device_mac"]
        d = agg.setdefault(
            key,
            {
                "device_mac": key,
                "device_ip": e.get("device_ip", ""),
                "hostname": e.get("hostname", ""),
                "device_type": e.get("device_type", ""),
                "site": e.get("site", ""),
                "in_ise": e.get("in_ise", False),
                "event_count": 0,
                "_worst": 5,
                "threat_types": set(),
                "blocked": 0,
            },
        )
        d["event_count"] += 1
        d["_worst"] = min(d["_worst"], SEVERITY_ORDER.index(e["severity"]) + 1)
        d["threat_types"].add(e["event_type"])
        if e["action"] in ("Blocked", "Would Block"):
            d["blocked"] += 1
    rows = []
    for d in agg.values():
        d["highest_severity"] = SEVERITY_ORDER[d.pop("_worst") - 1]
        d["threat_types"] = ", ".join(sorted(d["threat_types"]))
        d["ise_correlated"] = "Yes" if d.pop("in_ise") else "No (FMC-only)"
        rows.append(d)
    rows.sort(key=lambda r: (SEVERITY_ORDER.index(r["highest_severity"]),
                             -r["event_count"]))
    return rows


def attack_severity(hours=None, site=None, device_type=None):
    counts = {s: 0 for s in SEVERITY_ORDER}
    for e in _scoped(_events(), hours, site, device_type):
        if e["event_type"] == "Connection":
            continue
        counts[e["severity"]] += 1
    return [{"severity": s, "count": counts[s]} for s in SEVERITY_ORDER]


def trend(hours=24, site=None, device_type=None):
    gran, keys, keyfn = _window(hours)
    buckets = {
        k: {"label": k, "traffic_mb": 0.0, "threats": 0, "blocked": 0, "allowed": 0}
        for k in keys
    }
    for e in _apply_filters(_events(), site, device_type):
        b = buckets.get(keyfn(e))
        if not b:
            continue
        b["traffic_mb"] += (e.get("total_bytes") or 0) / 1_000_000
        if e["event_type"] != "Connection":
            b["threats"] += 1
        if e["action"] in ("Blocked", "Would Block"):
            b["blocked"] += 1
        else:
            b["allowed"] += 1
    points = [buckets[k] for k in keys]
    for p in points:
        p["traffic_mb"] = round(p["traffic_mb"], 1)
        p["display"] = p["label"][11:16] if gran == "hour" else p["label"][5:]
    return {"granularity": gran, "points": points}


def by_device_type(hours=None, site=None):
    agg = {}
    for e in _scoped(_events(), hours, site):
        t = e.get("device_type") or "(unclassified)"
        d = agg.setdefault(
            t,
            {"device_type": t, "_devices": set(), "events": 0, "threats": 0,
             "critical": 0, "blocked": 0, "_bytes": 0},
        )
        d["_devices"].add(e["device_mac"])
        d["events"] += 1
        d["_bytes"] += e.get("total_bytes") or 0
        if e["event_type"] != "Connection":
            d["threats"] += 1
            if e["severity"] == "Critical":
                d["critical"] += 1
        if e["action"] in ("Blocked", "Would Block"):
            d["blocked"] += 1
    rows = []
    for d in agg.values():
        d["devices"] = len(d.pop("_devices"))
        d["traffic_mb"] = round(d.pop("_bytes") / 1_000_000, 1)
        d["pct_blocked"] = round(100 * d["blocked"] / d["events"]) if d["events"] else 0
        rows.append(d)
    rows.sort(key=lambda r: (-r["threats"], -r["traffic_mb"]))
    return rows


def insecure_transfers():
    return [e for e in _events() if e.get("insecure_protocol")]


def outside_zone():
    return [e for e in _events() if e.get("zone_violation")]


def summary(hours=None, site=None, device_type=None):
    events = _scoped(_events(), hours, site, device_type)
    threats = [e for e in events if e["event_type"] != "Connection"]
    return {
        "total_events": len(events),
        "threat_events": len(threats),
        "blocked": sum(1 for e in events if e["action"] in ("Blocked", "Would Block")),
        "devices_at_risk": len({e["device_mac"] for e in threats}),
        "critical": sum(1 for e in threats if e["severity"] == "Critical"),
    }


# --------------------------------------------------------------------------- #
# ISE <-> FMC correlation (REAL): FMC-seen devices vs the ISE inventory
# --------------------------------------------------------------------------- #
def device_inventory():
    agg = {}
    for e in _events():
        d = agg.setdefault(
            e["device_mac"],
            {"mac": e["device_mac"], "ip": e.get("device_ip", ""),
             "device_type": e.get("device_type", ""), "hostname": e.get("hostname", ""),
             "site": e.get("site", ""), "in_ise": e.get("in_ise", False),
             "event_count": 0},
        )
        d["event_count"] += 1
    return list(agg.values())


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
    rows = correlate_to_ise()
    matched = sum(1 for r in rows if r["correlation"] == "Matched")
    total = len(rows)
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

    events = [e for e in _events() if e["device_mac"] == mac]
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
