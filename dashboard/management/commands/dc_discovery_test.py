"""
DRY RUN the new Data Connect paths WITHOUT writing to the DB, so you can verify
them (and the configured column names) before a full sync:

  1. IoT discovery by AUTHORIZATION profile (ISE_DC_IOT_BY_AUTHZ)
  2. LOCATION from the NAD hostname       (ISE_DC_LOCATION_BY_NAD_HOSTNAME)

Prints counts, a sample, and the resolved-site distribution. Nothing is stored.

    manage.py dc_discovery_test
    manage.py dc_discovery_test --limit 300 --samples 8
"""
from collections import Counter

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Dry-run IoT-by-authz discovery + NAD-hostname location (no DB writes)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=0,
                            help="cap discovered MACs before the location step (0=all)")
        parser.add_argument("--samples", type=int, default=10,
                            help="sample rows to print (default 10)")

    def handle(self, *args, **opts):
        from dashboard.services import get_dataconnect_client
        from dashboard.site_mapping import db_site_matcher

        dc = settings.DATACONNECT
        n = opts["samples"]
        client = get_dataconnect_client()
        client.log = lambda m: (self.stdout.write(m), self.stdout.flush())

        # ---- 1. discovery ----
        self.stdout.write(self.style.MIGRATE_HEADING("== 1. IoT discovery =="))
        try:
            if dc.get("IOT_BY_LOGICAL"):
                lps = settings.ISE["IOT_LOGICAL_PROFILES"]
                self.stdout.write(f"  logical profiles: {lps}")
                rows = client.iot_by_logical_profiles(
                    lps, endpoints_view=dc["ENDPOINTS_VIEW"], mac_col=dc["COL_MAC"],
                    profile_col=dc["COL_PROFILE"], ip_col=dc["COL_IP"],
                    lp_view=dc["LP_VIEW"], lp_name_col=dc["LP_NAME_COL"],
                    lp_policy_col=dc["LP_POLICY_COL"], limit=opts["limit"])
            else:
                rows = client.iot_by_authz(
                    view=dc["LOCATION_VIEW"], mac_col=dc["COL_LOC_MAC"],
                    authz_col=dc["COL_AUTHZ"], match=dc["AUTHZ_MATCH"],
                    col_profile=dc["AUTHZ_COL_PROFILE"],
                    col_devicetype=dc["AUTHZ_COL_DEVICETYPE"],
                    col_ip=dc["AUTHZ_COL_IP"], col_site=dc["COL_LOC_SITE"],
                    limit=opts["limit"])
        except Exception as exc:
            self.stdout.write(self.style.ERROR(f"  discovery FAILED: {exc}"))
            self.stdout.write("  -> if logical: check ISE_IOT_LOGICAL_PROFILES / "
                              "ISE_DC_LP_*; if authz: ISE_DC_COL_AUTHZ / "
                              "ISE_DC_AUTHZ_COL_* (blank a missing column).")
            return
        self.stdout.write(self.style.SUCCESS(f"  discovered {len(rows)} unique MACs"))
        if not rows:
            self.stdout.write("  (nothing matched — check ISE_DC_AUTHZ_MATCH token)")
            return
        self.stdout.write(f"  {'MAC':18} {'PROFILE':18} {'DEV_TYPE':16} "
                          f"{'IP':16} AUTHZ")
        for r in rows[:n]:
            self.stdout.write(
                f"  {r['mac']:18} {str(r['endpoint_profile'])[:18]:18} "
                f"{str(r['device_type'])[:16]:16} {str(r['ip'])[:16]:16} "
                f"{r.get('authz_profile', '')}")

        # ---- 1b. IP + device_type backfill from endpoints_data ----
        need = [r["mac"] for r in rows
                if not r.get("ip") or not r.get("device_type")]
        if need and dc["ENDPOINTS_VIEW"]:
            self.stdout.write("")
            self.stdout.write(self.style.MIGRATE_HEADING(
                "== 1b. IP + device_type backfill from endpoints view =="))
            try:
                attrs = client.endpoint_attrs_by_mac(
                    need, view=dc["ENDPOINTS_VIEW"], mac_col=dc["COL_MAC"],
                    ip_col=dc["COL_IP"], profile_col=dc["COL_PROFILE"])
                for r in rows:
                    a = attrs.get(r["mac"])
                    if not a:
                        continue
                    if not r.get("ip"):
                        r["ip"] = a["ip"]
                    if not r.get("device_type"):
                        r["device_type"] = a["profile"]
                have_ip = sum(1 for r in rows if r.get("ip"))
                have_dt = sum(1 for r in rows if r.get("device_type"))
                self.stdout.write(self.style.SUCCESS(
                    f"  ip {have_ip}/{len(rows)}, device_type {have_dt}/{len(rows)} "
                    f"after backfill (from {dc['ENDPOINTS_VIEW']})"))
            except Exception as exc:
                self.stdout.write(self.style.ERROR(f"  backfill FAILED: {exc}"))
                self.stdout.write("  -> fix ISE_DC_ENDPOINTS_VIEW / ISE_DC_COL_MAC / "
                                  "ISE_DC_COL_IP / ISE_DC_COL_PROFILE.")

        # ---- 2. location by NAD hostname ----
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(
            "== 2. Location by NAD hostname =="))
        macs = [r["mac"] for r in rows]
        try:
            if dc.get("LOC_HOST_COL"):
                loc = client.location_by_device_name(
                    macs, matcher=db_site_matcher(), view=dc["LOCATION_VIEW"],
                    mac_col=dc["COL_LOC_MAC"], host_col=dc["LOC_HOST_COL"],
                    time_col=dc["COL_LOC_TIME"], days=dc["LOCATION_DAYS"])
            else:
                loc = client.location_by_nad_hostname(
                    macs, matcher=db_site_matcher(), nd_view=dc["ND_VIEW"],
                    nd_name_col=dc["ND_NAME_COL"], nd_ip_col=dc["ND_IP_COL"],
                    radius_view=dc["LOCATION_VIEW"], mac_col=dc["COL_LOC_MAC"],
                    nas_col=dc["COL_NAS_IP"])
        except Exception as exc:
            self.stdout.write(self.style.ERROR(
                f"  location_by_nad_hostname FAILED: {exc}"))
            self.stdout.write("  -> fix ISE_DC_ND_VIEW / ISE_DC_ND_NAME_COL / "
                              "ISE_DC_ND_IP_COL / ISE_DC_COL_NAS_IP.")
            return
        resolved = {m: s for m, s in loc.items() if s}
        self.stdout.write(self.style.SUCCESS(
            f"  {len(resolved)}/{len(macs)} MACs resolved to a site"))
        for site, cnt in Counter(resolved.values()).most_common():
            self.stdout.write(f"    {site:26} {cnt:>7}")
        unresolved = [m for m in macs if not loc.get(m)][:n]
        if unresolved:
            self.stdout.write(f"  sample UNRESOLVED: {', '.join(unresolved)}")

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            "Dry run OK - NOTHING written. If both look right: manage.py sync_ise"))
