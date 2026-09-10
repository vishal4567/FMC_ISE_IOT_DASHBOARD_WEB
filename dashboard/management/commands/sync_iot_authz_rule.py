"""
IoT sync driven by the RADIUS AUTHORIZATION RULE.

Discovers every device whose authorization_rule contains 'IOT' (any case) from
the full radius_authentications log, taking the LATEST auth row per MAC. Maps:
    device_type            <- endpoint_profile       (the profiler device class)
    authorization_profile  <- authorization_rule     (e.g. IOT-CCTV, IOT-Quarantine-Access)
    ise_identity_group     <- identity_group         (e.g. Wipro_CCTV)
    ip                     <- framed_ip_address
    site                   <- device_name -> site-code map (backup: location leaf)
A device whose authorization_profile contains 'Quarantine' is a quarantined
device (filter on that in the dashboard).

    manage.py sync_iot_authz_rule                 # upsert all (full history)
    manage.py sync_iot_authz_rule --days 90        # bound the scan for speed
    manage.py sync_iot_authz_rule --additive       # insert only new MACs
    manage.py sync_iot_authz_rule --match IOT --limit 500
"""
import sys
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Sync IoT devices discovered by RADIUS authorization_rule (~IOT)."

    def add_arguments(self, parser):
        parser.add_argument("--match", default=None,
                            help="token the rule must contain (default settings "
                                 "AUTHZ_RULE_MATCH = IOT)")
        parser.add_argument("--days", type=int, default=0,
                            help="only scan the last N days (0 = all history)")
        parser.add_argument("--additive", action="store_true",
                            help="insert only new MACs; keep existing rows")
        parser.add_argument("--limit", type=int, default=0,
                            help="cap discovered MACs (0 = all)")

    def handle(self, *args, **opts):
        from dashboard.models import IoTDevice
        from dashboard.services import get_dataconnect_client
        from dashboard.site_mapping import db_site_matcher
        from integrations.location_map import site_from_hostname

        dc = settings.DATACONNECT
        match = opts["match"] or dc.get("AUTHZ_RULE_MATCH", "IOT")
        t0 = time.time()

        client = get_dataconnect_client()
        client.log = lambda m: (self.stdout.write(m), self.stdout.flush())

        # 1. discover — latest auth row per MAC where the rule ~ IOT
        rows = client.iot_by_authz_rule(
            match=match,
            view=dc.get("AUTHENTICATIONS_VIEW", "radius_authentications"),
            mac_col=dc["COL_LOC_MAC"],
            rule_col=dc.get("COL_AUTHZ_RULE", "authorization_rule"),
            profile_col=dc.get("AUTHZ_COL_PROFILE", "endpoint_profile"),
            group_col=dc.get("COL_IDENTITY_GROUP", "identity_group"),
            ip_col=dc.get("COL_FRAMED_IP", "framed_ip_address"),
            host_col=dc["LOC_HOST_COL"] or "device_name",
            loc_col=dc["COL_LOC_SITE"], time_col=dc["COL_LOC_TIME"],
            days=opts["days"], limit=opts["limit"])
        self.stdout.write(f"discovered {len(rows)} devices "
                          f"({round(time.time()-t0,1)}s)")
        if not rows:
            self.stdout.write(self.style.WARNING("nothing discovered"))
            return

        if opts["additive"]:
            existing = set(IoTDevice.objects.values_list("mac", flat=True))
            rows = [r for r in rows if r["mac"] not in existing]
            self.stdout.write(f"additive: {len(rows)} new (kept {len(existing)})")
            if not rows:
                self.stdout.write(self.style.SUCCESS("no new devices"))
                return

        # 2. site — NAD hostname (device_name) via the site-code map; else the
        #    RADIUS location leaf already returned on each row.
        matcher = db_site_matcher()
        now = timezone.now()

        objs = []
        quarantined = 0
        for r in rows:
            site = site_from_hostname(r.get("host", ""), matcher) \
                or r.get("location", "") or ""
            rule = r.get("authz_rule", "") or ""
            profile = r.get("endpoint_profile", "") or ""
            if "QUARANTINE" in rule.upper():
                quarantined += 1
            objs.append(IoTDevice(
                mac=r["mac"],
                device_type=profile,                            # endpoint_profile
                site=site,
                ip=r.get("ip") or None,
                ise_profile=profile,
                ise_identity_group=r.get("identity_group", "") or "",
                authorization_profile=rule,
                correlation="Matched",
                ise_endpoint_mac=r["mac"],
                last_seen=now,
            ))

        # 3. bulk UPSERT with a progress bar
        total = len(objs)
        chunk = 2000
        written = 0
        tw = time.time()
        for i in range(0, total, chunk):
            part = objs[i:i + chunk]
            if opts["additive"]:
                IoTDevice.objects.bulk_create(part, ignore_conflicts=True)
            else:
                IoTDevice.objects.bulk_create(
                    part, update_conflicts=True, unique_fields=["mac"],
                    update_fields=["device_type", "site", "ip", "ise_profile",
                                   "ise_identity_group", "authorization_profile",
                                   "correlation", "last_seen"])
            written += len(part)
            pct = int(100 * written / total)
            fill = pct * 30 // 100
            rate = written / (time.time() - tw) if time.time() > tw else 0
            sys.stdout.write(
                f"\rwriting [{'#' * fill}{'.' * (30 - fill)}] {pct:3d}%  "
                f"{written:,}/{total:,}  {rate:,.0f}/s   ")
            sys.stdout.flush()

        self.stdout.write(self.style.SUCCESS(
            f"\n{'added' if opts['additive'] else 'upserted'} {total:,} devices "
            f"({quarantined:,} quarantined) in {round(time.time()-t0,1)}s"))
