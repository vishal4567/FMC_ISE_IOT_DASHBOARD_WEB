"""
Delete FMC SecurityEvents older than N days (default 7), batched with a % loader
so a large delete never runs as one table-locking statement. Hourly rollups /
aggregates are untouched.

    manage.py purge_events                 # delete everything older than 7 days
    manage.py purge_events --days 3
    manage.py purge_events --days 7 --threats-days 30   # keep threats longer
    manage.py purge_events --batch 50000
"""
import sys
import time

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone


class Command(BaseCommand):
    help = "Delete FMC events older than N days (batched, with a loader)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=7,
                            help="delete events older than this many days (default 7)")
        parser.add_argument("--threats-days", type=int, default=0,
                            help="keep threat (non-Connection) events this many days "
                                 "instead of --days (0 = same cutoff for all)")
        parser.add_argument("--batch", type=int, default=50000,
                            help="rows deleted per statement (default 50000)")

    def handle(self, *args, **opts):
        from dashboard.models import SecurityEvent

        now = timezone.now()
        conn_cut = now - timezone.timedelta(days=opts["days"])
        threat_cut = (now - timezone.timedelta(days=opts["threats_days"])
                      if opts["threats_days"] else conn_cut)

        # a row is expired if: (Connection AND ts<conn_cut) OR (threat AND ts<threat_cut)
        expired = (Q(event_type="Connection", ts__lt=conn_cut)
                   | (~Q(event_type="Connection") & Q(ts__lt=threat_cut)))
        qs = SecurityEvent.objects.filter(expired)
        total = qs.count()
        if not total:
            self.stdout.write("nothing to purge")
            return
        self.stdout.write(
            f"purging {total:,} events older than {opts['days']}d"
            + (f" (threats {opts['threats_days']}d)" if opts["threats_days"] else "")
            + f" [< {conn_cut:%Y-%m-%d %H:%M}] ...")

        batch = opts["batch"]
        deleted = 0
        t0 = time.time()
        while True:
            ids = list(SecurityEvent.objects.filter(expired)
                       .values_list("id", flat=True)[:batch])
            if not ids:
                break
            n = SecurityEvent.objects.filter(id__in=ids).delete()[0]
            deleted += n
            pct = int(100 * deleted / total)
            fill = pct * 30 // 100
            rate = deleted / (time.time() - t0) if time.time() > t0 else 0
            end = "\n" if pct % 10 == 0 else ""
            sys.stdout.write(
                f"\r[{'#' * fill}{'.' * (30 - fill)}] {min(pct,100):3d}%  "
                f"{deleted:,}/{total:,}  {rate:,.0f}/s   {end}")
            sys.stdout.flush()
            if n == 0:
                break

        self.stdout.write(self.style.SUCCESS(
            f"\npurged {deleted:,} events in {round(time.time()-t0,1)}s"))
