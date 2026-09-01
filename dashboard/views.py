"""Views: landing dashboard, generic dataset table, and CSV export."""
from __future__ import annotations

import csv
from datetime import datetime, timedelta

from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone

from . import services
from .adminauth import admin_required


def index(request):
    """Main dashboard - the five requirement widgets.

    W1 Total IoT Devices Onboarded (ISE)   W2 IoT Devices at Risk
    W3 Quarantined / Blocked (ISE)          W4 Hourly Traffic & Threat trend
    W5 Attack Severity                      + ISE<->FMC correlation summary
    """
    import json

    from dashboard import analytics

    refresh = request.GET.get("refresh") == "1"

    def count(key):
        return len(services.fetch_dataset(key, use_cache=not refresh)["rows"])

    # ---- Global filters (site + time) apply to BOTH dashboards ----
    site = request.GET.get("site") or "All"
    rng = request.GET.get("range") or "24h"
    hours = {"1h": 1, "24h": 24, "7d": 168}.get(rng, 24)

    # ===== Dashboard 1 - ALL devices — NUMBERS ONLY =====
    # The at-risk *device rows* are NOT computed here; they stream in via
    # atrisk_partial (api/atrisk/) only when the user opens the table. The page
    # itself carries counts/charts (all cheap DB aggregations), no row lists.
    total_devices = analytics.ise_device_count(site=site)  # ISE inventory, site-aware
    unauthorized = count("ise-unauthorized")
    trend_all = analytics.trend(hours, site=site)
    severity_all = analytics.attack_severity(hours=hours, site=site)
    leaderboard = analytics.by_device_type(hours=hours, site=site)
    corr = analytics.correlation_summary()
    sum_all = analytics.summary(hours=hours, site=site)  # counts in one query
    compliance = analytics.compliance(hours=hours, site=site)

    # "Devices" = ISE onboarded inventory for the type (not FMC-seen MACs). Keep
    # the FMC-active count too, for context.
    ise_counts = analytics.ise_type_counts(site=site)
    for r in leaderboard:
        r["active_devices"] = r["devices"]
        r["devices"] = ise_counts.get(r["device_type"], r["devices"])
        _risk = min(r.get("at_risk", 0), r["devices"])
        r["compliance"] = round(100 * (r["devices"] - _risk) / r["devices"]) \
            if r["devices"] else 100

    # ===== Dashboard 2 - one DEVICE TYPE (default = most threats) =====
    types = [r["device_type"] for r in leaderboard]  # ordered by threats desc
    selected = request.GET.get("type")
    if selected not in types:
        selected = types[0] if types else None

    t_row = next((r for r in leaderboard if r["device_type"] == selected), None)
    t_trend = analytics.trend(hours, site=site, device_type=selected)
    t_severity = analytics.attack_severity(hours=hours, site=site, device_type=selected)
    sum_type = (analytics.summary(hours=hours, site=site, device_type=selected)
                if selected else {})

    _t_dev = ise_counts.get(selected, 0)
    _t_risk = min(sum_type.get("devices_at_risk", 0), _t_dev)
    type_metrics = {
        "devices": _t_dev,
        "at_risk": sum_type.get("devices_at_risk", 0),
        "threats": t_row["threats"] if t_row else 0,
        "critical": t_row["critical"] if t_row else 0,
        "traffic_mb": t_row["traffic_mb"] if t_row else 0,
        "pct_blocked": t_row["pct_blocked"] if t_row else 0,
        "compliance": round(100 * (_t_dev - _t_risk) / _t_dev) if _t_dev else 100,
    }

    context = {
        "status": services.connection_status(use_cache=not refresh),
        "filters": {
            "site": site,
            "range": rng,
            "sites": analytics.sites(),
            "granularity": trend_all["granularity"],
        },
        # Dashboard 1
        "widgets": {
            "total_devices": total_devices,
            "at_risk": sum_all["devices_at_risk"],
            "quarantined": unauthorized,
            "threats_window": sum_all["threat_events"],
            "critical": sum_all["critical"],
        },
        "correlation": corr,
        "compliance": compliance,
        "leaderboard": leaderboard,
        "severity_json": json.dumps(severity_all),
        "trend_json": json.dumps(trend_all["points"]),
        # Dashboard 2 (device type)
        "types": types,
        "selected_type": selected,
        "type_metrics": type_metrics,
        "type_severity_json": json.dumps(t_severity),
        "type_trend_json": json.dumps(t_trend["points"]),
    }
    return render(request, "dashboard/index.html", context)


