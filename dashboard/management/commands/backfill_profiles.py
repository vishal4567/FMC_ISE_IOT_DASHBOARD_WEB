"""
FAST profiler-profile backfill for existing IoTDevice rows.

radius_authentications.endpoint_profile is often blank, so device_type falls back
to the identity group. This fills device_type + ise_profile (and a missing IP)
from endpoints_data.endpoint_policy - the reliable profiler profile - via one
batched Data Connect lookup + a bulk UPDATE. Much cheaper than a full
authz-rule re-sync (no radius_authentications full-history scan).

    manage.py backfill_profiles                # only devices lacking a profile
    manage.py backfill_profiles --all          # re-check every device
    manage.py backfill_profiles --overwrite    # replace device_type even if set
"""
import sys
import time

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Backfill device_type/ise_profile (profiler profile) from endpoints_data."

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true",
                            help="process all devices, not just those missing a profile")
        parser.add_argument("--overwrite", action="store_true",
                            help="set device_type to the profiler profile even if a "
                                 "profile is already stored")

    def handle(self, *args, **opts):
        from dashboard.models import IoTDevice

        from dashboard.services import get_dataconnect_client

        dc = settings.DATACONNECT
        if not dc.get("ENDPOINTS_VIEW"):
            self.stderr.write(self.style.ERROR("ENDPOINTS_VIEW not configured."))
            return
        t0 = time.time()

        qs = IoTDevice.objects.all()
        if not opts["all"]:
            qs = qs.filter(ise_profile="")      # missing a profiler profile
        macs = list(qs.values_list("mac", flat=True))
        if not macs:
            self.stdout.write("nothing to backfill")
            return
        self.stdout.write(f"backfilling {len(macs):,} device(s) from "
                          f"{dc['ENDPOINTS_VIEW']} ...")

        client = get_dataconnect_client()
        client.log = lambda m: (self.stdout.write(m), self.stdout.flush())
        # one batched (IN 900) lookup, single TLS session
        attrs = client.endpoint_attrs_by_mac(
            macs, view=dc["ENDPOINTS_VIEW"], mac_col=dc["COL_MAC"],
            ip_col=dc["COL_IP"], profile_col=dc["COL_PROFILE"])
        self.stdout.write(f"endpoints_data returned {len(attrs):,} rows "
                          f"({round(time.time()-t0,1)}s)")
        if not attrs:
            self.stdout.write(self.style.WARNING(
                "endpoints_data had no matching rows - device_type cannot be "
                "improved beyond the identity group."))
            return

        updated = 0
        batch = []
        total = len(attrs)
        tty = sys.stdout.isatty()
        last_step = -1
        tw = time.time()

        # iterate only the devices we have attrs for
        want = list(attrs.keys())
        done = 0
        for i in range(0, len(want), 2000):
            chunk = want[i:i + 2000]
            for d in IoTDevice.objects.filter(mac__in=chunk):
                a = attrs.get(d.mac) or {}
                prof = (a.get("profile") or "").strip()
                ip = (a.get("ip") or "").strip()
                changed = False
                if prof and (opts["overwrite"] or not d.ise_profile):
                    d.ise_profile = prof
                    d.device_type = prof          # the point: real profiler profile
                    changed = True
                if ip and not d.ip:
                    d.ip = ip
                    changed = True
                if changed:
                    batch.append(d)
                done += 1
            if batch:
                IoTDevice.objects.bulk_update(
                    batch, ["device_type", "ise_profile", "ip"])
                updated += len(batch)
                batch = []
            pct = int(100 * done / total)
            rate = done / max(time.time() - tw, 1e-6)
            if tty:
                fill = pct * 30 // 100
                sys.stdout.write(f"\r[{'#' * fill}{'.' * (30 - fill)}] {pct:3d}%  "
                                 f"{done:,}/{total:,}  {rate:,.0f}/s   ")
                sys.stdout.flush()
            elif (pct // 10) != last_step:
                last_step = pct // 10
                self.stdout.write(f"[backfill] {pct:3d}%  {done:,}/{total:,}  "
                                  f"{updated:,} updated")

        self.stdout.write(self.style.SUCCESS(
            f"\ndone: updated {updated:,} device(s) in {round(time.time()-t0,1)}s"))
