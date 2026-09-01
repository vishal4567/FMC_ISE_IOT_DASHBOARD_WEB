"""
FAST bulk IoT sync via Data Connect logical profiles.

One JOIN query discovers every device whose profiling policy is mapped to an
IoT logical profile (mac + profile + logical_profile + ip), site is resolved
from the NAD device_name (batched), and everything is written with a single
bulk UPSERT (INSERT ... ON CONFLICT) instead of per-row update_or_create.
No per-device REST, no thread pool - built for full (re)loads at scale.

    manage.py sync_iot_fast              # upsert all (insert new + refresh existing)
    manage.py sync_iot_fast --additive   # insert ONLY new MACs, keep existing rows
"""
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Fast bulk IoT sync (logical-profile discovery + bulk upsert)."

    def add_arguments(self, parser):
        parser.add_argument("--additive", action="store_true",
                            help="insert only new MACs; keep existing rows untouched")

    def handle(self, *args, **opts):
        from dashboard.models import IoTDevice
        from dashboard.services import get_dataconnect_client
        from dashboard.site_mapping import db_site_matcher
        from dashboard.tasks import _parse_site_subnets, _site_for_ip

        dc_cfg = settings.DATACONNECT
        ise_cfg = settings.ISE
        t0 = time.time()

        dc = get_dataconnect_client()
        dc.log = lambda m: (self.stdout.write(m), self.stdout.flush())

        # 1. discover — one JOIN query
        rows = dc.iot_by_logical_profiles(
            ise_cfg["IOT_LOGICAL_PROFILES"], match=dc_cfg["LOGICAL_MATCH"],
            endpoints_view=dc_cfg["ENDPOINTS_VIEW"], mac_col=dc_cfg["COL_MAC"],
            profile_col=dc_cfg["COL_PROFILE"], ip_col=dc_cfg["COL_IP"],
            lp_view=dc_cfg["LP_VIEW"], lp_name_col=dc_cfg["LP_NAME_COL"],
            lp_policy_col=dc_cfg["LP_POLICY_COL"])
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

        macs = [r["mac"] for r in rows]

        # 2. site — NAD device_name (latest), then RADIUS-location backup
        loc = {}
        if dc_cfg["LOC_HOST_COL"]:
            loc = dc.location_by_device_name(
                macs, matcher=db_site_matcher(), view=dc_cfg["LOCATION_VIEW"],
                mac_col=dc_cfg["COL_LOC_MAC"], host_col=dc_cfg["LOC_HOST_COL"],
                time_col=dc_cfg["COL_LOC_TIME"], days=dc_cfg["LOCATION_DAYS"])
        unresolved = [m for m in macs if not loc.get(m)]
        if unresolved:
            backup = dc.location_by_mac(
                unresolved, view=dc_cfg["LOCATION_VIEW"],
                mac_col=dc_cfg["COL_LOC_MAC"], loc_col=dc_cfg["COL_LOC_SITE"],
                days=dc_cfg["LOCATION_DAYS"], time_col=dc_cfg["COL_LOC_TIME"])
            for m, s in backup.items():
                if s and not loc.get(m):
                    loc[m] = s

        subnets = _parse_site_subnets(ise_cfg["SITE_SUBNETS"])
        now = timezone.now()

        # 3. build + bulk UPSERT
        objs = []
        for r in rows:
            mac = r["mac"]
            site = loc.get(mac, "")
            if not site and subnets:
                site = _site_for_ip(r.get("ip", ""), subnets)
            objs.append(IoTDevice(
                mac=mac,
                device_type=r.get("device_type") or r.get("endpoint_profile", "") or "",
                site=site,
                ip=r.get("ip") or None,
                ise_profile=r.get("endpoint_profile", ""),
                logical_profile=r.get("logical_profile", ""),
                ise_identity_group=r.get("logical_profile", ""),
                correlation="Matched",
                ise_endpoint_mac=mac,
                last_seen=now,
            ))

        if opts["additive"]:
            IoTDevice.objects.bulk_create(objs, batch_size=2000,
                                          ignore_conflicts=True)
        else:
            IoTDevice.objects.bulk_create(
                objs, batch_size=2000, update_conflicts=True,
                unique_fields=["mac"],
                update_fields=["device_type", "site", "ip", "ise_profile",
                               "logical_profile", "ise_identity_group",
                               "correlation", "last_seen"])
        self.stdout.write(self.style.SUCCESS(
            f"upserted {len(objs)} devices in {round(time.time()-t0,1)}s"))
