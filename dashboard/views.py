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
    """Classic dashboard (unchanged)."""
    return render(request, "dashboard/index.html", _dashboard_context(request))


def soc_dashboard(request):
    """Alternate SOC-styled dashboard - same data, elegant dark-first layout."""
    return render(request, "dashboard/soc.html", _dashboard_context(request))


def _dashboard_context(request):
    """Build the shared dashboard context (numbers + charts) used by both the
    classic and the SOC views. Heavy analytics are cached per filter combo.

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

    # Click-through query strings: Dashboard 1 tiles carry ONLY site+time (they
    # are all-devices); Dashboard 2 (device-type) tiles also carry the type.
    from urllib.parse import urlencode
    overall_q = urlencode({"site": site, "range": rng})

    # ===== Heavy analytics, cached in Redis (short TTL) per filter combo =====
    # All the event aggregations below are the expensive part; the same (site,
    # range, type) combo is requested repeatedly (auto-refresh, multiple users),
    # so we compute once and cache the bundle. ?refresh=1 bypasses + refreshes.
    from django.conf import settings
    from django.core.cache import cache

    type_param = request.GET.get("type") or ""
    ttl = getattr(settings, "DASHBOARD_CACHE_TTL", 45)
    cache_key = f"dash:v1:{site}|{rng}|{type_param}"

    def _compute():
        total_devices = analytics.ise_device_count(site=site)
        quarantined = analytics.quarantined_count(site=site)
        trend_all = analytics.trend(hours, site=site)
        severity_all = analytics.attack_severity(hours=hours, site=site)
        leaderboard = analytics.by_device_type(hours=hours, site=site)
        corr = analytics.correlation_summary()
        sum_all = analytics.summary(hours=hours, site=site)
        comp = analytics.compliance(hours=hours, site=site)

        # "Devices" = ISE onboarded inventory for the type (not FMC-seen MACs).
        ise_counts = analytics.ise_type_counts(site=site)
        quar_counts = analytics.quarantined_type_counts(site=site)
        for r in leaderboard:
            r["active_devices"] = r["devices"]
            r["devices"] = ise_counts.get(r["device_type"], r["devices"])
            r["quarantined"] = quar_counts.get(r["device_type"], 0)
            _nc = min(r["devices"], r.get("at_risk", 0) + r["quarantined"])
            r["compliance"] = round(100 * (r["devices"] - _nc) / r["devices"]) \
                if r["devices"] else 100
            # per-row click-through scope (site + range + this device type)
            r["q"] = urlencode({"site": site, "range": rng,
                                "type": r["device_type"]})

        types = [r["device_type"] for r in leaderboard]  # ordered by threats desc
        selected = type_param if type_param in types else (types[0] if types else None)
        t_row = next((r for r in leaderboard if r["device_type"] == selected), None)
        t_trend = analytics.trend(hours, site=site, device_type=selected)
        t_severity = analytics.attack_severity(hours=hours, site=site, device_type=selected)
        type_comp = (analytics.compliance(hours=hours, site=site, device_type=selected)
                     if selected else {"total": 0, "at_risk": 0, "quarantined": 0,
                                       "score": 100})
        type_metrics = {
            "devices": type_comp["total"],
            "at_risk": type_comp["at_risk"],
            "quarantined": type_comp["quarantined"],
            "threats": t_row["threats"] if t_row else 0,
            "critical": t_row["critical"] if t_row else 0,
            "traffic_mb": t_row["traffic_mb"] if t_row else 0,
            "pct_blocked": t_row["pct_blocked"] if t_row else 0,
            "compliance": type_comp["score"],
        }
        return {
            "total_devices": total_devices, "quarantined": quarantined,
            "trend_all": trend_all, "severity_all": severity_all,
            "leaderboard": leaderboard, "corr": corr, "sum_all": sum_all,
            "compliance": comp, "types": types, "selected": selected,
            "t_trend": t_trend, "t_severity": t_severity,
            "type_metrics": type_metrics, "sites": analytics.sites(),
        }

    if refresh:
        b = _compute()
        cache.set(cache_key, b, ttl)
    else:
        b = cache.get_or_set(cache_key, _compute, ttl)

    context = {
        "status": services.connection_status(use_cache=not refresh),
        "filters": {
            "site": site,
            "range": rng,
            "sites": b["sites"],
            "granularity": b["trend_all"]["granularity"],
        },
        "widgets": {
            "total_devices": b["total_devices"],
            "at_risk": b["sum_all"]["devices_at_risk"],
            "quarantined": b["quarantined"],
            "threats_window": b["sum_all"]["threat_events"],
            "critical": b["sum_all"]["critical"],
        },
        "correlation": b["corr"],
        "compliance": b["compliance"],
        "overall_q": overall_q,
        "type_q": urlencode({"site": site, "range": rng, "type": b["selected"] or ""}),
        "leaderboard": b["leaderboard"],
        "severity_json": json.dumps(b["severity_all"]),
        "trend_json": json.dumps(b["trend_all"]["points"]),
        "types": b["types"],
        "selected_type": b["selected"],
        "type_metrics": b["type_metrics"],
        "type_severity_json": json.dumps(b["t_severity"]),
        "type_trend_json": json.dumps(b["t_trend"]["points"]),
    }
    return context


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
    live = _live_filtered(key, request)
    if live is not None:
        rows = live
        columns, error, fetched_at = _infer_cols(rows), None, None
    else:
        payload = services.fetch_dataset(key)
        rows = _filter_rows(payload["rows"], request)
        columns = payload["columns"] or _infer_cols(rows)
        error, fetched_at = payload["error"], payload.get("fetched_at")
    return JsonResponse({
        "key": key,
        "label": ds.label,
        "rows": rows,
        "columns": columns,
        "error": error,
        "fetched_at": fetched_at,
        "count": len(rows),
    })


def _scope_params(request):
    """(hours, site, device_type) from the dashboard filter query params."""
    hours = {"1h": 1, "24h": 24, "7d": 168}.get(request.GET.get("range") or "")
    site = (request.GET.get("site") or "").strip() or None
    if site == "All":
        site = None
    dtype = (request.GET.get("type") or "").strip() or None
    if dtype == "All":
        dtype = None
    return hours, site, dtype


def _events_qs(request):
    """SecurityEvent queryset with the active dashboard filters (site/type/time/
    severity/threats) applied - the source for the sim-events table."""
    from dashboard import analytics
    hours, site, dtype = _scope_params(request)
    qs = analytics._base_qs(hours=hours, site=site, device_type=dtype)
    sev = (request.GET.get("severity") or "").strip()
    if sev:
        qs = qs.filter(severity=sev)
    if request.GET.get("threats") == "1":
        qs = qs.filter(analytics._threat_q())
    return qs


def _live_filtered(key, request):
    """For datasets that natively support (hours, site, device_type), compute
    live with the active filters - so a click-through table honours the Time
    (and Site/Type) filter even though its rows are aggregated (no per-row
    timestamp for _filter_rows to use). Returns rows, or None to fall back."""
    hours, site, dtype = _scope_params(request)
    if key == "sim-devices-at-risk":
        from dashboard import analytics
        return analytics.devices_at_risk(hours=hours, site=site, device_type=dtype)

    if key == "sim-events":
        # Query SecurityEvent live with the SAME filters as the dashboard (site /
        # type / time / severity / threats), so a click-through table shows the
        # ACTUAL matching events - not a filtered slice of a capped snapshot.
        from dashboard import event_store
        return [event_store._to_dict(e)
                for e in _events_qs(request).order_by("-ts")[:2000]]
    return None


# --------------------------------------------------------------------------- #
# Server-side pagination (DataTables protocol). sim-events pages at the DB
# level (real counts, DB search); other datasets page a materialized list.
# --------------------------------------------------------------------------- #
_EVENT_SEARCH_TEXT = ["device_mac", "device_type", "event_type", "severity",
                      "hostname", "application", "site", "action",
                      "rule_matched", "firewall"]
_EVENT_SEARCH_IP = ["device_ip", "source_ip", "dest_ip"]


def dataset_data(request, key):
    """DataTables server-side endpoint (safe wrapper): never 500s the AJAX -
    returns a JSON error payload DataTables can display inline instead."""
    def _int(name, default):
        try:
            return int(request.GET.get(name, default))
        except (TypeError, ValueError):
            return default
    try:
        return _dataset_page(request, key)
    except Http404:
        raise
    except Exception as exc:
        import logging
        import traceback
        logging.getLogger("dashboard").error(
            "dataset_data(%s) failed: %s\n%s", key, exc, traceback.format_exc())
        return JsonResponse({"draw": _int("draw", 1), "recordsTotal": 0,
                             "recordsFiltered": 0, "data": [],
                             "error": f"{type(exc).__name__}: {exc}"})


def _dataset_page(request, key):
    """Returns one page + total/filtered counts.
    Params: draw, start, length, search[value]. Response: {draw, recordsTotal,
    recordsFiltered, data, columns}."""
    ds = services.DATASETS.get(key)
    if ds is None:
        raise Http404("Unknown dataset")

    def _int(name, default):
        try:
            return int(request.GET.get(name, default))
        except (TypeError, ValueError):
            return default

    draw = _int("draw", 1)
    start = max(0, _int("start", 0))
    length = _int("length", 25)
    if length < 0:
        length = 100000  # DataTables "All"
    search = (request.GET.get("search[value]") or "").strip()

    # ---- sim-events: page at the DB level ----
    if key == "sim-events":
        from django.db.models import Q, TextField
        from django.db.models.functions import Cast
        from dashboard import event_store

        qs = _events_qs(request)
        total = qs.count()
        if search:
            qs = qs.annotate(
                _dip=Cast("device_ip", TextField()),
                _sip=Cast("source_ip", TextField()),
                _pip=Cast("dest_ip", TextField()))
            cond = Q()
            for f in _EVENT_SEARCH_TEXT:
                cond |= Q(**{f"{f}__icontains": search})
            for f in ("_dip", "_sip", "_pip"):
                cond |= Q(**{f"{f}__icontains": search})
            qs = qs.filter(cond)
        filtered = qs.count() if search else total
        page = [event_store._to_dict(e)
                for e in qs.order_by("-ts")[start:start + length]]
        columns = _infer_cols(page)
        if not columns:
            # empty page (e.g. searched past the end) - derive stable columns
            one = _events_qs(request).order_by("-ts").first()
            columns = _infer_cols([event_store._to_dict(one)]) if one else []
        return JsonResponse({"draw": draw, "recordsTotal": total,
                             "recordsFiltered": filtered, "data": page,
                             "columns": columns})

    # ---- other datasets: page a materialized list ----
    live = _live_filtered(key, request)
    if live is not None:
        rows, columns = live, _infer_cols(live)
    else:
        payload = services.fetch_dataset(key)
        rows = _filter_rows(payload["rows"], request)
        columns = payload["columns"] or _infer_cols(rows)
    total = len(rows)
    if search:
        s = search.lower()
        rows = [r for r in rows if isinstance(r, dict)
                and any(s in str(r.get(c, "")).lower() for c in columns)]
    filtered = len(rows)
    page = rows[start:start + length]
    return JsonResponse({"draw": draw, "recordsTotal": total,
                         "recordsFiltered": filtered, "data": page,
                         "columns": columns})


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

    if request.GET.get("threats") == "1" and "event_type" in sample:
        from dashboard.analytics import THREAT_SEVERITIES
        rows = [r for r in rows
                if (r.get("event_type") or "") != "Connection"
                and (r.get("severity") or "") in THREAT_SEVERITIES]

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
    from dashboard import audit
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
            audit.log(request, "site.add", code, site)
        elif action == "delete" and rid:
            row = SiteCode.objects.filter(id=rid).first()
            SiteCode.objects.filter(id=rid).delete()
            if row:
                audit.log(request, "site.delete", row.code, row.site)
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
                audit.log(request, "site.edit", row.code, row.site)
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


@admin_required
def config_settings(request):
    """In-app admin config: operational settings (event retention days). Stored
    in AppSetting so the purge task reads them without a redeploy."""
    from dashboard import audit
    from dashboard.models import AppSetting, SecurityEvent

    keys = {"retention_threat_days": 7, "retention_connection_days": 7}
    if request.method == "POST":
        changed = []
        for k in keys:
            raw = (request.POST.get(k) or "").strip()
            try:
                v = int(raw)
                if 1 <= v <= 3650:
                    AppSetting.set(k, v)
                    changed.append(f"{k}={v}")
            except ValueError:
                pass
        if changed:
            audit.log(request, "settings.update", "retention", ", ".join(changed))
        return redirect(f"{request.path}?msg=Saved.")

    context = {
        "msg": request.GET.get("msg"),
        "retention_threat_days": AppSetting.get_int("retention_threat_days", 7),
        "retention_connection_days": AppSetting.get_int("retention_connection_days", 7),
        "event_count": SecurityEvent.objects.count(),
    }
    return render(request, "dashboard/config_settings.html", context)


@admin_required
def config_users(request):
    """Admin-only user management: list / add / delete users, set role
    (admin vs viewer) and reset passwords. Self-lockout guards included."""
    from django.contrib.auth import get_user_model
    from django.contrib.auth.password_validation import validate_password
    from django.core.exceptions import ValidationError

    from dashboard import audit

    User = get_user_model()

    def redir(msg=None, err=None):
        from urllib.parse import urlencode
        q = urlencode({k: v for k, v in {"msg": msg, "err": err}.items() if v})
        return redirect(f"{request.path}?{q}" if q else request.path)

    if request.method == "POST":
        action = request.POST.get("action")
        uid = request.POST.get("id")
        target = User.objects.filter(id=uid).first() if uid else None
        is_self = target and target.id == request.user.id
        admin_count = User.objects.filter(is_staff=True, is_active=True).count()

        if action == "add":
            username = (request.POST.get("username") or "").strip()
            password = request.POST.get("password") or ""
            make_admin = request.POST.get("role") == "admin"
            if not username:
                return redir(err="Username is required.")
            if User.objects.filter(username=username).exists():
                return redir(err=f"User '{username}' already exists.")
            try:
                validate_password(password)
            except ValidationError as e:
                return redir(err="Password: " + " ".join(e.messages))
            u = User(username=username, is_staff=make_admin, is_active=True)
            u.set_password(password)
            u.save()
            audit.log(request, "user.add", username,
                      "admin" if make_admin else "viewer")
            return redir(msg=f"Created {'admin' if make_admin else 'viewer'} '{username}'.")

        if not target:
            return redir(err="User not found.")

        if action == "delete":
            if is_self:
                return redir(err="You cannot delete your own account.")
            if target.is_staff and admin_count <= 1:
                return redir(err="Cannot delete the last admin.")
            name = target.username
            target.delete()
            audit.log(request, "user.delete", name)
            return redir(msg=f"Deleted '{name}'.")

        if action == "role":
            make_admin = request.POST.get("role") == "admin"
            if is_self and not make_admin:
                return redir(err="You cannot remove your own admin rights.")
            if target.is_staff and not make_admin and admin_count <= 1:
                return redir(err="Cannot demote the last admin.")
            target.is_staff = make_admin
            target.save(update_fields=["is_staff"])
            audit.log(request, "user.role", target.username,
                      "admin" if make_admin else "viewer")
            return redir(msg=f"{target.username} is now {'admin' if make_admin else 'viewer'}.")

        if action == "reset":
            password = request.POST.get("password") or ""
            try:
                validate_password(password, user=target)
            except ValidationError as e:
                return redir(err="Password: " + " ".join(e.messages))
            target.set_password(password)
            target.save(update_fields=["password"])
            audit.log(request, "user.reset_password", target.username)
            return redir(msg=f"Password reset for '{target.username}'.")

        if action == "toggle_active":
            if is_self:
                return redir(err="You cannot deactivate your own account.")
            target.is_active = not target.is_active
            target.save(update_fields=["is_active"])
            audit.log(request, "user.toggle_active", target.username,
                      "active" if target.is_active else "disabled")
            return redir(msg=f"{target.username} {'activated' if target.is_active else 'deactivated'}.")

        return redir(err="Unknown action.")

    context = {
        "msg": request.GET.get("msg"),
        "err": request.GET.get("err"),
        "users": User.objects.order_by("-is_staff", "username"),
    }
    return render(request, "dashboard/config_users.html", context)


@admin_required
def config_audit(request):
    """Admin-only audit trail of configuration changes."""
    from dashboard.models import AuditLog

    q = (request.GET.get("q") or "").strip()
    rows = AuditLog.objects.all()
    if q:
        from django.db.models import Q
        rows = rows.filter(Q(username__icontains=q) | Q(action__icontains=q)
                           | Q(target__icontains=q) | Q(detail__icontains=q))
    return render(request, "dashboard/config_audit.html",
                  {"rows": rows[:400], "q": q})


@admin_required
def config_activity(request):
    """Admin-only activity log of scheduled/background task runs."""
    from dashboard.models import TaskRun

    rows = TaskRun.objects.all()[:300]
    # quick stats over the recent window shown
    recent = list(rows)
    stats = {
        "total": len(recent),
        "success": sum(1 for r in recent if r.status == "success"),
        "failure": sum(1 for r in recent if r.status == "failure"),
        "running": sum(1 for r in recent if r.finished is None),
    }
    return render(request, "dashboard/config_activity.html",
                  {"rows": recent, "stats": stats})
