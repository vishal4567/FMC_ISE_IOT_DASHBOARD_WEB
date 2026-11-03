"""
Orchestration layer between Django views and the raw ISE/FMC clients.

Responsibilities
----------------
* Build configured clients from ``settings`` (lazily, so a missing config for
  one source never breaks the other).
* A **dataset registry**: every fetchable table is described once (label,
  source, which client method produces its rows, and the widget it maps to).
  A single generic view + CSV exporter are driven off this registry, so
  adding a data source is a one-entry change.
* Short-lived caching (Django cache) so repeated page loads and the CSV
  export of the same table don't re-hit the sandbox.

The ISE inventory is IoT-scoped: only endpoints in the allow-listed profiles
are imported (see dashboard/tasks.sync_iot_endpoints), stamped with device type
and site, and read back from the DB. FMC config datasets are fetched live.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from django.conf import settings
from django.core.cache import cache

from integrations.exceptions import ConfigError, IntegrationError
from integrations.fmc_client import FMCClient
from integrations.ise_client import ISEClient


# --------------------------------------------------------------------------- #
# Client factories
#
# Clients are memoised per-process and keyed by their effective config. A
# single dashboard page fetches many datasets; without reuse each one would
# re-authenticate, and FMC in particular limits concurrent API tokens and
# request rate - so re-login-per-dataset silently trips those limits. Sharing
# one authenticated client (FMC token TTL ~30 min) keeps a page load to a
# single login per source. The client's own session/token handle expiry.
# --------------------------------------------------------------------------- #
_CLIENT_CACHE: dict = {}


def _ise_cache_key(cfg):
    return ("ise", cfg["HOST"], cfg["ERS_PORT"], cfg["USERNAME"], cfg["PASSWORD"])


def _fmc_cache_key(cfg):
    return ("fmc", cfg["HOST"], cfg["PORT"], cfg["USERNAME"], cfg["PASSWORD"])


def get_ise_client() -> ISEClient:
    cfg = settings.ISE
    key = _ise_cache_key(cfg)
    client = _CLIENT_CACHE.get(key)
    if client is None:
        client = ISEClient(
            host=cfg["HOST"],
            username=cfg["USERNAME"],
            password=cfg["PASSWORD"],
            ers_port=cfg["ERS_PORT"],
            verify_tls=cfg["VERIFY_TLS"],
            timeout=cfg["TIMEOUT"],
            page_size=cfg["PAGE_SIZE"],
            max_pages=cfg["MAX_PAGES"],
            openapi_page_size=cfg["OPENAPI_PAGE_SIZE"],
            device_type_attr=cfg["DEVICE_TYPE_ATTR"],
        )
        _CLIENT_CACHE[key] = client
    return client


def get_dataconnect_client():
    """ISE Data Connect client (built from settings.DATACONNECT). Lazily imported
    so python-oracledb is only needed when Data Connect is actually used."""
    from integrations.ise_dataconnect import DataConnectClient

    cfg = settings.DATACONNECT
    return DataConnectClient(
        host=cfg["HOST"], password=cfg["PASSWORD"], port=cfg["PORT"],
        service_name=cfg["SERVICE_NAME"], user=cfg["USER"],
        verify_tls=cfg["VERIFY_TLS"], ca_cert=cfg["CA_CERT"], timeout=cfg["TIMEOUT"],
    )


def get_fmc_client() -> FMCClient:
    cfg = settings.FMC
    key = _fmc_cache_key(cfg)
    client = _CLIENT_CACHE.get(key)
    if client is None:
        client = FMCClient(
            host=cfg["HOST"],
            username=cfg["USERNAME"],
            password=cfg["PASSWORD"],
            port=cfg["PORT"],
            verify_tls=cfg["VERIFY_TLS"],
            timeout=cfg["TIMEOUT"],
            domain_uuid=cfg["DOMAIN_UUID"],
            page_limit=cfg["PAGE_LIMIT"],
            max_pages=cfg["MAX_PAGES"],
        )
        _CLIENT_CACHE[key] = client
    return client


# --------------------------------------------------------------------------- #
# Dataset registry
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Dataset:
    key: str  # URL slug
    label: str  # human title
    source: str  # "ISE" or "FMC"
    fetch: Callable[[], list]  # returns list[dict]
    description: str = ""
    widget: str = ""  # which requirement widget/use-case it feeds
    columns: list = field(default_factory=list)  # optional explicit column order
    # derived=True: computed from the local DB (event store / IoTDevice), so it's
    # read live at request time (cheap, always fresh) and NOT snapshotted. False:
    # external (ISE/FMC) - the scheduler snapshots it and the web reads the DB.
    derived: bool = False


def _ise_endpoints():
    """The IoT endpoint inventory as synced from ISE into IoTDevice (device type
    + site). This reads the DB (populated hourly by sync_iot_endpoints), so the
    Reports view never triggers a live 50k-endpoint walk."""
    from dashboard.models import IoTDevice

    rows = []
    for d in IoTDevice.objects.order_by("device_type", "mac"):
        rows.append({
            "mac": d.mac,
            # device_type column shows the ISE identity group (e.g. Wipro_CCTV);
            # the profiler policy is kept separately as endpoint_profile.
            "device_type": d.device_type or d.ise_identity_group,
            "authorization_profile": d.authorization_profile,
            "quarantined": "Yes" if "QUARANTINE" in
                           (d.authorization_profile or "").upper() else "",
            "endpoint_profile": d.ise_profile,
            "logical_profile": d.logical_profile or d.ise_identity_group,
            "site": d.site,
            "ip": d.ip or "",
            "hostname": d.hostname,
            "identity_group": d.ise_identity_group,
            "last_seen": d.last_seen.isoformat() if d.last_seen else "",
        })
    return rows


def _dc_on() -> bool:
    """True when ISE Data Connect is configured - route ISE reads through SQL."""
    return bool(settings.DATACONNECT.get("ENABLED"))


def _ise_endpoint_groups():
    # ENDPOINT_IDENTITY_GROUPS(id, name, description, status)
    if _dc_on():
        return get_dataconnect_client().rows(
            "endpoint_identity_groups", ["id", "name", "description", "status"],
            order="name")
    return get_ise_client().get_endpoint_groups()


def _ise_network_devices():
    # NETWORK_DEVICES(id, name, ip_mask, profile_name, location, type)
    if _dc_on():
        return get_dataconnect_client().rows(
            "network_devices",
            ["name", "ip_mask", "type", "location", "profile_name"], order="name")
    return get_ise_client().get_network_devices()


def _ise_profiles():
    # Profiles actually in use (+ counts) from ENDPOINTS_DATA.
    if _dc_on():
        _, rows = get_dataconnect_client().query(
            "SELECT endpoint_policy AS name, COUNT(*) AS count FROM endpoints_data "
            "WHERE endpoint_policy IS NOT NULL GROUP BY endpoint_policy "
            "ORDER BY COUNT(*) DESC")
        return rows
    return get_ise_client().get_profiler_profiles()


def _ise_quarantined():
    """IoT devices currently in a QUARANTINE authorization rule
    (authorization_profile contains 'Quarantine'). This is the W3 tile source."""
    from dashboard.models import IoTDevice

    rows = []
    for d in (IoTDevice.objects.filter(authorization_profile__icontains="quarantine")
              .order_by("authorization_profile", "mac")):
        rows.append({
            "mac": d.mac,
            "device_type": d.ise_identity_group or d.device_type,
            "authorization_profile": d.authorization_profile,
            "endpoint_profile": d.device_type,
            "site": d.site,
            "ip": d.ip or "",
            "last_seen": d.last_seen.isoformat() if d.last_seen else "",
        })
    return rows


def _onboarded_logical_profiles():
    """Onboarded LOGICAL profiles: one row per logical profile in our IoTDevice
    inventory, with the number of devices onboarded under it."""
    from django.db.models import Count

    from dashboard.models import IoTDevice

    rows = (IoTDevice.objects.exclude(logical_profile="")
            .values("logical_profile").annotate(devices=Count("id"))
            .order_by("-devices"))
    return [{"logical_profile": r["logical_profile"], "devices": r["devices"]}
            for r in rows]


def _onboarded_profiler_profiles():
    """Onboarded PROFILER profiles: one row per profiler policy in our IoTDevice
    inventory, with the number of devices of each (and its logical profile)."""
    from django.db.models import Count, Max

    from dashboard.models import IoTDevice

    rows = (IoTDevice.objects.exclude(ise_profile="")
            .values("ise_profile")
            .annotate(devices=Count("id"), logical_profile=Max("logical_profile"))
            .order_by("-devices"))
    return [{"profiler_profile": r["ise_profile"],
             "logical_profile": r["logical_profile"] or "",
             "devices": r["devices"]} for r in rows]


def _ise_sessions():
    # Fetch the ACTIVE sessions once, then drop non-IoT devices in code (keep only
    # MACs in the synced IoTDevice inventory). Run sync_ise first to populate it.
    if _dc_on():
        from dashboard.models import IoTDevice
        from integrations.ise_dataconnect import _loc_leaf

        iot = {str(m).upper() for m in IoTDevice.objects.values_list("mac", flat=True)}
        if not iot:
            return []
        dc = settings.DATACONNECT
        mac_col = dc["COL_SESSION_MAC"]
        rows = get_dataconnect_client().rows(
            dc["SESSIONS_VIEW"],
            [mac_col, "endpoint_profile", "device_type", "location",
             "nas_ip_address", "framed_ip_address", "identity_group"],
            where=dc["SESSIONS_WHERE"], limit=dc["SESSIONS_LIMIT"])
        # remove non-IoT devices in code
        out = []
        for r in rows:
            if str(r.get(mac_col, "")).upper() in iot:
                r["location"] = _loc_leaf(r.get("location"))
                out.append(r)
        return out
    return get_ise_client().get_active_sessions()


def _ise_unauthorized():
    # Endpoints in a Blocked/Unknown/Blacklist identity group (join ENDPOINTS_DATA
    # -> ENDPOINT_IDENTITY_GROUPS by group id).
    if _dc_on():
        _, rows = get_dataconnect_client().query(
            "SELECT e.mac_address AS mac, g.name AS unauthorized_group "
            "FROM endpoints_data e JOIN endpoint_identity_groups g "
            "ON e.identity_group_id = g.id "
            "WHERE UPPER(g.name) IN ('BLOCKED LIST','UNKNOWN','BLACKLIST',"
            "'DENY','QUARANTINE')")
        return rows
    return get_ise_client().get_unauthorized_endpoints()


def _fmc_devices():
    return get_fmc_client().get_devices()


def _fmc_access_policies():
    return get_fmc_client().get_access_policies()


def _fmc_access_rules():
    return get_fmc_client().get_all_access_rules()


def _fmc_intrusion_policies():
    return get_fmc_client().get_intrusion_policies()


def _fmc_file_policies():
    return get_fmc_client().get_file_policies()


def _fmc_prefilter_policies():
    return get_fmc_client().get_prefilter_policies()


def _fmc_security_zones():
    return get_fmc_client().get_security_zones()


def _fmc_network_objects():
    return get_fmc_client().get_network_objects()


def _fmc_audit():
    return get_fmc_client().get_audit_records()


# ---- Threat analytics (read from the DB event store - see analytics.py) ----
def _threat_events():
    from dashboard import analytics

    return analytics.all_events()


def _threat_devices_at_risk():
    from dashboard import analytics

    return analytics.devices_at_risk()


def _threat_insecure():
    from dashboard import analytics

    return analytics.insecure_transfers()


def _threat_outside_zone():
    from dashboard import analytics

    return analytics.outside_zone()


def _threat_correlation():
    from dashboard import analytics

    return analytics.correlate_to_ise()


DATASETS: dict[str, Dataset] = {
    d.key: d
    for d in [
        # ---- ISE ----
        Dataset(
            key="ise-endpoints",
            label="IoT Endpoints (ISE)",
            source="ISE",
            fetch=_ise_endpoints,
            derived=True,   # reads the IoTDevice DB inventory
            widget="Widget 1 - Total Devices Onboarded / Asset Inventory",
            description="IoT endpoints synced from ISE (allow-listed profiles "
            "only), each with its device type, ISE profile and site. Read from "
            "the DB inventory that sync_iot_endpoints refreshes hourly.",
        ),
        Dataset(
            key="ise-endpoint-groups",
            label="ISE Endpoint Identity Groups",
            source="ISE",
            fetch=_ise_endpoint_groups,
            widget="Asset Inventory - grouping",
            description="Endpoint identity groups used to classify devices.",
        ),
        Dataset(
            key="ise-network-devices",
            label="ISE Network Devices (NADs)",
            source="ISE",
            fetch=_ise_network_devices,
            widget="Location - switch / WLC",
            description="Switches / WLCs registered as network access devices.",
        ),
        Dataset(
            key="ise-profiles",
            label="ISE Profiler Profiles",
            source="ISE",
            fetch=_ise_profiles,
            widget="Device type / category",
            description="Profiler profiles (camera, printer, sensor, ...). "
            "Empty if the ERS resource is unavailable on this ISE version.",
        ),
        Dataset(
            key="ise-quarantined",
            label="Quarantined IoT Devices",
            source="ISE",
            fetch=_ise_quarantined,
            derived=True,
            widget="Widget 3 - Quarantined / Blocked",
            description="IoT devices in a QUARANTINE authorization rule "
            "(authorization profile contains 'Quarantine').",
        ),
        Dataset(
            key="ise-logical-profiles",
            label="Onboarded Logical Profiles",
            source="ISE",
            fetch=_onboarded_logical_profiles,
            derived=True,
            widget="Logical profiles onboarded + device counts",
            description="Logical profiles onboarded in this environment and the "
            "number of devices under each.",
        ),
        Dataset(
            key="ise-mapped-profiles",
            label="Onboarded Profiler Profiles",
            source="ISE",
            fetch=_onboarded_profiler_profiles,
            derived=True,
            widget="Profiler profiles onboarded + device counts",
            description="Profiler profiles onboarded in this environment, the "
            "logical profile each maps to, and the number of devices of each.",
        ),
        Dataset(
            key="ise-sessions",
            label="ISE Active Sessions (MnT)",
            source="ISE",
            fetch=_ise_sessions,
            widget="Network Access / Location",
            description="Live sessions from the MnT API (IP, NAS, posture). "
            "Empty if there are no active sessions.",
        ),
        Dataset(
            key="ise-unauthorized",
            label="Unauthorised Device Detection",
            source="ISE",
            fetch=_ise_unauthorized,
            widget="Use case 1.3 - Unauthorised Device Detection",
            description="Endpoints ISE placed in a deny-style group (Blocked "
            "List) or could not identify (Unknown). REST-only approximation - "
            "live failed-auth logs require the MnT API.",
        ),
        # ---- FMC ----
        Dataset(
            key="fmc-devices",
            label="FMC Managed Devices",
            source="FMC",
            fetch=_fmc_devices,
            widget="Policy - device / firewall name",
            description="Firewalls (FTD) managed by this FMC.",
        ),
        Dataset(
            key="fmc-access-policies",
            label="FMC Access Control Policies",
            source="FMC",
            fetch=_fmc_access_policies,
            widget="Policy - access control policy",
            description="Access control policies and their default action.",
        ),
        Dataset(
            key="fmc-access-rules",
            label="FMC Access Rules",
            source="FMC",
            fetch=_fmc_access_rules,
            widget="Policy - rule name / action taken",
            description="Individual rules across all access policies "
            "(zones, action, IPS policy).",
        ),
        Dataset(
            key="fmc-intrusion-policies",
            label="FMC Intrusion Policies",
            source="FMC",
            fetch=_fmc_intrusion_policies,
            widget="Threat - intrusion",
            description="Intrusion prevention policies configured on FMC.",
        ),
        Dataset(
            key="fmc-file-policies",
            label="FMC File / Malware Policies",
            source="FMC",
            fetch=_fmc_file_policies,
            widget="Advanced threat analysis - file/malware",
            description="File & malware inspection policies (the controls "
            "behind retrospective/malware events).",
        ),
        Dataset(
            key="fmc-prefilter-policies",
            label="FMC Prefilter Policies",
            source="FMC",
            fetch=_fmc_prefilter_policies,
            widget="Traffic handling / bypass",
            description="Prefilter policies (early traffic handling, tunnel "
            "and fastpath/bypass rules).",
        ),
        Dataset(
            key="fmc-security-zones",
            label="FMC Security Zones",
            source="FMC",
            fetch=_fmc_security_zones,
            widget="Traffic - zone",
            description="Security zones used in access/compliance rules.",
        ),
        Dataset(
            key="fmc-network-objects",
            label="FMC Network Objects",
            source="FMC",
            fetch=_fmc_network_objects,
            widget="Device mapping - host/network",
            description="Network/host address objects defined on FMC.",
        ),
        Dataset(
            key="fmc-audit",
            label="FMC Audit Records",
            source="FMC",
            fetch=_fmc_audit,
            widget="Events (REST-available subset)",
            description="Audit trail from FMC. NOTE: intrusion/malware/"
            "connection events are NOT in the REST API - they require "
            "eStreamer / Security Analytics & Logging.",
        ),
        # ---- FMC event feed (eStreamer -> DB, see analytics.py) ----
        Dataset(
            key="sim-events",
            label="FMC Events",
            source="FMC",
            fetch=_threat_events,
            derived=True,
            widget="Events feed (intrusion/connection/malware/file/SI)",
            description="FMC event feed (intrusion / connection / malware / "
            "file / security-intelligence).",
        ),
        Dataset(
            key="sim-devices-at-risk",
            label="IoT Devices at Risk",
            source="FMC",
            fetch=_threat_devices_at_risk,
            derived=True,
            widget="Widget 2 - IoT Devices at Risk",
            description="Devices ranked by threat events, with ISE correlation.",
        ),
        Dataset(
            key="sim-insecure",
            label="Insecure Data Transfer",
            source="FMC",
            fetch=_threat_insecure,
            derived=True,
            widget="Use case 4.2 - Insecure data transfer",
            description="Events over clear-text protocols "
            "(Telnet/FTP/HTTP/SNMP/TFTP/SMBv1).",
        ),
        Dataset(
            key="sim-outside-zone",
            label="Assets Outside Allowed Zone",
            source="FMC",
            fetch=_threat_outside_zone,
            derived=True,
            widget="Use case 4.3 - Assets talking outside allowed zone",
            description="Blocked cross-zone traffic to Outside / DMZ zones.",
        ),
        Dataset(
            key="sim-correlation",
            label="ISE â†” FMC Device Mapping",
            source="MAP",
            fetch=_threat_correlation,
            derived=True,
            widget="ISE <-> FMC correlation",
            description="Every FMC event-device mapped to an ISE endpoint by "
            "MAC: matched (with ISE identity) vs unmatched (FMC-only).",
        ),
    ]
}


def ise_endpoint_count(*, use_cache: bool = True) -> int:
    """W1 tile: number of IoT devices onboarded. Reads the DB inventory
    (IoTDevice) the hourly sync populates - no live ISE call."""
    from dashboard.models import IoTDevice

    return IoTDevice.objects.count()


def source_enabled(source: str) -> bool:
    if source == "ISE":
        return settings.ISE["ENABLED"]
    if source == "FMC":
        return settings.FMC["ENABLED"]
    return True  # derived/event datasets have no direct external dependency


# --------------------------------------------------------------------------- #
# DB snapshot store (scheduler writes, web reads)
#
# The web tier NEVER calls ISE/FMC live. The Celery snapshot task fetches each
# dataset with fetch_dataset_live() and persists it via save_snapshot(); the web
# reads it back with fetch_dataset(). This keeps request latency to a DB read
# and isolates the UI from ISE/FMC slowness or outages.
# --------------------------------------------------------------------------- #
def save_snapshot(name: str, data: dict) -> None:
    from django.utils import timezone
    from dashboard.models import Snapshot

    Snapshot.objects.update_or_create(
        name=name, defaults={"data": data, "fetched_at": timezone.now()}
    )


def load_snapshot(name: str):
    from dashboard.models import Snapshot

    row = Snapshot.objects.filter(name=name).first()
    if not row:
        return None, None
    return row.data, row.fetched_at


def fetch_dataset(key: str, *, use_cache: bool = True) -> dict:
    """DB-only read of a dataset snapshot: ``{rows, columns, error, cached,
    fetched_at}``. Data comes from the Snapshot table the scheduler writes -
    no live ISE/FMC call. ``use_cache`` is accepted for call-site compatibility
    but has no effect (reads are always from the DB)."""
    ds = DATASETS[key]
    # DB-derived datasets (event analytics, IoTDevice inventory) read the DB
    # directly and are cheap + always fresh - no snapshot needed.
    if getattr(ds, "derived", False):
        return fetch_dataset_live(key)

    data, fetched_at = load_snapshot(f"dataset:{key}")
    if data is None:
        return {"rows": [], "columns": ds.columns, "error":
                "Not collected yet - the scheduler will populate this shortly.",
                "cached": True, "fetched_at": None}
    data = dict(data)
    data["cached"] = True
    data["fetched_at"] = fetched_at.isoformat() if fetched_at else None
    return data


def fetch_dataset_live(key: str) -> dict:
    """Hit the source (ISE/FMC or the DB analytics) for a dataset. Used by the
    scheduler to build snapshots - NOT by the web request path for external
    datasets. Never raises: errors are captured in the envelope."""
    ds = DATASETS[key]
    if not source_enabled(ds.source):
        return {"rows": [], "columns": [], "error":
                f"{ds.source} is disabled in configuration.", "cached": False}
    try:
        rows = ds.fetch()
        columns = ds.columns or _infer_columns(rows)
        return {"rows": rows, "columns": columns, "error": None, "cached": False}
    except ConfigError as exc:
        return {"rows": [], "columns": [], "error": str(exc), "cached": False}
    except IntegrationError as exc:
        detail = f"{exc}"
        if exc.detail:
            detail += f" - {exc.detail}"
        return {"rows": [], "columns": [], "error": detail, "cached": False}
    except Exception as exc:  # pragma: no cover - defensive catch-all
        return {"rows": [], "columns": [], "error":
                f"Unexpected error fetching {key}: {exc}", "cached": False}


def snapshot_all_datasets(log=None) -> dict:
    """Fetch every external dataset live and persist it as a DB snapshot. Called
    by the scheduler (and the snapshot_datasets management command). Pass
    log=print for a per-dataset progress line with timing."""
    import time

    say = log if callable(log) else (lambda *a, **k: None)
    external = [ds for ds in DATASETS.values() if not getattr(ds, "derived", False)]
    say(f"[snapshot] {len(external)} external ISE/FMC datasets to fetch...")
    n, errors = 0, 0
    for ds in external:
        say(f"[snapshot]   fetching {ds.key} ({ds.source})...")
        t = time.monotonic()
        payload = fetch_dataset_live(ds.key)
        save_snapshot(f"dataset:{ds.key}", payload)
        secs = round(time.monotonic() - t, 1)
        n += 1
        if payload.get("error"):
            errors += 1
            say(f"[snapshot]     {ds.key}: ERROR ({secs}s) {str(payload['error'])[:80]}")
        else:
            say(f"[snapshot]     {ds.key}: {len(payload['rows'])} rows ({secs}s)")
    say("[snapshot]   probing ISE/FMC connectivity...")
    save_snapshot("connection_status", connection_status_live())
    say(f"[snapshot] done: {n} datasets, {errors} with errors")
    return {"datasets": n, "errors": errors}


def _infer_columns(rows: list) -> list:
    """Union of keys across rows, preserving first-seen order."""
    columns: list = []
    for row in rows:
        if isinstance(row, dict):
            for k in row.keys():
                if k not in columns:
                    columns.append(k)
    return columns


# --------------------------------------------------------------------------- #
# Dashboard summary (counts per dataset + connectivity)
# --------------------------------------------------------------------------- #
def connection_status(*, use_cache: bool = True) -> dict:
    """DB-only: the last connectivity probe result the scheduler stored. No live
    probe on the request path."""
    data, _ = load_snapshot("connection_status")
    if data:
        return data
    return {
        "ISE": {"ok": None, "detail": "Not probed yet."},
        "FMC": {"ok": None, "detail": "Not probed yet."},
    }


def connection_status_live(*, use_cache: bool = True) -> dict:
    """Actually probe ISE/FMC. Called by the scheduler, not the web request."""
    status = {}
    if _dc_on():
        try:
            get_dataconnect_client().test()
            status["ISE"] = {"ok": True, "detail": "Data Connect (SQL) reachable."}
        except Exception as exc:
            status["ISE"] = {"ok": False, "detail": f"Data Connect: {exc}"[:200]}
    elif settings.ISE["ENABLED"]:
        try:
            status["ISE"] = get_ise_client().test_connection()
        except ConfigError as exc:
            status["ISE"] = {"ok": False, "detail": str(exc)}
    else:
        status["ISE"] = {"ok": None, "detail": "Disabled in configuration."}

    if settings.FMC["ENABLED"]:
        try:
            status["FMC"] = get_fmc_client().test_connection()
        except ConfigError as exc:
            status["FMC"] = {"ok": False, "detail": str(exc)}
    else:
        status["FMC"] = {"ok": None, "detail": "Disabled in configuration."}
    return status


def dashboard_cards(*, use_cache: bool = True) -> list:
    """One card per dataset with a row count (or error) for the landing page.

    Reports that ERROR (e.g. FMC REST config reports and the ISE MnT sessions
    report when their credentials aren't configured) are skipped, so the page
    shows only the reports that actually work. They reappear automatically once
    the source is reachable."""
    cards = []
    for ds in DATASETS.values():
        payload = fetch_dataset(ds.key, use_cache=use_cache)
        if payload["error"]:
            continue   # hide broken reports (missing/invalid credentials, etc.)
        cards.append(
            {
                "key": ds.key,
                "label": ds.label,
                "source": ds.source,
                "widget": ds.widget,
                "description": ds.description,
                "count": len(payload["rows"]),
                "error": payload["error"],
                "cached": payload["cached"],
            }
        )
    return cards


# --------------------------------------------------------------------------- #
# Capability readiness  (use case -> live signal -> status)
#
# Judged from the data the platform actually has NOW: the eStreamer event feed
# (SecurityEvent), the ISE IoT inventory, correlation and quarantine - not FMC
# REST config counts (which need FMC credentials this deployment may lack). So
# the map reflects what is really operating today, per use case.
#
# Status keys:
#   operational   - live data is flowing for this capability (green)
#   ready         - capability is wired but no events in the window yet (amber)
#   action-needed - needs a write-capable ISE ANC account to act (blue)
#   disabled      - depends on connection logging, which is turned off (grey)
#   none          - no data yet (red)
# --------------------------------------------------------------------------- #
STATUS_LABELS = {
    "operational": "Operational",
    "ready": "Ready - no recent events",
    "action-needed": "Needs write access (ANC)",
    "disabled": "Disabled - connection logging off",
    "none": "No data yet",
}


def policy_readiness(*, use_cache: bool = True) -> list:
    from datetime import timedelta

    from django.db.models import Count
    from django.utils import timezone

    from dashboard import analytics
    from dashboard.models import IoTDevice, SecurityEvent

    def count(key):
        return len(fetch_dataset(key, use_cache=use_cache)["rows"])

    # LIVE signals: judge each capability from the data we actually have now -
    # the eStreamer event feed + ISE inventory + correlation - not FMC REST
    # config counts (which need FMC credentials this deployment may not have).
    win = analytics.EVENT_WINDOW_DAYS
    cutoff = timezone.now() - timedelta(days=win)
    by_type = {r["event_type"]: r["n"] for r in
               SecurityEvent.objects.filter(ts__gte=cutoff)
               .values("event_type").annotate(n=Count("id"))}
    intrusion = by_type.get("Intrusion", 0)
    malware = by_type.get("Malware", 0)
    files = by_type.get("File", 0)
    si = by_type.get("Security Intelligence", 0)

    devices = IoTDevice.objects.count()
    quarantined = analytics.quarantined_count()
    at_risk = count("sim-devices-at-risk")
    unauthorized = count("ise-unauthorized")
    insecure = count("sim-insecure")
    outside = count("sim-outside-zone")
    match_rate = analytics.correlation_summary().get("match_rate", 0)
    compliant = max(devices - at_risk - quarantined, 0)

    def live(n):
        # operational when we actually see data in the window; otherwise the
        # capability is wired but quiet.
        return "operational" if n > 0 else "ready"

    rows = [
        {
            "use_case": "Asset Inventory & Onboarding",
            "category": "Asset Inventory",
            "source": "ISE",
            "controls": "ISE-onboarded IoT inventory",
            "count": devices,
            "status": "operational" if devices else "none",
            "note": f"{devices:,} IoT devices onboarded in ISE and tracked live.",
            "links": [("ise-endpoints", "View devices")],
        },
        {
            "use_case": "Unauthorised Device Detection",
            "category": "Asset Inventory",
            "source": "ISE",
            "controls": "Blocked List / Unknown identity groups",
            "count": unauthorized,
            "status": live(unauthorized),
            "note": "Endpoints ISE blocked or could not identify (live from ISE).",
            "links": [("ise-unauthorized", "View endpoints")],
        },
        {
            "use_case": "IoT Device Risk Profile",
            "category": "Asset Inventory",
            "source": "FMC + ISE",
            "controls": "Devices with Medium+ events (last %dd)" % win,
            "count": at_risk,
            "status": live(at_risk),
            "note": "Devices at risk, ranked from the live event feed and "
            "correlated back to the ISE identity.",
            "links": [("sim-devices-at-risk", "View at-risk devices")],
        },
        {
            "use_case": "Real-Time Threat Detection & Incident Response",
            "category": "Threat Detection & Response",
            "source": "FMC",
            "controls": "Intrusion + Security-Intelligence events (last %dd)" % win,
            "count": intrusion + si,
            "status": live(intrusion + si),
            "note": "Live intrusion / SI events arriving on the eStreamer feed.",
            "links": [("sim-events", "View events")],
        },
        {
            "use_case": "Advanced Threat Analysis",
            "category": "Threat Detection & Response",
            "source": "FMC",
            "controls": "File + Malware events (last %dd)" % win,
            "count": malware + files,
            "status": live(malware + files),
            "note": "Malware / file inspection events from the feed "
            "(retrospective + EVE).",
            "links": [("sim-events", "View events")],
        },
        {
            "use_case": "Automated Threat Mitigation",
            "category": "Threat Detection & Response",
            "source": "FMC + ISE",
            "controls": "Blocked / quarantined devices",
            "count": quarantined,
            "status": "action-needed",
            "note": "Blocked events are detected live; AUTOMATED mitigation needs "
            "a write-capable ISE ANC/CoA account.",
            "links": [],
        },
        {
            "use_case": "Automated Device Isolation",
            "category": "Threat Detection & Response",
            "source": "ISE",
            "controls": "ISE ANC quarantine (write)",
            "count": quarantined,
            "status": "action-needed",
            "note": "Quarantined devices are tracked read-only; automated "
            "isolation needs an ANC write account.",
            "links": [("ise-quarantined", "View quarantined")],
        },
        {
            "use_case": "Compliance Tracking for IoT Devices",
            "category": "Compliance Management",
            "source": "FMC + ISE",
            "controls": "Compliant devices (not at-risk / quarantined)",
            "count": compliant,
            "status": "operational" if devices else "none",
            "note": "Compliance computed live from at-risk and quarantined "
            "devices; ISE↔FMC correlation at %d%%." % match_rate,
            "links": [("sim-devices-at-risk", "View at-risk")],
        },
        {
            "use_case": "Traffic / Flow Monitoring",
            "category": "Traffic Visibility",
            "source": "FMC",
            "controls": "Connection / flow events",
            "count": 0,
            "status": "disabled",
            "note": "Connection (flow) logging is disabled on this deployment, "
            "so top-talker / bandwidth data is not collected.",
            "links": [],
        },
        {
            "use_case": "Insecure Data Transfer",
            "category": "Traffic Visibility",
            "source": "FMC",
            "controls": "Clear-text protocol events",
            "count": insecure,
            "status": "operational" if insecure else "disabled",
            "note": "Telnet/FTP/HTTP/SNMP detection relied on connection events, "
            "which are currently disabled.",
            "links": [("sim-insecure", "View events")],
        },
        {
            "use_case": "Assets Talking Outside Allowed Zone",
            "category": "Traffic Visibility",
            "source": "FMC",
            "controls": "Blocked cross-zone events",
            "count": outside,
            "status": "operational" if outside else "disabled",
            "note": "Cross-zone / Outside-DMZ detection relied on connection "
            "events, which are currently disabled.",
            "links": [("sim-outside-zone", "View events")],
        },
    ]
    for r in rows:
        r["status_label"] = STATUS_LABELS[r["status"]]
    return rows
