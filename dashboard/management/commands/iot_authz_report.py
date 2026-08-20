"""
Test/report: authorization profiles containing a token (default IOT, any case)
in the RADIUS summary, with unique-MAC counts. Read-only - a quick way to size
the IoT set before enabling ISE_DC_IOT_BY_AUTHZ discovery.

    manage.py iot_authz_report
    manage.py iot_authz_report --match IOT --macs 5
"""
from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Report RADIUS authorization profiles matching a token (default IOT) + unique MACs."

    def add_arguments(self, parser):
        parser.add_argument("--match", default=None,
                            help="token to match (default: settings AUTHZ_MATCH)")
        parser.add_argument("--view", default=None,
                            help="RADIUS view (default: DC LOCATION_VIEW)")
        parser.add_argument("--macs", type=int, default=0,
                            help="also list N sample MACs per profile")

    def handle(self, *args, **opts):
        from dashboard.services import get_dataconnect_client

        dc_cfg = settings.DATACONNECT
        match = (opts["match"] or dc_cfg["AUTHZ_MATCH"]).upper()
        view = opts["view"] or dc_cfg["LOCATION_VIEW"]
        authz = dc_cfg["COL_AUTHZ"]
        mac = dc_cfg["COL_LOC_MAC"]

        dc = get_dataconnect_client()
        _, rows = dc.query(
            f"SELECT {authz} AS authz, COUNT(DISTINCT {mac}) AS macs "
            f"FROM {view} WHERE UPPER({authz}) LIKE :m "
            f"GROUP BY {authz} ORDER BY macs DESC", {"m": f"%{match}%"})

        if not rows:
            self.stdout.write(self.style.WARNING(
                f"No authorization profiles matching '{match}' in {view}."))
            return

        w = max([len("AUTHORIZATION PROFILE")]
                + [len(str(r.get("authz") or "")) for r in rows])
        bar = "-" * (w + 16)
        self.stdout.write(bar)
        self.stdout.write(f"{'AUTHORIZATION PROFILE':<{w}}   UNIQUE MACS")
        self.stdout.write(bar)
        for r in rows:
            name = str(r.get("authz") or "")
            n = int(r.get("macs") or 0)
            self.stdout.write(f"{name:<{w}}   {n:>11,}")
        self.stdout.write(bar)

        _, uniq = dc.query(
            f"SELECT COUNT(DISTINCT {mac}) AS n FROM {view} "
            f"WHERE UPPER({authz}) LIKE :m", {"m": f"%{match}%"})
        total_unique = int(uniq[0]["n"]) if uniq else 0
        self.stdout.write(self.style.SUCCESS(
            f"{len(rows)} profile(s); {total_unique:,} unique MAC(s) "
            f"across all '{match}' authz profiles."))

        if opts["macs"]:
            self.stdout.write("")
            for r in rows:
                name = str(r.get("authz") or "")
                _, sample = dc.query(
                    f"SELECT DISTINCT {mac} AS mac FROM {view} "
                    f"WHERE {authz} = :a FETCH FIRST {int(opts['macs'])} ROWS ONLY",
                    {"a": name})
                macs = ", ".join(str(s.get("mac")) for s in sample)
                self.stdout.write(f"  {name}: {macs}")
