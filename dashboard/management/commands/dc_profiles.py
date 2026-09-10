"""
CANDIDATE DISCOVERY (read-only): print the DISTINCT profile-like values across
every ISE Data Connect table we might key IoT discovery on, with unique-MAC
counts, so you can eyeball which ones are IoT candidates before wiring a sync.

Sweeps four sources (all configurable via settings.DATACONNECT):
  authz     radius_authentication_summary.authorization_profiles  (per MAC)
  sgt       radius_authentication_summary.security_group          (per MAC)
  endpoint  endpoints_data.endpoint_policy   (profiler/endpoint profiles, per MAC)
  logical   logical_profiles.logical_profile (+ member policies & device count)

    manage.py dc_profiles                 # all sources, all values
    manage.py dc_profiles --match IOT      # only values containing IOT (any case)
    manage.py dc_profiles --source authz endpoint
    manage.py dc_profiles --top 25 --macs 3
Nothing is written.
"""
from django.conf import settings
from django.core.management.base import BaseCommand

SOURCES = ("authz", "sgt", "endpoint", "logical")


class Command(BaseCommand):
    help = "List distinct IoT-candidate profiles (authz/sgt/endpoint/logical) + MAC counts."

    def add_arguments(self, parser):
        parser.add_argument("--match", default=None,
                            help="only values containing this token (case-insensitive); "
                                 "omit for ALL")
        parser.add_argument("--source", nargs="+", choices=SOURCES + ("all",),
                            default=["all"], help="which sources to scan (default all)")
        parser.add_argument("--top", type=int, default=50,
                            help="max rows to print per source (default 50)")
        parser.add_argument("--macs", type=int, default=0,
                            help="also list N sample MACs per value")

    def handle(self, *args, **opts):
        from dashboard.services import get_dataconnect_client

        dc = settings.DATACONNECT
        want = set(SOURCES) if "all" in opts["source"] else set(opts["source"])
        match = (opts["match"] or "").upper().strip()
        top, nmacs = opts["top"], opts["macs"]

        client = get_dataconnect_client()
        client.log = lambda m: (self.stdout.write(m), self.stdout.flush())

        # (key, header, view, value_col, mac_col)  mac_col="" -> COUNT(*) not DISTINCT MAC
        specs = [
            ("authz", "AUTHORIZATION PROFILE (radius_authentication_summary)",
             dc["LOCATION_VIEW"], dc["COL_AUTHZ"], dc["COL_LOC_MAC"]),
            ("sgt", "SECURITY GROUP (radius_authentication_summary)",
             dc["LOCATION_VIEW"], "security_group", dc["COL_LOC_MAC"]),
            ("endpoint", "ENDPOINT / PROFILER POLICY (endpoints_data)",
             dc["ENDPOINTS_VIEW"], dc["COL_PROFILE"], dc["COL_MAC"]),
        ]

        with client.session():
            for key, header, view, col, mac in specs:
                if key not in want:
                    continue
                self._distinct(client, key, header, view, col, mac, match, top, nmacs)
            if "logical" in want:
                self._logical(client, dc, match, top)

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Done - nothing written."))

    # ------------------------------------------------------------------ #
    def _distinct(self, client, key, header, view, col, mac, match, top, nmacs):
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(f"== {header} =="))
        cnt = f"COUNT(DISTINCT {mac})" if mac else "COUNT(*)"
        binds = {"m": f"%{match}%"} if match else {}
        sql = (f"SELECT {col} AS val, {cnt} AS n FROM {view} "
               f"WHERE {col} IS NOT NULL"
               + (f" AND UPPER({col}) LIKE :m" if match else "")
               + f" GROUP BY {col} ORDER BY n DESC")
        try:
            _, rows = client.query(sql, binds)
        except Exception as exc:
            self.stdout.write(self.style.ERROR(f"  FAILED: {str(exc)[:140]}"))
            return
        if not rows:
            self.stdout.write("  (none)")
            return
        w = min(60, max([len("VALUE")] + [len(str(r.get("val") or "")) for r in rows]))
        label = "UNIQUE MACS" if mac else "ROWS"
        self.stdout.write(f"  {'VALUE':<{w}}   {label}")
        self.stdout.write("  " + "-" * (w + 14))
        for r in rows[:top]:
            self.stdout.write(f"  {str(r.get('val') or '')[:w]:<{w}}   {int(r.get('n') or 0):>11,}")
        if len(rows) > top:
            self.stdout.write(f"  ... {len(rows) - top} more")
        self.stdout.write(self.style.SUCCESS(f"  {len(rows)} distinct value(s)"))

        if nmacs and mac:
            for r in rows[:top]:
                val = str(r.get("val") or "")
                try:
                    _, s = client.query(
                        f"SELECT DISTINCT {mac} AS mac FROM {view} WHERE {col} = :v "
                        f"FETCH FIRST {int(nmacs)} ROWS ONLY", {"v": val})
                    self.stdout.write(f"    {val}: "
                                      + ", ".join(str(x.get('mac')) for x in s))
                except Exception:
                    pass

    def _logical(self, client, dc, match, top):
        """Logical profiles have no MAC column; join to endpoints_data via the
        member profiling policies to get a real device count per logical profile."""
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(
            "== LOGICAL PROFILE (logical_profiles -> endpoints_data) =="))
        lp, name, pol = dc["LP_VIEW"], dc["LP_NAME_COL"], dc["LP_POLICY_COL"]
        ev, emac, eprof = dc["ENDPOINTS_VIEW"], dc["COL_MAC"], dc["COL_PROFILE"]
        where = f"WHERE UPPER(lp.{name}) LIKE :m" if match else ""
        binds = {"m": f"%{match}%"} if match else {}
        try:
            _, rows = client.query(
                f"SELECT lp.{name} AS val, COUNT(DISTINCT e.{emac}) AS n "
                f"FROM {lp} lp LEFT JOIN {ev} e ON e.{eprof} = lp.{pol} "
                f"{where} GROUP BY lp.{name} ORDER BY n DESC", binds)
        except Exception as exc:
            self.stdout.write(self.style.ERROR(f"  FAILED: {str(exc)[:140]}"))
            return
        if not rows:
            self.stdout.write("  (none)")
            return
        w = min(60, max([len("LOGICAL PROFILE")]
                        + [len(str(r.get("val") or "")) for r in rows]))
        self.stdout.write(f"  {'LOGICAL PROFILE':<{w}}   DEVICES")
        self.stdout.write("  " + "-" * (w + 12))
        for r in rows[:top]:
            self.stdout.write(f"  {str(r.get('val') or '')[:w]:<{w}}   {int(r.get('n') or 0):>9,}")
        self.stdout.write(self.style.SUCCESS(f"  {len(rows)} logical profile(s)"))
