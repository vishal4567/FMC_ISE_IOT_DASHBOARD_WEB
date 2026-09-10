"""
Re-map every stored SecurityEvent onto the CURRENT IoT baseline (IoTDevice).

Events are enriched with ISE identity at ingest time, so old rows keep whatever
inventory existed then. After changing the baseline (e.g. switching to the
authorization-rule sync), run this to re-stamp device_type / site / hostname /
device_ip / in_ise / mapped_ise_mac from the current IoTDevice inventory.

Matching mirrors ingest: MAC first, then the raw flow IPs (source_ip, dest_ip)
via the IP->device bridge. Unmatched rows become FMC-only (in_ise=False, ISE
fields cleared, device_ip reverts to the flow source_ip).

    manage.py reenrich_events                 # all events
    manage.py reenrich_events --since-days 30  # only recent rows
    manage.py reenrich_events --batch 5000
"""
import sys
import time

from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Re-stamp stored SecurityEvents from the current IoTDevice baseline."

    def add_arguments(self, parser):
        parser.add_argument("--since-days", type=int, default=0,
                            help="only re-map events newer than N days (0 = all)")
        parser.add_argument("--batch", type=int, default=5000,
                            help="rows per bulk_update (default 5000)")

    def handle(self, *args, **opts):
        from dashboard import event_store
        from dashboard.models import SecurityEvent

        ise_map = event_store.ise_identity_map()
        ip_map = event_store.ise_ip_map()
        self.stdout.write(f"baseline: {len(ise_map):,} MACs, {len(ip_map):,} IPs")
        if not ise_map and not ip_map:
            self.stdout.write(self.style.WARNING(
                "IoTDevice inventory is EMPTY - run the baseline sync first."))
            return

        qs = SecurityEvent.objects.all()
        if opts["since_days"]:
            since = timezone.now() - timezone.timedelta(days=opts["since_days"])
            qs = qs.filter(ts__gte=since)
        total = qs.count()
        if not total:
            self.stdout.write("no events to re-map")
            return
        self.stdout.write(f"re-mapping {total:,} events ...")

        fields = event_store.REMAP_FIELDS
        batch = opts["batch"]
        done = changed = 0
        t0 = time.time()
        buf = []

        # .iterator() streams rows without loading all into memory
        for ev in qs.only("id", *fields, "source_ip", "dest_ip").iterator(
                chunk_size=batch):
            if event_store.remap_row(ev, ise_map, ip_map):
                buf.append(ev)
            done += 1

            if len(buf) >= batch:
                SecurityEvent.objects.bulk_update(buf, fields)
                changed += len(buf)
                buf = []
            if done % batch == 0 or done == total:
                pct = int(100 * done / total)
                fill = pct * 30 // 100
                rate = done / (time.time() - t0) if time.time() > t0 else 0
                sys.stdout.write(
                    f"\r[{'#' * fill}{'.' * (30 - fill)}] {pct:3d}%  "
                    f"{done:,}/{total:,}  {rate:,.0f}/s   ")
                sys.stdout.flush()

        if buf:
            SecurityEvent.objects.bulk_update(buf, fields)
            changed += len(buf)

        self.stdout.write(self.style.SUCCESS(
            f"\nre-mapped {done:,} events, {changed:,} rows changed, "
            f"in {round(time.time()-t0,1)}s"))
