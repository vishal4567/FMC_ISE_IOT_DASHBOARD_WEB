"""
API probe / diagnostic harness.

    python manage.py probe_apis --out api_responses.json

Hits every ISE and FMC API the app uses (plus an eStreamer connectivity check),
records status / timing / row-count and a small **raw sample** of each response,
and writes it all to a JSON file. Hand that file back to tune the client field
mappings (ISE endpoint attrs, FMC event/config fields, eStreamer mapping) to
your environment.

Nothing is modified - all calls are read-only.
"""
import json
import time

from django.core.management.base import BaseCommand

from dashboard import services
from dashboard.estreamer import collector


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

    def handle(self, *args, **opts):
        report = {"ise": {}, "fmc": {}, "estreamer": {}}

        self._probe_ise(report["ise"])
        self._probe_fmc(report["fmc"])
        self._probe_estreamer(report["estreamer"])

        with open(opts["out"], "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, default=str)
        self.stdout.write(self.style.SUCCESS(f"\nWrote {opts['out']}"))
        self._summary(report)

    # ---- ISE ----------------------------------------------------------------
    def _probe_ise(self, out):
        try:
            ise = services.get_ise_client()
        except Exception as exc:
            out["_client"] = {"ok": False, "error": str(exc)}
            return
        probes = {
            "endpoints": ("/endpoint", {"size": 2, "page": 1}),
            "endpoint_groups": ("/endpointgroup", {"size": 2, "page": 1}),
            "network_devices": ("/networkdevice", {"size": 2, "page": 1}),
            "profiler_profiles": ("/profilerprofile", {"size": 2, "page": 1}),
        }
        for name, (path, params) in probes.items():
            out[name] = self._timed(lambda: _sample(ise._ers_get(path, params)))
        # MnT sessions (XML) - capture raw text head
        out["mnt_sessions"] = self._timed(
            lambda: ise.get_active_sessions()[:2], is_list=True
        )

    # ---- FMC ----------------------------------------------------------------
    def _probe_fmc(self, out):
        try:
            fmc = services.get_fmc_client()
            fmc.login()
            out["_auth"] = {"ok": True, "domain": fmc.domain_uuid, "domains": fmc.domains}
        except Exception as exc:
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
            out[name] = self._timed(lambda p=path, q=params: _sample(fmc._get(p, q)))
        # one policy's access rules (to see rule/zone field names)
        try:
            pols = fmc._get(f"{cfg}/policy/accesspolicies", {"limit": 1}).get("items", [])
            if pols:
                pid = pols[0]["id"]
                out["access_rules"] = self._timed(
                    lambda: _sample(fmc._get(
                        f"{cfg}/policy/accesspolicies/{pid}/accessrules",
                        {"limit": 2, "expanded": "true"}))
                )
        except Exception as exc:
            out["access_rules"] = {"ok": False, "error": str(exc)}

    # ---- eStreamer ----------------------------------------------------------
    def _probe_estreamer(self, out):
        import os
        from django.conf import settings

        host = settings.FMC["HOST"]
        cert = os.environ.get("ESTREAMER_PKCS12", "")
        if not host:
            out["_note"] = "FMC_HOST not set - skipped"
            return
        out["connectivity"] = self._timed(
            lambda: collector.check_connectivity(host, 8302, cert), is_list=False
        )
        out["_note"] = ("eStreamer events require the eNcore collector piped into "
                        "`manage.py estreamer_ingest`. This is a TCP/TLS reachability "
                        "check only.")

    # ---- helpers ------------------------------------------------------------
    def _timed(self, fn, is_list=False):
        start = time.perf_counter()
        try:
            data = fn()
            return {"ok": True, "seconds": round(time.perf_counter() - start, 2),
                    "sample": data}
        except Exception as exc:
            return {"ok": False, "seconds": round(time.perf_counter() - start, 2),
                    "error": str(exc)}

    def _summary(self, report):
        self.stdout.write("\n=== probe summary ===")
        for src, section in report.items():
            for name, res in section.items():
                if not isinstance(res, dict):
                    continue
                status = "OK " if res.get("ok") else "ERR"
                secs = res.get("seconds", "")
                extra = res.get("error", "")[:60]
                self.stdout.write(f"  [{status}] {src}.{name:20} {secs}s {extra}")
