"""
Probe ISE Data Connect: connect, list views, and DUMP ALL COLUMNS + sample rows
from the endpoints view (and a few others) so we can see exactly which attributes
are available - device type, location, IP, identity group, etc. - before mapping
them into the sync.

    manage.py probe_dataconnect                 # endpoints_data + radius_authentications
    manage.py probe_dataconnect --view foo      # dump a specific view too
    manage.py probe_dataconnect --rows 3        # sample N rows (default 5)
    manage.py probe_dataconnect --sql "SELECT ..."   # run an ad-hoc query

Writes one JSON file per view to --out-dir (default api_out/).
"""
import json
import os
import time

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Inspect the ISE Data Connect schema (dump endpoint columns + samples)."

    # Views worth dumping by default (endpoints first — that's our discovery source).
    DEFAULT_VIEWS = ["endpoints_data", "radius_authentications",
                     "network_device_groups"]

    def add_arguments(self, parser):
        parser.add_argument("--out-dir", default="api_out")
        parser.add_argument("--rows", type=int, default=5,
                            help="sample rows per view (default 5)")
        parser.add_argument("--view", action="append", default=[],
                            help="extra view(s) to dump (repeatable)")
        parser.add_argument("--sql", default="",
                            help="run an ad-hoc SELECT and print the result")

    def handle(self, *args, **opts):
        from dashboard import services
        from integrations.ise_dataconnect import DataConnectError

        out_dir = opts["out_dir"]
        os.makedirs(out_dir, exist_ok=True)

        def write(name, data):
            with open(os.path.join(out_dir, f"dataconnect.{name}.json"), "w",
                      encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)

        def run(label, fn):
            self.stdout.write(f"  → {label:32} ... ", ending="")
            self.stdout.flush()
            t = time.perf_counter()
            try:
                data = fn()
                self.stdout.write(self.style.SUCCESS(
                    f"OK {round(time.perf_counter()-t,2)}s"))
                return data
            except Exception as exc:
                self.stdout.write(self.style.ERROR(
                    f"ERR {round(time.perf_counter()-t,2)}s {str(exc)[:90]}"))
                return {"error": str(exc)}

        try:
            dc = services.get_dataconnect_client()
        except Exception as exc:
            self.stdout.write(self.style.ERROR(f"Data Connect not configured: {exc}"))
            return

        self.stdout.write(self.style.MIGRATE_HEADING("=== ISE Data Connect ==="))
        write("_test", run("connect (SELECT 1 FROM dual)", dc.test))

        if opts["sql"]:
            res = run("ad-hoc sql", lambda: _qr(dc, opts["sql"]))
            write("_adhoc", res)
            self.stdout.write(json.dumps(res, indent=2, default=str)[:4000])
            return

        views_avail = run("list views", dc.list_views)
        write("_views", views_avail)
        if isinstance(views_avail, list):
            self.stdout.write(f"    {len(views_avail)} views visible")

        rows = opts["rows"]
        for view in self.DEFAULT_VIEWS + opts["view"]:
            def dump(v=view):
                cols = dc.columns(v)
                _, sample = dc.sample(v, rows)
                return {"view": v, "columns": cols, "column_count": len(cols),
                        "sample_rows": sample}
            data = run(f"dump {view}", dump)
            write(view, data)
            if isinstance(data, dict) and data.get("columns"):
                self.stdout.write("    columns: " + ", ".join(data["columns"]))

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Done. Per-view JSON (columns + samples) in {out_dir}/ — send back "
            f"dataconnect.endpoints_data.json so I can map device type / site / IP."))


def _qr(dc, sql):
    cols, rows = dc.query(sql)
    return {"columns": cols, "rows": rows}
