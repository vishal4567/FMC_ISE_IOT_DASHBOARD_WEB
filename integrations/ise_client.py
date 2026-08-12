"""
Cisco ISE API client.

Talks to two ISE interfaces:

* **ERS** (External RESTful Services) on TCP/9060 using HTTP Basic auth.
  Used for configuration objects: endpoints, endpoint identity groups,
  network devices, and profiler profiles.
* **MnT** (Monitoring & Troubleshooting) session API on TCP/443.
  Used for live session data (IP, NAS/switch, posture) which ERS does not
  expose. MnT returns XML, so we parse it into dicts.

Design notes
------------
* Endpoint *lists* from ERS are cheap (id + MAC only). Full attributes
  (profile, identity group, static assignment, ...) require one GET per
  endpoint, so we enrich only up to ``detail_limit`` endpoints, in parallel.
* Every public method returns plain ``list[dict]`` / ``dict`` so the client
  is usable outside Django (scripts, tests) with no framework coupling.
* TLS verification is configurable and defaults off for lab/sandbox use.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import xml.etree.ElementTree as ET

import requests
from requests.auth import HTTPBasicAuth

from .exceptions import AuthError, ConfigError, IntegrationError

SOURCE = "ISE"


class ISEClient:
    def __init__(
        self,
        host,
        username,
        password,
        *,
        ers_port=9060,
        verify_tls=False,
        timeout=30,
        detail_limit=50,
        page_size=100,
        max_pages=20,
        site_attr="Location",
        device_type_attr="Device Type",
    ):
        if not host or not username or not password:
            raise ConfigError(
                "ISE host, username and password must all be configured.",
                source=SOURCE,
            )
        self.host = host.replace("https://", "").replace("http://", "").strip("/")
        self.username = username
        self.password = password
        self.ers_port = ers_port
        self.verify_tls = verify_tls
        self.timeout = timeout
        self.detail_limit = detail_limit
        self.page_size = page_size
        self.max_pages = max_pages
        # Names of the ISE endpoint *custom attributes* that carry site/location
        # and device type (as seen in the ISE Context Visibility columns). These
        # are org-defined, so they're configurable.
        self.site_attr = site_attr
        self.device_type_attr = device_type_attr

        self._session = requests.Session()
        self._session.auth = HTTPBasicAuth(username, password)
        self._session.verify = verify_tls

    # ------------------------------------------------------------------ #
    # Low-level helpers
    # ------------------------------------------------------------------ #
    @property
    def ers_base(self):
        return f"https://{self.host}:{self.ers_port}/ers/config"

    def _ers_get(self, path, params=None):
        url = f"{self.ers_base}{path}"
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        try:
            resp = self._session.get(
                url, params=params, headers=headers, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise IntegrationError(
                f"Could not reach ISE ERS at {self.host}:{self.ers_port}: {exc}",
                source=SOURCE,
            ) from exc

        if resp.status_code in (401, 403):
            raise AuthError(
                "ISE ERS authentication failed - check the username/password "
                "and that the account has the ERS Admin role enabled.",
                source=SOURCE,
                status=resp.status_code,
            )
        if resp.status_code >= 400:
            raise IntegrationError(
                f"ISE ERS returned an error for {path}",
                source=SOURCE,
                status=resp.status_code,
                detail=resp.text[:500],
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise IntegrationError(
                f"ISE ERS returned non-JSON for {path}",
                source=SOURCE,
                detail=resp.text[:500],
            ) from exc

    def _ers_collection(self, path, params=None):
        """Fetch a paginated ERS collection and return the flat resource list.

        ERS wraps lists as ``{"SearchResult": {"total": N, "resources": [...]}}``
        and paginates with ``page`` / ``size`` query params.
        """
        resources = []
        page = 1
        total = None
        while page <= self.max_pages:
            q = {"size": self.page_size, "page": page}
            if params:
                q.update(params)
            data = self._ers_get(path, params=q)
            result = data.get("SearchResult", {}) if isinstance(data, dict) else {}
            if total is None:
                total = result.get("total")
            batch = result.get("resources", []) or []
            resources.extend(batch)
            # Stop when a page is not full (last page reached)...
            if len(batch) < self.page_size:
                break
            # ...or once we've collected the reported total. This matters when
            # the total is an exact multiple of the page size (e.g. a filtered
            # group with 500 endpoints): requesting the next, out-of-range page
            # makes ISE ERS return HTTP 400 instead of an empty result.
            if total is not None and len(resources) >= total:
                break
            page += 1
        return resources

    # ------------------------------------------------------------------ #
    # Endpoints
    # ------------------------------------------------------------------ #
    def get_endpoints(self, detail=True):
        """Return endpoints, each labelled with its **identity group** and
        (where available) its **profiler-profile name**.

        Coverage strategy: every ISE endpoint belongs to exactly one identity
        group, and ISE's profile-based groups mirror the profiler result
        (Cisco-IP-Phone, Axis-Device, ...). So we build the base list by
        listing endpoints per identity group - this labels *all* endpoints
        with their profile category cheaply. We then enrich up to
        ``detail_limit`` endpoints (profiled ones first) with per-endpoint
        detail to resolve the granular profiler-profile name from ``profileId``.
        """
        base = self._endpoints_by_group()
        if not base:
            return []

        detailed = {}
        if detail:
            # Enrich profiled (non-"Unknown") endpoints first so their profile
            # name is always resolved, then spend any remaining budget.
            ordered = sorted(base, key=lambda b: b["group"].lower() == "unknown")
            to_enrich = ordered[: self.detail_limit]
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = {
                    pool.submit(self._get_endpoint_detail, b["id"]): b["id"]
                    for b in to_enrich
                    if b["id"]
                }
                for fut in as_completed(futures):
                    ep_id = futures[fut]
                    try:
                        detailed[ep_id] = fut.result()
                    except IntegrationError:
                        detailed[ep_id] = None

        # Resolve profiler-profile names ONLY for the profileIds actually present
        # in the enriched endpoints. Fetching the full profiler-profile catalogue
        # (~700 entries) just to name a handful of profiled endpoints costs ~40s
        # against the sandbox; per-id lookups are a couple of calls at most.
        profile_ids = {
            d.get("profileId") for d in detailed.values() if d and d.get("profileId")
        }
        profile_map = {pid: self._profiler_profile_name(pid) for pid in profile_ids}

        return [self._endpoint_row(b, detailed.get(b["id"]), profile_map) for b in base]

    @staticmethod
    def _flatten_custom_attrs(full):
        """Return the endpoint's custom attributes as a flat ``{name: value}``
        dict. ERS nests them as ``customAttributes.customAttributes``; the Open
        API returns a flat ``customAttributes`` - handle both."""
        if not isinstance(full, dict):
            return {}
        ca = full.get("customAttributes") or {}
        if isinstance(ca, dict) and isinstance(ca.get("customAttributes"), dict):
            ca = ca["customAttributes"]
        return ca if isinstance(ca, dict) else {}

    def _profiler_profile_name(self, profile_id):
        """Resolve a single profiler-profile id -> name, memoised per client."""
        cache = self.__dict__.setdefault("_profile_name_cache", {})
        if profile_id in cache:
            return cache[profile_id]
        name = profile_id
        try:
            data = self._ers_get(f"/profilerprofile/{profile_id}")
            if isinstance(data, dict):
                name = (data.get("ProfilerProfile", data) or {}).get("name", profile_id)
        except IntegrationError:
            pass
        cache[profile_id] = name
        return name

    def get_endpoint_count(self):
        """Cheap total endpoint count (single ERS call) for the W1 tile."""
        data = self._ers_get("/endpoint", params={"size": 1, "page": 1})
        result = data.get("SearchResult", {}) if isinstance(data, dict) else {}
        return int(result.get("total", 0) or 0)

    def get_endpoint_macs(self, limit=120):
        """A fast MAC sample (plain endpoint list, capped) - used to seed the
        correlation device pool without the full per-group/detail enrichment."""
        macs = []
        page = 1
        while page <= self.max_pages and len(macs) < limit:
            data = self._ers_get("/endpoint", params={"size": self.page_size, "page": page})
            result = data.get("SearchResult", {}) if isinstance(data, dict) else {}
            batch = result.get("resources", []) or []
            macs.extend(e.get("name", "") for e in batch if e.get("name"))
            if len(batch) < self.page_size:
                break
            page += 1
        return macs[:limit]

    def _endpoints_by_group(self):
        """Every endpoint as ``{id, mac, group}`` by walking identity groups."""
        rows = []
        for g in self.get_endpoint_groups():
            for e in self.get_endpoints_in_group(g["id"]):
                rows.append(
                    {"id": e.get("id", ""), "mac": e.get("name", ""), "group": g["name"]}
                )
        return rows

    def _get_endpoint_detail(self, endpoint_id):
        data = self._ers_get(f"/endpoint/{endpoint_id}")
        return data.get("ERSEndPoint", data) if isinstance(data, dict) else {}

    def _endpoint_row(self, base, full, profile_map):
        row = {
            "mac": base["mac"],
            "identity_group": base["group"],
            "endpoint_profile": "",
            "static_group_assignment": "",
            "endpoint_id": base["id"],
            "description": "",
            "custom_attributes": {},
            "site": "",
            "device_type_attr": "",
        }
        if full:
            pid = full.get("profileId", "")
            if pid:
                row["endpoint_profile"] = profile_map.get(pid, pid)
            else:
                row["endpoint_profile"] = "Unknown / unprofiled"
            row["static_group_assignment"] = full.get("staticGroupAssignment", "")
            row["description"] = full.get("description", "")
            # Org-defined custom attributes carry site (Location) + device type,
            # as shown in the ISE Context Visibility columns.
            ca = self._flatten_custom_attrs(full)
            row["custom_attributes"] = ca
            row["site"] = str(ca.get(self.site_attr, "") or "")
            row["device_type_attr"] = str(ca.get(self.device_type_attr, "") or "")
        else:
            # Not enriched - identity group still conveys the profile category.
            row["endpoint_profile"] = "(detail not fetched)"
        return row

    # ------------------------------------------------------------------ #
    # Endpoint identity groups
    # ------------------------------------------------------------------ #
    def get_endpoint_groups(self):
        groups = self._ers_collection("/endpointgroup")
        return [
            {
                "name": g.get("name", ""),
                "id": g.get("id", ""),
                "description": g.get("description", ""),
            }
            for g in groups
        ]

    # ------------------------------------------------------------------ #
    # Unauthorised device detection (partial, from ERS group membership)
    #
    # A full implementation would read failed-auth / rejected-endpoint events
    # from the MnT API. As a REST-only approximation we surface endpoints that
    # ISE has placed in a "deny"-style identity group (Blocked List) or could
    # not identify (Unknown) - both are strong unauthorised-device signals.
    # ------------------------------------------------------------------ #
    UNAUTHORIZED_GROUP_PATTERNS = (
        "block",  # "Blocked List", blocklist, blacklist
        "unknown",
        "unauth",
        "deny",
        "reject",
        "quarantine",
        "blacklist",
    )

    @classmethod
    def _unauthorized_reason(cls, group_name):
        name = group_name.lower()
        if "block" in name or "blacklist" in name or "deny" in name:
            return "On ISE Blocked List - access denied"
        if "unknown" in name:
            return "Unprofiled / unidentified endpoint"
        if "quarantine" in name:
            return "Quarantined by policy"
        return f"Member of restricted group '{group_name}'"

    def get_endpoints_in_group(self, group_id):
        """All endpoints whose identity group is ``group_id`` (ERS filter)."""
        return self._ers_collection(
            "/endpoint", params={"filter": f"groupId.EQ.{group_id}"}
        )

    def get_unauthorized_endpoints(self):
        groups = self.get_endpoint_groups()
        matched = [
            g
            for g in groups
            if any(p in g["name"].lower() for p in self.UNAUTHORIZED_GROUP_PATTERNS)
        ]
        rows = []
        for g in matched:
            reason = self._unauthorized_reason(g["name"])
            for e in self.get_endpoints_in_group(g["id"]):
                rows.append(
                    {
                        "mac": e.get("name", ""),
                        "endpoint_id": e.get("id", ""),
                        "unauthorized_group": g["name"],
                        "reason": reason,
                        "description": e.get("description", ""),
                        "detected_via": "ERS group membership",
                    }
                )
        return rows

    def unauthorized_group_summary(self):
        """Names of the identity groups treated as 'unauthorised' signals -
        used by the UI to explain what this report is based on."""
        return [
            g["name"]
            for g in self.get_endpoint_groups()
            if any(p in g["name"].lower() for p in self.UNAUTHORIZED_GROUP_PATTERNS)
        ]

    # ------------------------------------------------------------------ #
    # Network devices (NADs - switches / WLCs)
    # ------------------------------------------------------------------ #
    def get_network_devices(self):
        devices = self._ers_collection("/networkdevice")
        return [
            {
                "name": d.get("name", ""),
                "id": d.get("id", ""),
                "description": d.get("description", ""),
            }
            for d in devices
        ]

    # ------------------------------------------------------------------ #
    # Profiler profiles (device categories: camera, printer, ...)
    # ------------------------------------------------------------------ #
    def get_profiler_profiles(self):
        try:
            profiles = self._ers_collection("/profilerprofile")
        except IntegrationError as exc:
            # Not all ISE versions expose this ERS resource (404). Any other
            # error (auth, rate limit, unreachable) must surface, not hide.
            if exc.status in (404, 405):
                return []
            raise
        return [
            {
                "name": p.get("name", ""),
                "id": p.get("id", ""),
                "description": p.get("description", ""),
            }
            for p in profiles
        ]

    # ------------------------------------------------------------------ #
    # MnT - live sessions (XML API on 443)
    # ------------------------------------------------------------------ #
    def get_active_sessions(self):
        """Return active authentication sessions from the MnT API.

        Endpoint: ``/admin/API/mnt/Session/ActiveList`` (XML response).
        Gives IP address, NAS/switch, and audit-session context that ERS
        endpoints do not carry.
        """
        url = f"https://{self.host}/admin/API/mnt/Session/ActiveList"
        try:
            resp = self._session.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            raise IntegrationError(
                f"Could not reach ISE MnT at {self.host}: {exc}", source=SOURCE
            ) from exc
        if resp.status_code in (401, 403):
            raise AuthError(
                "ISE MnT authentication failed.", source=SOURCE, status=resp.status_code
            )
        if resp.status_code >= 400:
            raise IntegrationError(
                "ISE MnT returned an error.",
                source=SOURCE,
                status=resp.status_code,
                detail=resp.text[:500],
            )
        return self._parse_mnt_sessions(resp.text)

    @staticmethod
    def _parse_mnt_sessions(xml_text):
        rows = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return rows
        # <activeList><activeSession>...<field>value</field></activeSession>...
        for session in root.iter("activeSession"):
            row = {child.tag: (child.text or "").strip() for child in session}
            if row:
                rows.append(row)
        return rows

    # ------------------------------------------------------------------ #
    # Connectivity test
    # ------------------------------------------------------------------ #
    def test_connection(self):
        """Cheap probe used by the status dashboard. Returns a dict."""
        try:
            data = self._ers_get("/endpoint", params={"size": 1, "page": 1})
            total = data.get("SearchResult", {}).get("total", "unknown")
            return {"ok": True, "detail": f"ERS reachable (endpoint total: {total})"}
        except IntegrationError as exc:
            return {"ok": False, "detail": str(exc)}
