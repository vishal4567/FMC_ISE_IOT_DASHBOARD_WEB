"""
Cisco FMC (Secure Firewall Management Center) REST API client.

Auth flow
---------
1. ``POST /api/fmc_platform/v1/auth/generatetoken`` with HTTP Basic auth.
   FMC returns two headers we care about:
     * ``X-auth-access-token``  - sent on every subsequent request
     * ``DOMAINS``              - JSON list of {name, uuid}; we default to the
                                  first (Global) domain unless one is pinned.
2. All config calls hit ``/api/fmc_config/v1/domain/{domainUUID}/...`` and
   carry the access-token header.

Important scope note
--------------------
The FMC *config* REST API exposes managed devices, access/intrusion policies,
security zones, network objects, and **audit records** - but it does NOT
expose live intrusion / malware / connection *events*. Those require the
eStreamer streaming API or Security Analytics & Logging. The dashboard
therefore surfaces audit records as the closest event-like data available
over REST, and clearly labels the gap. See README for details.
"""
from __future__ import annotations

import json

import requests
from requests.auth import HTTPBasicAuth

from .exceptions import AuthError, ConfigError, IntegrationError

SOURCE = "FMC"


class FMCClient:
    def __init__(
        self,
        host,
        username,
        password,
        *,
        port=443,
        verify_tls=False,
        timeout=30,
        domain_uuid="",
        page_limit=100,
        max_pages=20,
    ):
        if not host or not username or not password:
            raise ConfigError(
                "FMC host, username and password must all be configured.",
                source=SOURCE,
            )
        self.host = host.replace("https://", "").replace("http://", "").strip("/")
        self.username = username
        self.password = password
        self.port = port
        self.verify_tls = verify_tls
        self.timeout = timeout
        self.page_limit = page_limit
        self.max_pages = max_pages

        self._session = requests.Session()
        self._session.verify = verify_tls

        self._access_token = None
        self._domain_uuid = domain_uuid or None
        self._domains = []

    @property
    def base_url(self):
        return f"https://{self.host}:{self.port}"

    # ------------------------------------------------------------------ #
    # Authentication
    # ------------------------------------------------------------------ #
    def login(self):
        url = f"{self.base_url}/api/fmc_platform/v1/auth/generatetoken"
        try:
            resp = self._session.post(
                url,
                auth=HTTPBasicAuth(self.username, self.password),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise IntegrationError(
                f"Could not reach FMC at {self.host}:{self.port}: {exc}",
                source=SOURCE,
            ) from exc

        if resp.status_code in (401, 403):
            raise AuthError(
                "FMC authentication failed - check username/password.",
                source=SOURCE,
                status=resp.status_code,
            )
        if resp.status_code >= 400:
            raise IntegrationError(
                "FMC token generation failed.",
                source=SOURCE,
                status=resp.status_code,
                detail=resp.text[:500],
            )

        token = resp.headers.get("X-auth-access-token")
        if not token:
            raise AuthError(
                "FMC did not return an access token.", source=SOURCE
            )
        self._access_token = token

        # Parse available domains and choose one if not pinned.
        raw_domains = resp.headers.get("DOMAINS")
        if raw_domains:
            try:
                self._domains = json.loads(raw_domains)
            except ValueError:
                self._domains = []
        if not self._domain_uuid:
            if self._domains:
                self._domain_uuid = self._domains[0].get("uuid")
            else:
                # Fallback to the well-known default global domain UUID.
                self._domain_uuid = "e276abec-e0f2-11e3-8169-6d9ed49b625f"
        return self

    def _ensure_auth(self):
        if not self._access_token:
            self.login()

    @property
    def domain_uuid(self):
        self._ensure_auth()
        return self._domain_uuid

    @property
    def domains(self):
        self._ensure_auth()
        return self._domains

    # ------------------------------------------------------------------ #
    # Low-level GET with pagination
    # ------------------------------------------------------------------ #
    def _get(self, path, params=None, _retried=False):
        self._ensure_auth()
        url = f"{self.base_url}{path}"
        headers = {
            "X-auth-access-token": self._access_token,
            "Accept": "application/json",
        }
        try:
            resp = self._session.get(
                url, params=params, headers=headers, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise IntegrationError(
                f"Could not reach FMC at {self.host}: {exc}", source=SOURCE
            ) from exc

        # A stale/expired token yields 401. Re-authenticate once and retry so a
        # long-lived client (token TTL ~30 min) heals itself transparently.
        # A 403 means authenticated-but-forbidden (role limit) - re-login won't
        # help, so surface it without churning FMC's limited token pool.
        if resp.status_code == 401 and not _retried:
            self._access_token = None
            return self._get(path, params=params, _retried=True)
        if resp.status_code == 401:
            raise AuthError(
                "FMC token rejected or expired.", source=SOURCE, status=resp.status_code
            )
        if resp.status_code == 403:
            raise AuthError(
                "FMC user is not authorized for this resource "
                "(the sandbox account may lack the required role).",
                source=SOURCE,
                status=403,
            )
        if resp.status_code >= 400:
            raise IntegrationError(
                f"FMC returned an error for {path}",
                source=SOURCE,
                status=resp.status_code,
                detail=resp.text[:500],
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise IntegrationError(
                f"FMC returned non-JSON for {path}",
                source=SOURCE,
                detail=resp.text[:500],
            ) from exc

    def _config_path(self, resource):
        return f"/api/fmc_config/v1/domain/{self.domain_uuid}{resource}"

    def _platform_path(self, resource):
        return f"/api/fmc_platform/v1/domain/{self.domain_uuid}{resource}"

    def _paginate(self, path, params=None, expanded=True):
        """Walk FMC ``offset``/``limit`` pagination and return all items."""
        items = []
        offset = 0
        for _ in range(self.max_pages):
            q = {"limit": self.page_limit, "offset": offset}
            if expanded:
                q["expanded"] = "true"
            if params:
                q.update(params)
            data = self._get(path, params=q)
            batch = data.get("items", []) if isinstance(data, dict) else []
            items.extend(batch)
            # FMC 'paging.count' is the size of the *current* page, not the
            # grand total, so we can't compare offset against it. Instead stop
            # on the universal signal: a short (or empty) page is the last one.
            if len(batch) < self.page_limit:
                break
            offset += self.page_limit
        return items

    def _paginate_optional(self, path, params=None, expanded=True):
        """Like ``_paginate`` but tolerates a resource that simply doesn't
        exist on this FMC version (HTTP 404) by returning ``[]``.

        Crucially it does NOT swallow auth (401/403), rate-limit (429), or
        other errors - those propagate so the UI shows a real failure instead
        of a misleading empty table.
        """
        try:
            return self._paginate(path, params=params, expanded=expanded)
        except IntegrationError as exc:
            if exc.status == 404:
                return []
            raise

    # ------------------------------------------------------------------ #
    # Managed devices
    # ------------------------------------------------------------------ #
    def get_devices(self):
        items = self._paginate(self._config_path("/devices/devicerecords"))
        rows = []
        for d in items:
            rows.append(
                {
                    "name": d.get("name", ""),
                    "id": d.get("id", ""),
                    "hostname": d.get("hostName", ""),
                    "model": d.get("model", ""),
                    "sw_version": d.get("sw_version", ""),
                    "health_status": d.get("healthStatus", ""),
                    "ftd_mode": d.get("ftdMode", ""),
                    "license": ", ".join(d.get("license_caps", []) or []),
                }
            )
        return rows

    # ------------------------------------------------------------------ #
    # Access control policies
    # ------------------------------------------------------------------ #
    def get_access_policies(self):
        items = self._paginate(self._config_path("/policy/accesspolicies"))
        rows = []
        for p in items:
            default = p.get("defaultAction", {}) or {}
            rows.append(
                {
                    "name": p.get("name", ""),
                    "id": p.get("id", ""),
                    "default_action": default.get("action", ""),
                    "description": p.get("description", ""),
                }
            )
        return rows

    def get_access_rules(self, policy_id, policy_name=""):
        """Rules within a single access policy (matched rule / action data)."""
        path = self._config_path(f"/policy/accesspolicies/{policy_id}/accessrules")
        items = self._paginate(path)
        rows = []
        for r in items:
            rows.append(
                {
                    "policy": policy_name,
                    "rule_name": r.get("name", ""),
                    "action": r.get("action", ""),
                    "enabled": r.get("enabled", ""),
                    "source_zones": _names(r.get("sourceZones")),
                    "dest_zones": _names(r.get("destinationZones")),
                    "ips_policy": (r.get("ipsPolicy", {}) or {}).get("name", ""),
                }
            )
        return rows

    def get_all_access_rules(self):
        rows = []
        for pol in self.get_access_policies():
            rows.extend(self.get_access_rules(pol["id"], pol["name"]))
        return rows

    # ------------------------------------------------------------------ #
    # Intrusion policies
    # ------------------------------------------------------------------ #
    def get_intrusion_policies(self):
        items = self._paginate_optional(self._config_path("/policy/intrusionpolicies"))
        return [
            {
                "name": p.get("name", ""),
                "id": p.get("id", ""),
                "inspection_mode": p.get("inspectionMode", ""),
                "base_policy": (p.get("basePolicy", {}) or {}).get("name", ""),
                "description": p.get("description", ""),
            }
            for p in items
        ]

    # ------------------------------------------------------------------ #
    # File / malware policies  (retrospective + malware defense controls)
    # ------------------------------------------------------------------ #
    def get_file_policies(self):
        items = self._paginate_optional(self._config_path("/policy/filepolicies"))
        return [
            {
                "name": p.get("name", ""),
                "id": p.get("id", ""),
                "description": p.get("description", ""),
            }
            for p in items
        ]

    # ------------------------------------------------------------------ #
    # Prefilter policies  (early traffic handling / tunnel + bypass)
    # ------------------------------------------------------------------ #
    def get_prefilter_policies(self):
        items = self._paginate_optional(self._config_path("/policy/prefilterpolicies"))
        return [
            {
                "name": p.get("name", ""),
                "id": p.get("id", ""),
                "description": p.get("description", ""),
            }
            for p in items
        ]

    # ------------------------------------------------------------------ #
    # Security zones
    # ------------------------------------------------------------------ #
    def get_security_zones(self):
        items = self._paginate_optional(self._config_path("/object/securityzones"))
        return [
            {
                "name": z.get("name", ""),
                "id": z.get("id", ""),
                "interface_mode": z.get("interfaceMode", ""),
            }
            for z in items
        ]

    # ------------------------------------------------------------------ #
    # Network (host) objects
    # ------------------------------------------------------------------ #
    def get_network_objects(self):
        items = self._paginate_optional(self._config_path("/object/networkaddresses"))
        return [
            {
                "name": o.get("name", ""),
                "type": o.get("type", ""),
                "value": o.get("value", ""),
                "id": o.get("id", ""),
            }
            for o in items
        ]

    # ------------------------------------------------------------------ #
    # Audit records - closest thing to "events" over the REST API
    # ------------------------------------------------------------------ #
    def get_audit_records(self):
        items = self._paginate_optional(
            self._platform_path("/audit/auditrecords"), expanded=True
        )
        rows = []
        for a in items:
            rows.append(
                {
                    "time": a.get("time", ""),
                    "user": (a.get("user", {}) or {}).get("name", ""),
                    "subsystem": a.get("subsystem", ""),
                    "source": a.get("source", ""),
                    "action": a.get("action", ""),
                    "message": a.get("message", ""),
                    "domain": (a.get("domain", {}) or {}).get("name", ""),
                }
            )
        return rows

    # ------------------------------------------------------------------ #
    # Connectivity test
    # ------------------------------------------------------------------ #
    def test_connection(self):
        try:
            self.login()
            names = ", ".join(d.get("name", "?") for d in self._domains) or "n/a"
            return {
                "ok": True,
                "detail": f"Authenticated. Domain(s): {names}",
            }
        except IntegrationError as exc:
            return {"ok": False, "detail": str(exc)}


def _names(obj):
    """Flatten an FMC {'objects': [{'name': ...}]} container to a string."""
    if not obj:
        return ""
    objects = obj.get("objects", []) if isinstance(obj, dict) else obj
    return ", ".join(o.get("name", "") for o in objects if isinstance(o, dict))
