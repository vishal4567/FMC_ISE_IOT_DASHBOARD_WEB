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
* This is a large ISE (tens of thousands of endpoints). We never walk every
  endpoint: the inventory is IoT-scoped via ISE server-side filters
  (``logicalProfileName`` / ``profileId``) and only that small set is enriched
  (device type from ``mfcAttributes``; site from session -> NAS -> NAD Location).
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
        page_size=100,
        max_pages=200,
        openapi_page_size=500,
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
        self.page_size = page_size
        self.max_pages = max_pages
        self.openapi_page_size = openapi_page_size
        # Endpoint custom attribute that (in some orgs) carries device type; used
        # only as a last-resort fallback - the primary source is mfcAttributes.
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
    # IoT-scoped endpoint inventory
    #
    # This is a large ISE (tens of thousands of endpoints), so we NEVER walk
    # every endpoint. We import only endpoints whose profiling policy / logical
    # profile is in the IoT allow-list, using ISE's server-side filters, then
    # enrich that small set with device type (mfcAttributes) and site (session
    # -> NAS -> NAD Location group).
    # ------------------------------------------------------------------ #
    def resolve_profile_ids(self, names):
        """Map IoT profiling-policy *names* -> ids via ``/profilerprofile``.
        Returns ``{id: name}`` for the names that exist (unmatched are skipped).
        Fetches the profiler catalogue once - run daily, not hourly."""
        wanted = {n.strip().lower() for n in names if n and n.strip()}
        out = {}
        if not wanted:
            return out
        for p in self.get_profiler_profiles():
            nm = p.get("name") or ""
            if nm.lower() in wanted and p.get("id"):
                out[p["id"]] = nm
        return out

    def iot_endpoint_refs(self, *, logical_profiles=(), profile_ids=()):
        """Light ``{MAC: {id, mac, ...}}`` for IoT endpoints, via ERS server-side
        filters. ``logicalProfileName`` is preferred (one filtered call per
        logical profile); ``profileId`` is the per-policy fallback."""
        refs = {}
        for lp in logical_profiles:
            if not lp:
                continue
            for e in self._ers_collection("/endpoint", {"filter": f"logicalProfileName.EQ.{lp}"}):
                mac = (e.get("name") or "").upper()
                if mac:
                    refs.setdefault(mac, {"id": e.get("id", ""), "mac": mac})["logical_profile"] = lp
        for pid in profile_ids:
            if not pid:
                continue
            for e in self._ers_collection("/endpoint", {"filter": f"profileId.EQ.{pid}"}):
                mac = (e.get("name") or "").upper()
                if mac:
                    refs.setdefault(mac, {"id": e.get("id", ""), "mac": mac})["profile_id"] = pid
        return refs

    def endpoint_detail(self, endpoint_id):
        """Full ERS endpoint object (incl. ``mfcAttributes`` + ``profileId``)."""
        data = self._ers_get(f"/endpoint/{endpoint_id}")
        return data.get("ERSEndPoint", data) if isinstance(data, dict) else {}

    @staticmethod
    def mfc_device_type(full):
        """Device type from Cisco endpoint fingerprinting - the ISE Context
        Visibility "Device Type" column (e.g. 'Zebra-Device')."""
        mfc = (full or {}).get("mfcAttributes") or {}
        dt = mfc.get("mfcDeviceType") or []
        if isinstance(dt, list):
            return str(dt[0]) if dt else ""
        return str(dt or "")

    @staticmethod
    def mfc_manufacturer(full):
        mfc = (full or {}).get("mfcAttributes") or {}
        m = mfc.get("mfcHardwareManufacturer") or []
        if isinstance(m, list):
            return str(m[0]) if m else ""
        return str(m or "")

    @staticmethod
    def _flatten_custom_attrs(full):
        """Endpoint custom attributes as a flat ``{name: value}`` dict (ERS nests
        them as ``customAttributes.customAttributes``)."""
        if not isinstance(full, dict):
            return {}
        ca = full.get("customAttributes") or {}
        if isinstance(ca, dict) and isinstance(ca.get("customAttributes"), dict):
            ca = ca["customAttributes"]
        return ca if isinstance(ca, dict) else {}

    def enrich_endpoint(self, ref, profile_name_by_id=None):
        """Turn a light ref into a full inventory row: device type (mfc ->
        profile name -> custom attr), profile, manufacturer."""
        profile_name_by_id = profile_name_by_id or {}
        full = {}
        try:
            full = self.endpoint_detail(ref["id"]) if ref.get("id") else {}
        except IntegrationError:
            full = {}
        pid = full.get("profileId", "") or ""
        profile = profile_name_by_id.get(pid, "") or ref.get("logical_profile", "")
        device_type = (
            self.mfc_device_type(full)
            or profile
            or self._flatten_custom_attrs(full).get(self.device_type_attr, "")
        )
        return {
            "mac": ref["mac"],
            "endpoint_id": ref.get("id", ""),
            "profile_id": pid or ref.get("profile_id", ""),
            "endpoint_profile": profile,
            "logical_profile": ref.get("logical_profile", ""),
            "device_type": str(device_type or ""),
            "manufacturer": self.mfc_manufacturer(full),
            "description": full.get("description", ""),
            "site": "",   # filled by the location resolver
            "ip": "",
        }

    def get_endpoint_count(self):
        """Cheap total endpoint count (single ERS call) for the W1 tile."""
        data = self._ers_get("/endpoint", params={"size": 1, "page": 1})
        result = data.get("SearchResult", {}) if isinstance(data, dict) else {}
        return int(result.get("total", 0) or 0)

    # ------------------------------------------------------------------ #
    # Location: NAD Location groups + live session lookup
    # ------------------------------------------------------------------ #
    @staticmethod
    def _location_from_groups(groups):
        """Deepest site from a NAD's ``NetworkDeviceGroupList``. Root
        'Location#All Locations' (no sub-tree) yields '' (not a real site)."""
        for g in groups or []:
            if isinstance(g, str) and g.startswith("Location#"):
                parts = g.split("#")
                if len(parts) >= 3:      # Location # All Locations # <site> [ # ...]
                    return parts[-1].strip()
        return ""

    def nad_location_map(self):
        """``{key: site}`` from every NAD's Location group, keyed by BOTH each
        NAD IP and the NAD name (sessions may report either). Slow (one detail
        GET per NAD) - run daily and cache."""
        out = {}
        light = self._ers_collection("/networkdevice")
        ids = [d.get("id") for d in light if d.get("id")]
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(self._networkdevice_detail, i): i for i in ids}
            for fut in as_completed(futures):
                try:
                    nad = fut.result()
                except IntegrationError:
                    continue
                site = self._location_from_groups(nad.get("NetworkDeviceGroupList"))
                if not site:
                    continue
                if nad.get("name"):
                    out[nad["name"]] = site
                for ipentry in nad.get("NetworkDeviceIPList", []) or []:
                    ip = (ipentry or {}).get("ipaddress")
                    if ip:
                        out[ip] = site
        return out

    def _networkdevice_detail(self, nad_id):
        data = self._ers_get(f"/networkdevice/{nad_id}")
        return data.get("NetworkDevice", data) if isinstance(data, dict) else {}

    def session_by_mac(self, mac):
        """Live MnT session for a MAC -> flat field dict (nas_ip_address,
        framed_ip_address, ...). Empty dict if no active session."""
        url = f"https://{self.host}/admin/API/mnt/Session/MACAddress/{mac}"
        try:
            resp = self._session.get(url, timeout=self.timeout)
        except requests.RequestException:
            return {}
        if resp.status_code >= 400:
            return {}
        return self._parse_session_fields(resp.text)

    @staticmethod
    def _parse_session_fields(xml_text):
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return {}
        fields = {}
        # Take the last (most recent) session's leaf fields.
        containers = list(root.iter("activeSession")) or [root]
        for child in containers[-1].iter():
            if list(child):        # skip container nodes
                continue
            if child.tag and child.text and child.text.strip():
                fields[child.tag] = child.text.strip()
        return fields

    # ------------------------------------------------------------------ #
    # ISE Open API (/api/v1) - primary IoT discovery path
    #
    # /api/v1/endpoint supports a profileId filter and returns deviceType /
    # vendor / ipAddress / customAttributes INLINE, so one call per IoT profile
    # yields discovery + device type + IP with no ERS/MnT follow-up.
    # ------------------------------------------------------------------ #
    @property
    def openapi_base(self):
        return f"https://{self.host}/api/v1"

    def _openapi_get(self, path, params=None):
        url = f"{self.openapi_base}{path}"
        try:
            resp = self._session.get(
                url, params=params, headers={"Accept": "application/json"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise IntegrationError(
                f"Could not reach ISE Open API at {self.host}: {exc}", source=SOURCE
            ) from exc
        if resp.status_code in (401, 403):
            raise AuthError("ISE Open API authentication failed.",
                            source=SOURCE, status=resp.status_code)
        if resp.status_code >= 400:
            raise IntegrationError(
                f"ISE Open API error for {path}", source=SOURCE,
                status=resp.status_code, detail=resp.text[:400])
        try:
            return resp.json()
        except ValueError:
            return {}

    @staticmethod
    def _openapi_items(data):
        """Extract the list of endpoint objects from an Open API response,
        whatever container ISE wraps them in."""
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for k in ("response", "resources", "items", "SearchResult"):
                v = data.get(k)
                if isinstance(v, list):
                    return v
                if isinstance(v, dict) and isinstance(v.get("resources"), list):
                    return v["resources"]
        return []

    def openapi_endpoint_by_mac(self, mac):
        """Full Open API endpoint object for one MAC (untruncated)."""
        return self._openapi_get(f"/endpoint/{mac.strip().upper()}")

    def openapi_iot_endpoints(self, profile_ids):
        """``{MAC: endpoint_obj}`` for the given profileIds via the Open API
        filter. Falls back to one full paged scan (client-side filter) if this
        ISE rejects the profileId filter."""
        ids = [p for p in profile_ids if p]
        if not ids:
            return {}
        try:
            return self._openapi_by_filter(ids)
        except IntegrationError as exc:
            if getattr(exc, "status", None) == 400:   # filter unsupported
                return self._openapi_full_scan(set(ids))
            raise

    def _openapi_page(self, params):
        page, size = 1, self.openapi_page_size
        while page <= self.max_pages:
            q = {"size": size, "page": page}
            q.update(params)
            batch = self._openapi_items(self._openapi_get("/endpoint", q))
            if not batch:
                break
            yield from batch
            if len(batch) < size:
                break
            page += 1

    def _openapi_by_filter(self, ids):
        out = {}
        for pid in ids:
            for obj in self._openapi_page({"filter": f"profileId.EQ.{pid}"}):
                mac = (obj.get("mac") or obj.get("name") or "").upper()
                if mac:
                    out[mac] = obj
        return out

    def _openapi_full_scan(self, id_set):
        out = {}
        for obj in self._openapi_page({}):
            if obj.get("profileId") in id_set:
                mac = (obj.get("mac") or obj.get("name") or "").upper()
                if mac:
                    out[mac] = obj
        return out

    def map_openapi_endpoint(self, obj, profile_name_by_id=None):
        """Normalise an Open API endpoint object into an inventory row.
        device_type: Open API deviceType -> profile name -> vendor."""
        profile_name_by_id = profile_name_by_id or {}
        pid = obj.get("profileId", "") or ""
        profile = profile_name_by_id.get(pid, "")
        device_type = (
            str(obj.get("deviceType") or "").strip()
            or profile
            or str(obj.get("vendor") or "").strip()
        )
        return {
            "mac": (obj.get("mac") or obj.get("name") or "").upper(),
            "endpoint_id": obj.get("id", ""),
            "profile_id": pid,
            "endpoint_profile": profile,
            "logical_profile": "",
            "device_type": device_type,
            "manufacturer": str(obj.get("vendor") or ""),
            "description": obj.get("description", ""),
            "ip": str(obj.get("ipAddress") or ""),
            "site": "",
        }

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
