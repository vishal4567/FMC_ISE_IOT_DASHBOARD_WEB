"""
API probe / diagnostic harness.

    python manage.py probe_apis                 # writes to ./api_out/
    python manage.py probe_apis --out-dir /tmp/probe --full-endpoint

Hits every ISE (ERS + MnT), FMC and eStreamer API the app uses, printing each
result **live as it runs**, and writes **one JSON file per probe** into the
output directory (plus device_types.json and a _summary.json index). Hand the
folder back to tune the client field mappings to your environment.

All calls are read-only.
"""
import json
import os
import time

from django.core.management.base import BaseCommand


def _sample(obj, container_keys=("resources", "items"), n=2):
    """Keep the first ``n`` items of a list-ish response so field names are
    visible without dumping thousands of rows."""
    if isinstance(obj, dict):
        for k in container_keys:
            if isinstance(obj.get(k), list):
                trimmed = dict(obj)
                trimmed[k] = obj[k][:n]
                trimmed["_total_in_container"] = len(obj[k])
                return trimmed
        return obj
    if isinstance(obj, list):
        return obj[:n]
    return obj


class Command(BaseCommand):
    help = "Probe ISE/FMC/eStreamer APIs; write one file per response."

    def add_arguments(self, parser):
        parser.add_argument("--out-dir", default="api_out",
                            help="directory for the per-probe JSON files")
        parser.add_argument("--full-endpoint", action="store_true",
                            help="(with --all) also fetch one full /endpoint/{id} JSON")
        parser.add_argument("--mac", default="",
                            help="dump the FULL endpoint JSON (ERS + Open API) for "
                                 "this MAC, incl. all customAttributes — use it to "
                                 "confirm the site/device-type attribute names")
        parser.add_argument("--all", action="store_true",
                            help="also run the full ERS catalogue, MnT, FMC and "
                                 "eStreamer probes (default: only Open API + location)")

    def handle(self, *args, **opts):
        self._out_dir = opts["out_dir"]
        os.makedirs(self._out_dir, exist_ok=True)
        self._opts = opts
        self._t0 = time.perf_counter()
        self._summary = []

        self._init_ise()

        # Default: focus on ISE Open API + device location only (clear output).
        self._section("Cisco ISE (Open API /api/v1)")
        self._probe_ise_openapi()
        self._section("Cisco ISE (Location)")
        self._probe_location()

        # Targeted: dump one MAC's full endpoint (ERS + Open API) with all its
        # custom attributes — the definitive way to see where the ISE Context
        # Visibility "Location" / "Device Type" columns come from.
        if opts.get("mac"):
            self._section(f"Cisco ISE (endpoint by MAC: {opts['mac']})")
            self._probe_endpoint_by_mac(opts["mac"])

        # Everything else only with --all.
        if opts["all"]:
            self._section("Cisco ISE (ERS catalogue)")
            self._probe_ise_ers()
            self._section("Cisco ISE (MnT)")
            self._probe_ise_mnt()
            self._section("Cisco FMC")
            self._probe_fmc()
            self._section("eStreamer")
            self._probe_estreamer()

        with open(os.path.join(self._out_dir, "_summary.json"), "w", encoding="utf-8") as f:
            json.dump(self._summary, f, indent=2, default=str)

        elapsed = round(time.perf_counter() - self._t0, 1)
        ok = sum(1 for s in self._summary if s["ok"])
        err = len(self._summary) - ok
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Done in {elapsed}s — {ok} OK, {err} error(s). "
            f"Files in {self._out_dir}/ (one per probe + _summary.json)"))
        if err:
            self.stdout.write(self.style.WARNING(
                "Review the ERR lines / files above; send the folder back for tuning."))

    # ---- live-printing runner (writes one file per probe) -------------------
    def _section(self, title):
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(f"=== {title} ==="))

    def _run(self, name, fn):
        self.stdout.write(f"  → {name:24} ... ", ending="")
        self.stdout.flush()
        start = time.perf_counter()
        try:
            data = fn()
            secs = round(time.perf_counter() - start, 2)
            result = {"ok": True, "seconds": secs, "sample": data}
            self.stdout.write(self.style.SUCCESS(f"OK   {secs:>6}s  {self._count(data)}"))
        except Exception as exc:
            secs = round(time.perf_counter() - start, 2)
            result = {"ok": False, "seconds": secs, "error": str(exc)}
            self.stdout.write(self.style.ERROR(f"ERR  {secs:>6}s  {str(exc)[:80]}"))
        self._write(name, result)
        self._summary.append({"probe": name, "ok": result["ok"],
                              "seconds": result["seconds"], "error": result.get("error", "")})
        return result

    def _write(self, name, obj):
        with open(os.path.join(self._out_dir, f"{name}.json"), "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, default=str)

    @staticmethod
    def _count(data):
        if isinstance(data, dict):
            sr = data.get("SearchResult") or {}
            if "total" in sr:
                return f"total={sr['total']}"
            if "_total_in_container" in data:
                return f"items={data['_total_in_container']}"
            if "status" in data:
                return f"http={data['status']}"
            if "paging" in data:
                return f"count={data.get('paging', {}).get('count', '?')}"
        if isinstance(data, list):
            return f"items={len(data)}"
        return ""

    # ---- client init --------------------------------------------------------
    def _init_ise(self):
        from dashboard import services
        try:
            self._ise = services.get_ise_client()
        except Exception as exc:
            self.stdout.write(self.style.ERROR(f"  ISE client init failed: {exc}"))
            self._write("ise.client", {"ok": False, "error": str(exc)})
            self._ise = None

    # ---- ISE Location -------------------------------------------------------
    def _probe_location(self):
        """Where a device's location lives in ISE: the Network Device Group
        hierarchy (Location tree) and the Location group on the switch/WLC (NAD)
        the endpoint connects through; plus the MnT session that maps a device
        MAC to that NAD."""
        ise = getattr(self, "_ise", None)
        if not ise:
            return
        self._run("ise.location.ndg_tree",
                  lambda: _sample(ise._ers_get("/networkdevicegroup", {"size": 100, "page": 1}), n=100))
        self._run("ise.location.nad_full", lambda: self._full_network_device(ise))
        mac = self._one_mac(ise)
        if mac:
            self._run("ise.location.session_by_mac",
                      lambda m=mac: self._mnt(ise, f"/Session/MACAddress/{m}"))

    # ---- ISE ERS catalogue (only with --all) --------------------------------
    def _probe_ise_ers(self):
        ise = getattr(self, "_ise", None)
        if not ise:
            return
        probes = {
            "ise.endpoints": ("/endpoint", {"size": 2, "page": 1}),
            "ise.endpoint_groups": ("/endpointgroup", {"size": 2, "page": 1}),
            "ise.network_devices": ("/networkdevice", {"size": 2, "page": 1}),
            "ise.profiler_profiles": ("/profilerprofile", {"size": 2, "page": 1}),
        }
        for name, (path, params) in probes.items():
            self._run(name, lambda p=path, q=params: _sample(ise._ers_get(p, q)))
        self._run("ise.device_types", lambda: self._device_types(ise))
        if self._opts.get("full_endpoint"):
            self._run("ise.endpoint_full", lambda: self._full_endpoint(ise))

    def _device_types(self, ise):
        profiles = sorted(p["name"] for p in ise.get_profiler_profiles() if p.get("name"))
        groups = sorted(g["name"] for g in ise.get_endpoint_groups() if g.get("name"))
        cat = {"identity_groups": groups, "profiler_profiles": profiles,
               "counts": {"identity_groups": len(groups), "profiler_profiles": len(profiles)}}
        with open(os.path.join(self._out_dir, "device_types.json"), "w", encoding="utf-8") as f:
            json.dump(cat, f, indent=2)
        return cat

    def _full_endpoint(self, ise):
        lst = ise._ers_get("/endpoint", {"size": 1, "page": 1})
        res = (lst.get("SearchResult", {}) or {}).get("resources", [])
        if not res:
            return {"note": "no endpoints"}
        return ise._ers_get(f"/endpoint/{res[0]['id']}")

    def _probe_endpoint_by_mac(self, mac):
        """Fetch ONE endpoint by MAC via both ERS and the Open API and dump the
        FULL JSON (not truncated), so the exact custom-attribute names for
        site/device-type are visible. Writes ise.endpoint_by_mac.* files."""
        ise = getattr(self, "_ise", None)
        if not ise:
            self.stdout.write("  (ISE client unavailable)")
            return
        mac = mac.strip().upper()

        def _ers_by_mac():
            # ERS supports filtering the endpoint list by MAC.
            lst = ise._ers_get("/endpoint", {"filter": f"mac.EQ.{mac}"})
            res = (lst.get("SearchResult", {}) or {}).get("resources", [])
            if not res:
                return {"note": f"no ERS endpoint for {mac}"}
            full = ise._ers_get(f"/endpoint/{res[0]['id']}")
            ep = full.get("ERSEndPoint", full) if isinstance(full, dict) else full
            return {"mfcAttributes": (ep or {}).get("mfcAttributes"),
                    "customAttributes": (ep or {}).get("customAttributes"),
                    "device_type_resolved": ise.mfc_device_type(ep or {}),
                    "full": full}

        def _openapi_by_mac():
            # Open API returns rich attributes inline, incl. deviceType + customAttributes.
            url = f"https://{ise.host}/api/v1/endpoint/{mac}"
            r = ise._session.get(url, headers={"Accept": "application/json"},
                                 timeout=ise.timeout)
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:150]}")
            return r.json()

        self._run("ise.endpoint_by_mac.ers", _ers_by_mac)
        self._run("ise.endpoint_by_mac.openapi", _openapi_by_mac)

    def _full_network_device(self, ise):
        lst = ise._ers_get("/networkdevice", {"size": 1, "page": 1})
        res = (lst.get("SearchResult", {}) or {}).get("resources", [])
        if not res:
            return {"note": "no network devices"}
        return ise._ers_get(f"/networkdevice/{res[0]['id']}")

    # ---- ISE Open API (/api/v1) ---------------------------------------------
    def _probe_ise_openapi(self):
        ise = getattr(self, "_ise", None)
        if not ise:
            self.stdout.write("  (ISE client unavailable - skipping Open API)")
            return
        # deployment/node reveals the ISE version.
        self._run("ise.openapi.deployment_node",
                  lambda: self._openapi(ise, "/deployment/node", {}))
        # Fetch endpoints via the Open API — the list already carries the rich
        # attributes inline (profileId, groupId, ipAddress, deviceType, vendor,
        # serialNumber, custom/mdm attributes). An endpoint that is currently
        # connected/active shows a non-null "connectedLinks".
        self._run("ise.openapi.endpoints",
                  lambda: self._openapi(ise, "/endpoint", {"size": 20, "page": 1}))
        self._run("ise.openapi.endpoint_group",
                  lambda: self._openapi(ise, "/endpoint-group", {"size": 5, "page": 1}))
        # IoT scoping: confirm we can fetch ONLY endpoints in an allow-listed
        # profile / logical profile (the whole point of the hourly sync).
        self._run("ise.iot.profile_filter", lambda: self._iot_filter(ise))

    def _iot_filter(self, ise):
        """Resolve the first IoT profiling policy + logical profile from settings
        and prove the three filter paths work against this ISE:
          - ERS  filter=profileId.EQ.<id>
          - ERS  filter=logicalProfileName.EQ.<name>
          - Open API filter=profileId.EQ.<id>
        Returns counts so we know which method to use for the sync."""
        from django.conf import settings
        cfg = settings.ISE
        out = {"tried": {}, "counts": {}}

        # a) resolve one profile name -> id
        names = cfg.get("IOT_PROFILES") or []
        prof_map = ise.resolve_profile_ids(names[:5]) if names else {}
        pid = next(iter(prof_map), "")
        out["resolved_profile"] = {pid: prof_map.get(pid, "")} if pid else {}

        # b) ERS by profileId
        if pid:
            try:
                res = ise._ers_get("/endpoint", {"filter": f"profileId.EQ.{pid}", "size": 5})
                out["counts"]["ers_by_profileId"] = (
                    (res.get("SearchResult", {}) or {}).get("total"))
                out["tried"]["ers_by_profileId"] = "ok"
            except Exception as exc:
                out["tried"]["ers_by_profileId"] = str(exc)[:120]

        # c) ERS by logicalProfileName
        lp = (cfg.get("IOT_LOGICAL_PROFILES") or [""])[0]
        if lp:
            try:
                res = ise._ers_get("/endpoint", {"filter": f"logicalProfileName.EQ.{lp}", "size": 5})
                out["counts"]["ers_by_logicalProfile"] = (
                    (res.get("SearchResult", {}) or {}).get("total"))
                out["tried"][f"ers_by_logicalProfile:{lp}"] = "ok"
            except Exception as exc:
                out["tried"][f"ers_by_logicalProfile:{lp}"] = str(exc)[:120]

        # d) Open API by profileId - dump the FIRST FULL object untruncated so we
        #    can see whether deviceType / vendor / ipAddress populate (=> no ERS).
        if pid:
            try:
                raw = ise._openapi_get("/endpoint", {"filter": f"profileId.EQ.{pid}", "size": 5})
                items = ise._openapi_items(raw)
                out["tried"]["openapi_by_profileId"] = "ok"
                out["counts"]["openapi_by_profileId"] = len(items)
                first = items[0] if items else {}
                out["openapi_first_full"] = first
                out["openapi_fields_present"] = {
                    k: (k in first and first.get(k) not in ("", None, [], {}))
                    for k in ("profileId", "deviceType", "vendor", "ipAddress",
                              "productId", "hardwareRevision", "customAttributes")
                }
            except Exception as exc:
                out["tried"]["openapi_by_profileId"] = str(exc)[:150]
        return out

    def _openapi(self, ise, path, params):
        """ISE Open API GET. Base: https://<host>/api/v1 (always :443).
        Raises on HTTP >= 400 so 404 (not enabled/older ISE) / 401 shows as ERR."""
        url = f"https://{ise.host}/api/v1{path}"
        r = ise._session.get(url, params=params,
                             headers={"Accept": "application/json"}, timeout=ise.timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:150]}")
        try:
            data = r.json()
        except ValueError:
            return {"status": r.status_code, "body": r.text[:2000]}
        return _sample(data, container_keys=("response", "resources", "items", "SearchResult"))

    # ---- ISE MnT (session/location; XML) ------------------------------------
    def _probe_ise_mnt(self):
        ise = getattr(self, "_ise", None)
        if not ise:
            self.stdout.write("  (ISE client unavailable - skipping MnT)")
            return
        self._run("ise.mnt.version", lambda: self._mnt(ise, "/Version"))
        self._run("ise.mnt.active_count", lambda: self._mnt(ise, "/Session/ActiveCount"))
        self._run("ise.mnt.active_list", lambda: self._mnt(ise, "/Session/ActiveList"))
        # session-by-MAC for a real endpoint -> carries NAS/switch + (some ISE) location
        mac = self._one_mac(ise)
        if mac:
            self._run("ise.mnt.session_by_mac",
                      lambda m=mac: self._mnt(ise, f"/Session/MACAddress/{m}"))
        else:
            self.stdout.write("  (no endpoint MAC available for session_by_mac)")

    def _mnt(self, ise, path):
        """Raw MnT GET (returns XML). Base: https://<host>/admin/API/mnt.
        Raises on HTTP >= 400 so it shows as ERR (e.g. 401 = account lacks MnT)."""
        url = f"https://{ise.host}/admin/API/mnt{path}"
        r = ise._session.get(url, timeout=ise.timeout)
        body = r.text
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {body[:120]}")
        return {"url": url, "status": r.status_code,
                "content_type": r.headers.get("Content-Type", ""),
                "body": body[:6000]}

    def _one_mac(self, ise):
        try:
            d = ise._ers_get("/endpoint", {"size": 1, "page": 1})
            res = (d.get("SearchResult", {}) or {}).get("resources", [])
            return res[0]["name"] if res else None
        except Exception:
            return None

    # ---- FMC ----------------------------------------------------------------
    def _probe_fmc(self):
        from dashboard import services
        try:
            fmc = services.get_fmc_client()
        except Exception as exc:
            self.stdout.write(self.style.ERROR(f"  FMC client init failed: {exc}"))
            self._write("fmc.client", {"ok": False, "error": str(exc)})
            return
        auth = self._run("fmc.auth", lambda: self._fmc_login(fmc))
        if not auth["ok"]:
            return
        d = fmc.domain_uuid
        cfg = f"/api/fmc_config/v1/domain/{d}"
        plat = f"/api/fmc_platform/v1/domain/{d}"
        probes = {
            "fmc.devices": (f"{cfg}/devices/devicerecords", {"limit": 2, "expanded": "true"}),
            "fmc.access_policies": (f"{cfg}/policy/accesspolicies", {"limit": 2, "expanded": "true"}),
            "fmc.intrusion_policies": (f"{cfg}/policy/intrusionpolicies", {"limit": 2}),
            "fmc.file_policies": (f"{cfg}/policy/filepolicies", {"limit": 2}),
            "fmc.security_zones": (f"{cfg}/object/securityzones", {"limit": 2}),
            "fmc.network_objects": (f"{cfg}/object/networkaddresses", {"limit": 2}),
            "fmc.audit_records": (f"{plat}/audit/auditrecords", {"limit": 2, "expanded": "true"}),
        }
        for name, (path, params) in probes.items():
            self._run(name, lambda p=path, q=params: _sample(fmc._get(p, q)))
        self._run("fmc.access_rules", lambda: self._fmc_rules(fmc, cfg))

    @staticmethod
    def _fmc_login(fmc):
        fmc.login()
        return {"domain": fmc.domain_uuid, "domains": fmc.domains}

    @staticmethod
    def _fmc_rules(fmc, cfg):
        pols = fmc._get(f"{cfg}/policy/accesspolicies", {"limit": 1}).get("items", [])
        if not pols:
            return {"note": "no access policies"}
        pid = pols[0]["id"]
        return _sample(fmc._get(f"{cfg}/policy/accesspolicies/{pid}/accessrules",
                                {"limit": 2, "expanded": "true"}))

    # ---- eStreamer ----------------------------------------------------------
    def _probe_estreamer(self):
        from django.conf import settings
        from dashboard.estreamer import collector

        host = settings.FMC["HOST"]
        cert = os.environ.get("ESTREAMER_PKCS12", "")
        if not host:
            self.stdout.write("  → estreamer.tcp8302      ... skipped (FMC_HOST not set)")
            return
        self._run("estreamer.tcp8302",
                  lambda: collector.check_connectivity(host, 8302, cert))
