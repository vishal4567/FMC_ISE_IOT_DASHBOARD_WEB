"""
Delete FMC SecurityEvents older than N days (default 7) from a VERY large table
(tens of millions of rows) efficiently:

  * raw set-based DELETE using a ctid sub-select (no Python id list, no big IN
    param) - Postgres deletes a physical block of matching rows per statement;
  * large batches (default 200k), each its own committed statement so locks
    release between batches and autovacuum can reclaim space as it goes;
  * a % loader that also prints periodic lines (visible under run_task.sh).

    manage.py purge_events                 # older than 7 days
    manage.py purge_events --days 3
    manage.py purge_events --days 7 --threats-days 30   # keep threats longer
    manage.py purge_events --batch 500000 --sleep 0.2   # bigger + throttled

Hourly rollups / aggregates are untouched. After a big purge, autovacuum
reclaims space; run VACUUM (ANALYZE) manually if you need it immediately.
"""
import sys
import time

from django.core.management.base import BaseCommand
from django.db import connection
from django.utils import timezone


class Command(BaseCommand):
    help = "Delete FMC events older than N days (raw ctid-batched, big-table safe)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=7,
                            help="delete events older than this many days (default 7)")
        parser.add_argument("--threats-days", type=int, default=0,
                            help="keep threat (non-Connection) events this many days "
                                 "instead of --days (0 = same cutoff for all)")
        parser.add_argument("--batch", type=int, default=200000,
                            help="rows deleted per statement (default 200000)")
        parser.add_argument("--sleep", type=float, default=0.0,
                            help="seconds to pause between batches (throttle I/O)")

    def handle(self, *args, **opts):
        from dashboard.models import SecurityEvent

        if connection.vendor != "postgresql":
            self.stderr.write(self.style.ERROR(
                "purge_events needs PostgreSQL (ctid). Use the ORM path otherwise."))
            return

        tbl = SecurityEvent._meta.db_table
        now = timezone.now()
        conn_cut = now - timezone.timedelta(days=opts["days"])
        threat_cut = (now - timezone.timedelta(days=opts["threats_days"])
                      if opts["threats_days"] else conn_cut)

        # WHERE for "expired". Same cutoff -> a single ts predicate (best index
        # use). Different threat window -> per-type predicate.
        if opts["threats_days"]:
            where = ("((event_type = 'Connection' AND ts < %(c)s) OR "
                     "(event_type <> 'Connection' AND ts < %(t)s))")
            params = {"c": conn_cut, "t": threat_cut}
        else:
            where = "ts < %(c)s"
            params = {"c": conn_cut}

        # estimate remaining (indexed count on ts); used only for the % loader
        with connection.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {tbl} WHERE {where}", params)
            total = cur.fetchone()[0]
        if not total:
            self.stdout.write("nothing to purge")
            return
        self.stdout.write(
            f"purging {total:,} events older than {opts['days']}d"
            + (f" (threats {opts['threats_days']}d)" if opts["threats_days"] else "")
            + f" [< {conn_cut:%Y-%m-%d %H:%M}], batch {opts['batch']:,} ...")

        sql = (f"DELETE FROM {tbl} WHERE ctid IN "
               f"(SELECT ctid FROM {tbl} WHERE {where} LIMIT {int(opts['batch'])})")

        deleted = 0
        t0 = time.time()
        # autocommit so each batch commits on its own (locks release between)
        old_autocommit = connection.get_autocommit()
        connection.set_autocommit(True)
        try:
            while True:
                with connection.cursor() as cur:
                    cur.execute(sql, params)
                    n = cur.rowcount
                if not n:
                    break
                deleted += n
                pct = min(100, int(100 * deleted / total))
                fill = pct * 30 // 100
                rate = deleted / (time.time() - t0) if time.time() > t0 else 0
                end = "\n" if pct % 10 == 0 else ""
                sys.stdout.write(
                    f"\r[{'#' * fill}{'.' * (30 - fill)}] {pct:3d}%  "
                    f"{deleted:,}/{total:,}  {rate:,.0f}/s   {end}")
                sys.stdout.flush()
                if opts["sleep"]:
                    time.sleep(opts["sleep"])
        finally:
            connection.set_autocommit(old_autocommit)

        self.stdout.write(self.style.SUCCESS(
            f"\npurged {deleted:,} events in {round(time.time()-t0,1)}s"))
