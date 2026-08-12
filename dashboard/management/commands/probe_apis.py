"""
API probe / diagnostic harness.

    python manage.py probe_apis --out api_responses.json

Hits every ISE and FMC API the app uses (plus an eStreamer connectivity check),
printing each result **live as it runs**, records status / timing / row-count and
a small **raw sample** of each response, and writes it all to a JSON file. Hand
that file back to tune the client field mappings to your environment.

Nothing is modified - all calls are read-only.
"""
import json
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
    help = "Probe ISE/FMC/eStreamer APIs and dump raw responses to a file."

    def add_arguments(self, parser):
        parser.add_argument("--out", default="api_responses.json")
        parser.add_argument(
            "--device-types-out", default="device_types.json",
            help="write the full ISE device-type catalogue (profiler profiles + "
                 "identity groups) here, for building an import allow-list")
        parser.add_argument(
            "--full-endpoint", action="store_true",
            help="also fetch one endpoint's complete /endpoint/{id} JSON "
                 "(all attributes) into the output file")

    def handle(self, *args, **opts):
        self._opts = opts
        self._t0 = time.perf_counter()
        self._ok = 0
        self._err = 0
        report = {"ise": {}, "fmc": {}, "estreamer": {}}

        self._section("Cisco ISE")
        self._probe_ise(report["ise"])
        self._section("Cisco FMC")
        self._probe_fmc(report["fmc"])
        self._section("eStreamer")
        self._probe_estreamer(report["estreamer"])

        with open(opts["out"], "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, default=str)

        elapsed = round(time.perf_counter() - self._t0, 1)
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Done in {elapsed}s — {self._ok} OK, {self._err} error(s). "
            f"Wrote {opts['out']}"))
        if self._err:
            self.stdout.write(self.style.WARNING(
                "Review the ERR lines above and the output file; send it back "
                "for parser tuning."))

    # ---- live-printing runner ----------------------------------------------
    def _section(self, title):
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(f"=== {title} ==="))

    def _run(self, name, fn):
        """Run one probe, printing a live line before and after."""
        # 'before' line (no newline) so a slow/hanging call is visible.
        self.stdout.write(f"  → {name:22} ... ", ending="")
        self.stdout.flush()
        start = time.perf_counter()
        try:
            data = fn()
            secs = round(time.perf_counter() - start, 2)
            self._ok += 1
            n = self._count(data)
            self.stdout.write(self.style.SUCCESS(f"OK   {secs:>6}s  {n}"))
            return {"ok": True, "seconds": secs, "sample": data}
        except Exception as exc:
            secs = round(time.perf_counter() - start, 2)
            self._err += 1
            self.stdout.write(self.style.ERROR(f"ERR  {secs:>6}s  {str(exc)[:80]}"))
            return {"ok": False, "seconds": secs, "error": str(exc)}

    @staticmethod
    def _count(data):
        if isinstance(data, dict):
            sr = data.get("SearchResult") or {}
            if "total" in sr:
                return f"total={sr['total']}"
            if "_total_in_container" in data:
                return f"items={data['_total_in_container']}"
            if "paging" in data:
                return f"count={data.get('paging', {}).get('count', '?')}"
        if isinstance(data, list):
            return f"items={len(data)}"
        return ""

    # ---- ISE ----------------------------------------------------------------
    def _probe_ise(self, out):
        from dashboard import services
        try:
            ise = services.get_ise_client()
        except Exception as exc:
            self.stdout.write(self.style.ERROR(f"  ISE client init failed: {exc}"))
            out["_client"] = {"ok": False, "error": str(exc)}
            return
        probes = {
            "endpoints": ("/endpoint", {"size": 2, "page": 1}),
            "endpoint_groups": ("/endpointgroup", {"size": 2, "page": 1}),
            "network_devices": ("/networkdevice", {"size": 2, "page": 1}),
            "profiler_profiles": ("/profilerprofile", {"size": 2, "page": 1}),
        }
        for name, (path, params) in probes.items():
            out[name] = self._run(f"ise.{name}",
                                   lambda p=path, q=params: _sample(ise._ers_get(p, q)))
        out["mnt_sessions"] = self._run("ise.mnt_sessions",
                                        lambda: ise.get_active_sessions()[:2])

        # --- full device-type catalogue (for building an import allow-list) ---
        res = self._run("ise.device_types", lambda: self._device_types(ise))
        out["device_types"] = res
        if res.get("ok"):
            import json as _json
            path = self._opts["device_types_out"]
            with open(path, "w", encoding="utf-8") as f:
                _json.dump(res["sample"], f, indent=2)
            self.stdout.write(self.style.SUCCESS(f"     ↳ wrote {path}"))

        # --- location: NDG hierarchy + a NAD's full JSON (Location group) ---
        out["network_device_groups"] = self._run(
            "ise.network_device_groups",
            lambda: _sample(ise._ers_get("/networkdevicegroup", {"size": 100, "page": 1}), n=100))
        out["network_device_full"] = self._run(
            "ise.network_device_full", lambda: self._full_network_device(ise))

        # --- one full endpoint JSON (all attributes) ---
        if self._opts.get("full_endpoint"):
            out["endpoint_full"] = self._run("ise.endpoint_full",
                                             lambda: self._full_endpoint(ise))

    def _device_types(self, ise):
        """Every ISE device type: profiler profiles + endpoint identity groups
        (names, sorted). This is the source list for an import allow-list."""
        profiles = sorted(p["name"] for p in ise.get_profiler_profiles() if p.get("name"))
        groups = sorted(g["name"] for g in ise.get_endpoint_groups() if g.get("name"))
        return {
            "identity_groups": groups,
            "profiler_profiles": profiles,
            "counts": {"identity_groups": len(groups), "profiler_profiles": len(profiles)},
        }

    def _full_endpoint(self, ise):
        """Complete /endpoint/{id} JSON for the first endpoint (all attributes)."""
        lst = ise._ers_get("/endpoint", {"size": 1, "page": 1})
        res = (lst.get("SearchResult", {}) or {}).get("resources", [])
        if not res:
            return {"note": "no endpoints"}
        return ise._ers_get(f"/endpoint/{res[0]['id']}")   # untrimmed, full detail

    def _full_network_device(self, ise):
        """Full /networkdevice/{id} JSON for the first NAD - its
        NetworkDeviceGroupList carries the device's Location (and Device Type)
        network-device-group values, e.g. 'Location#All Locations#Pune#Bldg-A'."""
        lst = ise._ers_get("/networkdevice", {"size": 1, "page": 1})
        res = (lst.get("SearchResult", {}) or {}).get("resources", [])
        if not res:
            return {"note": "no network devices"}
        return ise._ers_get(f"/networkdevice/{res[0]['id']}")

    # ---- FMC ----------------------------------------------------------------
    def _probe_fmc(self, out):
        from dashboard import services
        try:
            fmc = services.get_fmc_client()
            self.stdout.write("  → fmc.auth              ... ", ending="")
            self.stdout.flush()
            t = time.perf_counter()
            fmc.login()
            self.stdout.write(self.style.SUCCESS(
                f"OK   {round(time.perf_counter()-t,2):>6}s  domain={fmc.domain_uuid}"))
            self._ok += 1
            out["_auth"] = {"ok": True, "domain": fmc.domain_uuid, "domains": fmc.domains}
        except Exception as exc:
            self.stdout.write(self.style.ERROR(f"ERR  {str(exc)[:80]}"))
            self._err += 1
            out["_auth"] = {"ok": False, "error": str(exc)}
            return
        d = fmc.domain_uuid
        cfg = f"/api/fmc_config/v1/domain/{d}"
        plat = f"/api/fmc_platform/v1/domain/{d}"
        probes = {
            "devices": (f"{cfg}/devices/devicerecords", {"limit": 2, "expanded": "true"}),
            "access_policies": (f"{cfg}/policy/accesspolicies", {"limit": 2, "expanded": "true"}),
            "intrusion_policies": (f"{cfg}/policy/intrusionpolicies", {"limit": 2}),
            "file_policies": (f"{cfg}/policy/filepolicies", {"limit": 2}),
            "security_zones": (f"{cfg}/object/securityzones", {"limit": 2}),
            "network_objects": (f"{cfg}/object/networkaddresses", {"limit": 2}),
            "audit_records": (f"{plat}/audit/auditrecords", {"limit": 2, "expanded": "true"}),
        }
        for name, (path, params) in probes.items():
            out[name] = self._run(f"fmc.{name}",
                                  lambda p=path, q=params: _sample(fmc._get(p, q)))
        # one policy's access rules (to see rule/zone field names)
        out["access_rules"] = self._run("fmc.access_rules",
                                        lambda: self._fmc_rules(fmc, cfg))

    @staticmethod
    def _fmc_rules(fmc, cfg):
        pols = fmc._get(f"{cfg}/policy/accesspolicies", {"limit": 1}).get("items", [])
        if not pols:
            return {"note": "no access policies"}
        pid = pols[0]["id"]
        return _sample(fmc._get(
            f"{cfg}/policy/accesspolicies/{pid}/accessrules",
            {"limit": 2, "expanded": "true"}))

    # ---- eStreamer ----------------------------------------------------------
    def _probe_estreamer(self, out):
        import os
        from django.conf import settings
        from dashboard.estreamer import collector

        host = settings.FMC["HOST"]
        cert = os.environ.get("ESTREAMER_PKCS12", "")
        if not host:
            self.stdout.write("  → estreamer.tcp8302     ... skipped (FMC_HOST not set)")
            out["_note"] = "FMC_HOST not set - skipped"
            return
        out["connectivity"] = self._run(
            "estreamer.tcp8302",
            lambda: collector.check_connectivity(host, 8302, cert))
        out["_note"] = ("eStreamer events require the eNcore collector piped into "
                        "`manage.py estreamer_ingest`. This is a TCP/TLS reachability "
                        "check only.")