def atrisk_partial(request):
    """Rendered at-risk device-table fragment, fetched lazily on click so the
    dashboard page itself carries only numbers, never device rows."""
    from dashboard import analytics

    site = request.GET.get("site") or "All"
    rng = request.GET.get("range") or "24h"
    hours = {"1h": 1, "24h": 24, "7d": 168}.get(rng, 24)
    dtype = request.GET.get("type") or None
    try:
        limit = min(int(request.GET.get("limit") or 50), 500)
    except (TypeError, ValueError):
        limit = 50
    rows = analytics.devices_at_risk(hours=hours, site=site, device_type=dtype)[:limit]
    return render(request, "dashboard/_atrisk_table.html", {"rows": rows})


def reports(request):
    """All click-through datasets, split into ISE and FMC tabs."""
    refresh = request.GET.get("refresh") == "1"
    cards = services.dashboard_cards(use_cache=not refresh)
    context = {
        "ise_cards": [c for c in cards if c["source"] == "ISE"],
        "fmc_cards": [c for c in cards if c["source"] == "FMC"],
    }
    return render(request, "dashboard/reports.html", context)


def mapping(request):
    """ISE <-> FMC device mapping window (correlation by MAC)."""
    from dashboard import analytics

    refresh = request.GET.get("refresh") == "1"
    payload = services.fetch_dataset("sim-correlation", use_cache=not refresh)
    context = {
        "summary": analytics.correlation_summary(),
        "rows": payload["rows"],
    }
    return render(request, "dashboard/mapping.html", context)


def device_search(request):
    """Find a device by MAC / IP / hostname and open its Device 360.

    A unique match redirects straight to the 360 view; otherwise a results
    list is shown. With no query, lists all 360-capable devices (a directory).
    """
    from dashboard import analytics

    q = (request.GET.get("q") or "").strip()
    inventory = analytics.device_inventory()

    matches = inventory
    if q:
        ql = q.lower()
        exact = [
            d for d in inventory
            if ql in (d["mac"].lower(), d["hostname"].lower(), str(d["ip"]).lower())
        ]
        if len(exact) == 1:
            return redirect("dashboard:device", mac=exact[0]["mac"])
        matches = [
            d for d in inventory
            if ql in d["mac"].lower()
            or ql in str(d["ip"]).lower()
            or ql in d["hostname"].lower()
            or ql in d["device_type"].lower()
        ]
        if len(matches) == 1:
            return redirect("dashboard:device", mac=matches[0]["mac"])

    matches = sorted(matches, key=lambda d: (-d["event_count"], d["hostname"]))
    context = {"q": q, "matches": matches, "total": len(inventory)}
    return render(request, "dashboard/device_search.html", context)


def device_360(request, mac):
    """Single at-risk device deep-dive (Device 360)."""
    import json

    from dashboard import analytics

    data = analytics.device_360(mac)
    context = {"mac": mac, "d": data}
    if data.get("found"):
        context["severity_json"] = json.dumps(data["severity"])
        context["daily_json"] = json.dumps(data["daily"])
    return render(request, "dashboard/device_360.html", context)


def policy_readiness(request):
    """Use-case -> configured-controls readiness map (the 'events gap' view)."""
    refresh = request.GET.get("refresh") == "1"
    rows = services.policy_readiness(use_cache=not refresh)
    # Group by requirement category for display.
    categories = {}
    for r in rows:
        categories.setdefault(r["category"], []).append(r)
    context = {
        "categories": categories,
        "datasets": services.DATASETS,
    }
    return render(request, "dashboard/readiness.html", context)


def dataset_table(request, key):
    """Report table shell. Renders instantly; the rows are fetched separately
    via dataset_json (AJAX) so the page paints without waiting on the data."""
    ds = services.DATASETS.get(key)
    if ds is None:
        raise Http404("Unknown dataset")
    return render(request, "dashboard/table.html", {"dataset": ds})


