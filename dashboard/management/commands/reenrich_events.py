"""
Re-map every stored SecurityEvent onto the CURRENT IoT baseline (IoTDevice).

Events are enriched with ISE identity at ingest time, so old rows keep whatever
inventory existed then. After changing the baseline (e.g. switching to the
authorization-rule sync), run this to re-stamp device_type / site / hostname /
device_ip / in_ise / mapped_ise_mac from the current IoTDevice inventory.

FAST path (default): set-based SQL, batched by id-range, with a % loader - the
whole re-map runs as a handful of UPDATEs per batch inside Postgres instead of
pulling every row into Python. Matching mirrors ingest precedence: MAC first,
then source_ip, then dest_ip, via a join to IoTDevice. Unmatched rows become
FMC-only (in_ise=false, ISE fields cleared, device_ip reverts to source_ip).

    manage.py reenrich_events                  # all events (SQL fast path)
    manage.py reenrich_events --since-days 30   # only recent rows
    manage.py reenrich_events --batch 100000    # id-range window per loop
    manage.py reenrich_events --python          # row-by-row fallback (any DB)
"""
import sys
import time

from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.db.models import Max, Min
from django.utils import timezone


class Command(BaseCommand):
    help = "Re-stamp stored SecurityEvents from the current IoTDevice baseline (fast SQL)."

    def add_arguments(self, parser):
        parser.add_argument("--since-days", type=int, default=0,
                            help="only re-map events newer than N days (0 = all)")
        parser.add_argument("--batch", type=int, default=100000,
                            help="id-range window per loop (SQL path); rows per "
                                 "bulk_update (python path)")
        parser.add_argument("--python", action="store_true",
                            help="row-by-row Python re-map (DB-agnostic fallback)")

    def handle(self, *args, **opts):
        from dashboard.models import IoTDevice, SecurityEvent

        n_dev = IoTDevice.objects.count()
        n_ip = IoTDevice.objects.exclude(ip__isnull=True).count()
        self.stdout.write(f"baseline: {n_dev:,} devices ({n_ip:,} with IP)")
        if not n_dev:
            self.stdout.write(self.style.WARNING(
                "IoTDevice inventory is EMPTY - run the baseline sync first."))
            return

        since = None
        if opts["since_days"]:
            since = timezone.now() - timezone.timedelta(days=opts["since_days"])

        if opts["python"] or connection.vendor != "postgresql":
            return self._python(SecurityEvent, since, opts["batch"])
        return self._sql(SecurityEvent, IoTDevice, since, opts["batch"])

    # ------------------------------------------------------------------ #
    def _sql(self, SecurityEvent, IoTDevice, since, batch):
        ev = SecurityEvent._meta.db_table
        dev = IoTDevice._meta.db_table
        t0 = time.time()

        # id range (respecting the optional time window)
        base = SecurityEvent.objects.all()
        if since:
            base = base.filter(ts__gte=since)
        agg = base.aggregate(lo=Min("id"), hi=Max("id"))
        lo, hi = agg["lo"], agg["hi"]
        if lo is None:
            self.stdout.write("no events to re-map")
            return
        total_ids = hi - lo + 1
        self.stdout.write(f"re-mapping ids {lo:,}..{hi:,} (SQL, batch {batch:,}) ...")

        # time filter fragment shared by every statement
        tw = " AND ts >= %s" if since else ""
        tp = [since] if since else []

        # precedence: reset -> dest_ip -> source_ip -> MAC (later overrides).
        # UPDATE ... FROM joins each event to its IoTDevice by IP / MAC.
        reset = (f"UPDATE {ev} SET in_ise=false, mapped_ise_mac='', "
                 f"device_type='', site='', device_ip=source_ip "
                 f"WHERE id>=%s AND id<%s{tw}")
        by_ip = (lambda col:
                 f"UPDATE {ev} AS e SET in_ise=true, mapped_ise_mac=d.mac, "
                 f"device_mac=CASE WHEN e.device_mac='' OR upper(e.device_mac)='NONE' "
                 f"THEN d.mac ELSE e.device_mac END, "
                 f"device_type=COALESCE(d.device_type,''), site=COALESCE(d.site,''), "
                 f"hostname=CASE WHEN COALESCE(d.hostname,'')<>'' THEN d.hostname "
                 f"ELSE e.hostname END, "
                 f"device_ip=COALESCE(d.ip, e.device_ip) "
                 f"FROM {dev} d WHERE e.id>=%s AND e.id<%s{tw} "
                 f"AND e.{col} IS NOT NULL AND e.{col}=d.ip")
        by_mac = (f"UPDATE {ev} AS e SET in_ise=true, mapped_ise_mac=d.mac, "
                  f"device_type=COALESCE(d.device_type,''), site=COALESCE(d.site,''), "
                  f"hostname=CASE WHEN COALESCE(d.hostname,'')<>'' THEN d.hostname "
                  f"ELSE e.hostname END, "
                  f"device_ip=COALESCE(d.ip, e.device_ip) "
                  f"FROM {dev} d WHERE e.id>=%s AND e.id<%s{tw} "
                  f"AND e.device_mac<>'' AND upper(e.device_mac)=d.mac")

        processed = 0
        with connection.cursor() as cur:
            b = lo
            while b <= hi:
                top = b + batch
                with transaction.atomic():
                    cur.execute(reset, [b, top, *tp])
                    processed += cur.rowcount
                    cur.execute(by_ip("dest_ip"), [b, top, *tp])
                    cur.execute(by_ip("source_ip"), [b, top, *tp])
                    cur.execute(by_mac, [b, top, *tp])
                done_ids = min(top, hi + 1) - lo
                pct = int(100 * done_ids / total_ids)
                fill = pct * 30 // 100
                rate = processed / (time.time() - t0) if time.time() > t0 else 0
                # newline every ~10% so journald (detached runs) shows progress too
                end = "\n" if pct % 10 == 0 else ""
                sys.stdout.write(
                    f"\r[{'#' * fill}{'.' * (30 - fill)}] {pct:3d}%  "
                    f"{processed:,} rows  {rate:,.0f}/s   {end}")
                sys.stdout.flush()
                b = top

        in_ise = SecurityEvent.objects.filter(in_ise=True)
        if since:
            in_ise = in_ise.filter(ts__gte=since)
        self.stdout.write(self.style.SUCCESS(
            f"\nre-mapped {processed:,} events in {round(time.time()-t0,1)}s; "
            f"{in_ise.count():,} now matched to the baseline"))

    # ------------------------------------------------------------------ #
    def _python(self, SecurityEvent, since, batch):
        from dashboard import event_store

        ise_map = event_store.ise_identity_map()
        ip_map = event_store.ise_ip_map()
        qs = SecurityEvent.objects.all()
        if since:
            qs = qs.filter(ts__gte=since)
        total = qs.count()
        if not total:
            self.stdout.write("no events to re-map")
            return
        self.stdout.write(f"re-mapping {total:,} events (python) ...")
        fields = event_store.REMAP_FIELDS
        done = changed = 0
        t0 = time.time()
        buf = []
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
                sys.stdout.write(f"\r[{'#' * fill}{'.' * (30 - fill)}] {pct:3d}%  "
                                 f"{done:,}/{total:,}   ")
                sys.stdout.flush()
        if buf:
            SecurityEvent.objects.bulk_update(buf, fields)
            changed += len(buf)
        self.stdout.write(self.style.SUCCESS(
            f"\nre-mapped {done:,} events, {changed:,} changed, "
            f"in {round(time.time()-t0,1)}s"))
