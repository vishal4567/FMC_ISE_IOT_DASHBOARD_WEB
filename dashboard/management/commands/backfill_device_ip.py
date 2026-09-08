"""
Batched back-fill of SecurityEvent.device_ip = the device's ISE endpoint IP
(IoTDevice.ip), with a live % progress bar. Walks the table by id range and
commits each batch, so it never locks the whole table and can be re-run to
resume.

    manage.py backfill_device_ip
    manage.py backfill_device_ip --batch 50000
"""
import sys
import time

from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Batched device_ip back-fill from IoTDevice.ip, with a % loader."

    def add_arguments(self, parser):
        parser.add_argument("--batch", type=int, default=50000,
                            help="rows (by id range) per batch (default 50000)")

    def handle(self, *args, **opts):
        batch = max(1000, opts["batch"])
        with connection.cursor() as cur:
            cur.execute("SELECT min(id), max(id) FROM dashboard_securityevent")
            lo, hi = cur.fetchone()
        if lo is None:
            self.stdout.write("no events to update")
            return

        span = hi - lo + 1
        self.stdout.write(f"scanning ids {lo:,}..{hi:,} ({span:,}) in batches "
                          f"of {batch:,}")
        updated, scanned, cur_id, t0 = 0, 0, lo, time.time()

        while cur_id <= hi:
            top = min(cur_id + batch - 1, hi)
            with connection.cursor() as cur:
                cur.execute(
                    "UPDATE dashboard_securityevent e SET device_ip = d.ip "
                    "FROM dashboard_iotdevice d "
                    "WHERE e.id BETWEEN %s AND %s "
                    "  AND UPPER(e.device_mac) = UPPER(d.mac) "
                    "  AND d.ip IS NOT NULL "
                    "  AND e.device_ip IS DISTINCT FROM d.ip",
                    [cur_id, top])
                updated += cur.rowcount
            scanned = top - lo + 1
            pct = min(100, int(100 * scanned / span))
            fill = pct * 30 // 100
            bar = "#" * fill + "." * (30 - fill)
            rate = scanned / (time.time() - t0) if time.time() > t0 else 0
            sys.stdout.write(
                f"\r[{bar}] {pct:3d}%  scanned {scanned:,}/{span:,}  "
                f"updated {updated:,}  {rate:,.0f} id/s   ")
            sys.stdout.flush()
            cur_id = top + 1

        self.stdout.write(self.style.SUCCESS(
            f"\ndone: updated {updated:,} rows in "
            f"{round(time.time()-t0,1)}s"))