def dataset_json(request, key):
    """Rows for one dataset, as JSON, read from the DB snapshot. This is the
    'separate API' the table page calls on load - keeps the initial page fast."""
    ds = services.DATASETS.get(key)
    if ds is None:
        raise Http404("Unknown dataset")
    payload = services.fetch_dataset(key)
    rows = _filter_rows(payload["rows"], request)
    return JsonResponse({
        "key": key,
        "label": ds.label,
        "rows": rows,
        "columns": payload["columns"] or _infer_cols(rows),
        "error": payload["error"],
        "fetched_at": payload.get("fetched_at"),
        "count": len(rows),
    })


def _filter_rows(rows, request):
    """Apply the dashboard Site / device-type / Time filters (from query params)
    to a dataset's rows - generic, only on fields the rows actually carry, so a
    clicked-through table shows the SAME scope as the dashboard."""
    if not rows or not isinstance(rows[0], dict):
        return rows
    from dashboard.analytics import SITE_UNASSIGNED

    sample = rows[0]
    site = (request.GET.get("site") or "").strip()
    dtype = (request.GET.get("type") or "").strip()
    sev = (request.GET.get("severity") or "").strip()
    hours = {"1h": 1, "24h": 24, "7d": 168}.get(request.GET.get("range") or "")

    if sev and "severity" in sample:
        rows = [r for r in rows if (r.get("severity") or "") == sev]

    if site and site != "All" and "site" in sample:
        if site == SITE_UNASSIGNED:
            rows = [r for r in rows
                    if (r.get("site") or "").strip().lower() in ("", "all locations")]
        else:
            rows = [r for r in rows if (r.get("site") or "") == site]
    if dtype and dtype != "All" and "device_type" in sample:
        if dtype == "(unclassified)":
            rows = [r for r in rows if not (r.get("device_type") or "")]
        else:
            rows = [r for r in rows if (r.get("device_type") or "") == dtype]
    if hours and "_ts" in sample and isinstance(sample.get("_ts"), datetime):
        cutoff = timezone.now() - timedelta(hours=hours)
        rows = [r for r in rows
                if isinstance(r.get("_ts"), datetime) and r["_ts"] >= cutoff]
    return rows


def _infer_cols(rows):
    cols = []
    for r in rows:
        if isinstance(r, dict):
            for k in r:
                if k not in cols:
                    cols.append(k)
    return cols


def dataset_csv(request, key):
    """Stream the dataset as CSV (satisfies the report export requirement)."""
    ds = services.DATASETS.get(key)
    if ds is None:
        raise Http404("Unknown dataset")

    payload = services.fetch_dataset(key, use_cache=True)
    columns = payload["columns"] or ["value"]
    rows = _filter_rows(payload["rows"], request)

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{key}.csv"'
    writer = csv.writer(response)
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_stringify(row.get(c, "")) for c in columns])
    return response


def _stringify(value):
    if isinstance(value, (dict, list)):
        import json

        return json.dumps(value, ensure_ascii=False)
    return value


@admin_required
def config_sites(request):
    """In-app admin config: manage the NAD-hostname -> site mapping (SiteCode).
    Add / edit / delete / enable rows, and test a hostname against the map."""
    from dashboard.models import SiteCode
    from dashboard.site_mapping import db_site_matcher
    from integrations.location_map import site_from_hostname

    if request.method == "POST":
        action = request.POST.get("action")
        rid = request.POST.get("id")
        code = (request.POST.get("code") or "").strip()
        site = (request.POST.get("site") or "").strip()
        if action == "add" and code and site:
            SiteCode.objects.update_or_create(
                code=code, defaults={"site": site, "active": True})
        elif action == "delete" and rid:
            SiteCode.objects.filter(id=rid).delete()
        elif action == "toggle" and rid:
            row = SiteCode.objects.filter(id=rid).first()
            if row:
                row.active = not row.active
                row.save(update_fields=["active", "updated_at"])
        elif action == "edit" and rid:
            row = SiteCode.objects.filter(id=rid).first()
            if row:
                if code:
                    row.code = code
                if site:
                    row.site = site
                row.save(update_fields=["code", "site", "updated_at"])
        return redirect("dashboard:config_sites")

    test_host = (request.GET.get("test") or "").strip()
    test_result = (site_from_hostname(test_host, db_site_matcher())
                   if test_host else None)
    context = {
        "rows": SiteCode.objects.all(),
        "active_count": SiteCode.objects.filter(active=True).count(),
        "test_host": test_host,
        "test_result": test_result,
    }
    return render(request, "dashboard/config_sites.html", context)
