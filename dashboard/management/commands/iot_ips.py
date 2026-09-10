"""
Distinct, unique IP list of IoT devices, keyed on the AUTHORIZATION PROFILE (and
optionally the security group) in radius_authentications - the only RADIUS view
that carries framed_ip_address. Writes one IP per line to a file (a big list
would truncate in the terminal) and prints the count.

    manage.py iot_ips                         # authz profile ~ IOT, all history
    manage.py iot_ips --match IOT --days 90    # last 90 days (faster)
    manage.py iot_ips --field both             # authz OR security_group ~ IOT
    manage.py iot_ips --field sgt --extra-sgt CCTV_Cameras Access_Control_Devices BMS_Devices
    manage.py iot_ips --out /tmp/iot_ips.txt

framed_ip_address is not in radius_authentication_summary, so this reads the full
radius_authentications log - use --days to bound the scan.
"""
import os

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Distinct IoT IP list from radius_authentications by authorization profile (+SGT)."

    def add_arguments(self, parser):
        parser.add_argument("--match", default="IOT",
                            help="token the profile/SGT name must contain (default IOT)")
        parser.add_argument("--field", choices=["authz", "sgt", "both"],
                            default="authz",
                            help="which field(s) to match: authorization_profiles "
                                 "(authz), security_group (sgt), or both")
        parser.add_argument("--extra-sgt", nargs="*", default=[],
                            help="extra security_group names to include verbatim "
                                 "(e.g. CCTV_Cameras Access_Control_Devices BMS_Devices)")
        parser.add_argument("--days", type=int, default=0,
                            help="only look back N days (0 = all history)")
        parser.add_argument("--view", default="radius_authentications",
                            help="RADIUS view (must carry framed_ip_address)")
        parser.add_argument("--ip-col", default="framed_ip_address")
        parser.add_argument("--out", default="api_out/iot_ips.txt",
                            help="output file (one IP per line)")

    def handle(self, *args, **opts):
        from dashboard.services import get_dataconnect_client

        dc = settings.DATACONNECT
        view = opts["view"]
        ipc = opts["ip_col"]
        mac = dc["COL_LOC_MAC"]
        tok = f"%{opts['match'].upper()}%"

        conds, binds = [], {"m": tok}
        if opts["field"] in ("authz", "both"):
            conds.append(f"UPPER({dc['COL_AUTHZ']}) LIKE :m")
        if opts["field"] in ("sgt", "both"):
            conds.append("UPPER(security_group) LIKE :m")
        if opts["extra_sgt"]:
            keys = []
            for i, name in enumerate(opts["extra_sgt"]):
                binds[f"s{i}"] = name
                keys.append(f":s{i}")
            conds.append(f"security_group IN ({', '.join(keys)})")
        match_where = "(" + " OR ".join(conds) + ")"

        window = ""
        if opts["days"] and dc["COL_LOC_TIME"]:
            window = (f" AND {dc['COL_LOC_TIME']} >= SYSTIMESTAMP "
                      f"- INTERVAL '{int(opts['days'])}' DAY")

        where = f"{match_where} AND {ipc} IS NOT NULL{window}"

        client = get_dataconnect_client()
        client.log = lambda msg: (self.stdout.write(msg), self.stdout.flush())

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"IoT IPs by {opts['field']} ~ '{opts['match']}'"
            + (f" (+{len(opts['extra_sgt'])} named SGT)" if opts["extra_sgt"] else "")
            + (f", last {opts['days']}d" if opts["days"] else ", all history")))

        with client.session():
            # unique IPs, unique MACs, and rows - so you can see IP vs MAC spread
            _, agg = client.query(
                f"SELECT COUNT(DISTINCT {ipc}) AS ips, COUNT(DISTINCT {mac}) AS macs "
                f"FROM {view} WHERE {where}", binds)
            n_ips = int(agg[0]["ips"]) if agg else 0
            n_macs = int(agg[0]["macs"]) if agg else 0
            self.stdout.write(self.style.SUCCESS(
                f"  {n_ips:,} distinct IPs across {n_macs:,} distinct MACs"))
            if not n_ips:
                self.stdout.write("  (nothing matched)")
                return
            _, rows = client.query(
                f"SELECT DISTINCT {ipc} AS ip FROM {view} WHERE {where} "
                f"ORDER BY {ipc}", binds)

        ips = [str(r["ip"]) for r in rows if r.get("ip")]
        out = opts["out"]
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(ips) + "\n")
        self.stdout.write(self.style.SUCCESS(f"  wrote {len(ips):,} IPs -> {out}"))
        for ip in ips[:20]:
            self.stdout.write(f"    {ip}")
        if len(ips) > 20:
            self.stdout.write(f"    ... {len(ips) - 20} more (see {out})")
