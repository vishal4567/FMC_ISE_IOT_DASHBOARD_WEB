"""
IoT sync driven by the RADIUS AUTHORIZATION RULE.

Discovers every device whose authorization_rule contains 'IOT' (any case) from
the full radius_authentications log, taking the LATEST auth row per MAC. Maps:
    device_type            <- identity_group         (the class, e.g. Wipro_CCTV)
    ise_profile            <- endpoint_profile        (the profiler policy)
    authorization_profile  <- authorization_rule     (e.g. IOT-CCTV, IOT-Quarantine-Access)
    ip                     <- framed_ip_address
    site                   <- device_name -> site-code map (backup: location leaf)
A device whose authorization_profile contains 'Quarantine' is a quarantined
device (filter on that in the dashboard).

    manage.py sync_iot_authz_rule                 # upsert all (full history)
    manage.py sync_iot_authz_rule --days 90        # bound the scan for speed
    manage.py sync_iot_authz_rule --additive       # insert only new MACs
    manage.py sync_iot_authz_rule --match IOT --limit 500
"""
import sys
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Sync IoT devices discovered by RADIUS authorization_rule (~IOT)."

    def add_arguments(self, parser):
        parser.add_argument("--match", default=None,
                            help="token the rule must contain (default settings "
                                 "AUTHZ_RULE_MATCH = IOT)")
        parser.add_argument("--days", type=int, default=0,
                            help="only scan the last N days (0 = all history)")
        parser.add_argument("--additive", action="store_true",
                            help="insert only new MACs; keep existing rows")
        parser.add_argument("--limit", type=int, default=0,
                            help="cap discovered MACs (0 = all)")
        parser.add_argument("--prune", action="store_true",
                            help="DELETE IoTDevice rows whose MAC is NOT in this "
                                 "run's discovery, so the baseline becomes exactly "
                                 "the authz-rule set (removes old-baseline devices). "
                                 "Refused with --additive/--limit; use full history.")
        parser.add_argument("--prune-guard", type=float, default=0.5,
                            help="safety: SKIP the prune if the discovered set is "
                                 "smaller than this fraction of the current "
                                 "inventory (guards against a partial query wiping "
                                 "devices). Default 0.5; set 0 to force prune.")
        parser.add_argument("--remap", action="store_true",
                            help="after the sync, re-map ONLY the events tied to "
                                 "devices ADDED or REMOVED this run (by MAC or flow "
                                 "IP) - not the whole event table.")

    def handle(self, *args, **opts):
        from dashboard.models import IoTDevice
        from dashboard.services import get_dataconnect_client
        from dashboard.site_mapping import db_site_matcher
        from integrations.location_map import site_from_hostname

        dc = settings.DATACONNECT
        match = opts["match"] or dc.get("AUTHZ_RULE_MATCH", "IOT")
        t0 = time.time()

        if opts["prune"] and (opts["additive"] or opts["limit"]):
            self.stderr.write(self.style.ERROR(
                "--prune needs the FULL discovery set; not allowed with "
                "--additive or --limit."))
            return
        if opts["prune"] and opts["days"]:
            self.stdout.write(self.style.WARNING(
                f"--prune with --days {opts['days']}: devices that did not "
                f"authenticate in the last {opts['days']}d will be DELETED."))

        client = get_dataconnect_client()
        client.log = lambda m: (self.stdout.write(m), self.stdout.flush())

        # 1. discover — latest auth row per MAC where the rule ~ IOT
        rows = client.iot_by_authz_rule(
            match=match,
            view=dc.get("AUTHENTICATIONS_VIEW", "radius_authentications"),
            mac_col=dc["COL_LOC_MAC"],
            rule_col=dc.get("COL_AUTHZ_RULE", "authorization_rule"),
            profile_col=dc.get("AUTHZ_COL_PROFILE", "endpoint_profile"),
            group_col=dc.get("COL_IDENTITY_GROUP", "identity_group"),
            ip_col=dc.get("COL_FRAMED_IP", "framed_ip_address"),
            host_col=dc["LOC_HOST_COL"] or "device_name",
            loc_col=dc["COL_LOC_SITE"], time_col=dc["COL_LOC_TIME"],
            days=opts["days"], limit=opts["limit"])
        self.stdout.write(f"discovered {len(rows)} devices "
                          f"({round(time.time()-t0,1)}s)")
        if not rows:
            self.stdout.write(self.style.WARNING("nothing discovered"))
            return

        # snapshot the current inventory (mac -> ip) BEFORE writing, so we can
        # compute what this run ADDS and REMOVES for the targeted event re-map.
        cur = {m: ip for m, ip in IoTDevice.objects.values_list("mac", "ip")}
        discovered = {r["mac"] for r in rows}
        added_macs = discovered - set(cur)
        removed_macs = set(cur) - discovered
        added_ips = {str(r["ip"]) for r in rows
                     if r["mac"] in added_macs and r.get("ip")}
        removed_ips = {str(cur[m]) for m in removed_macs if cur.get(m)}

        if opts["additive"]:
            rows = [r for r in rows if r["mac"] not in cur]
            self.stdout.write(f"additive: {len(rows)} new (kept {len(cur)})")
            if not rows:
                self.stdout.write(self.style.SUCCESS("no new devices"))
                return

        # 2. site — NAD hostname (device_name) via the site-code map; else the
        #    RADIUS location leaf already returned on each row.
        matcher = db_site_matcher()
        now = timezone.now()

        objs = []
        quarantined = 0
        for r in rows:
            site = site_from_hostname(r.get("host", ""), matcher) \
                or r.get("location", "") or ""
            rule = r.get("authz_rule", "") or ""
            profile = r.get("endpoint_profile", "") or ""
            igroup = r.get("identity_group", "") or ""
            if "QUARANTINE" in rule.upper():
                quarantined += 1
            objs.append(IoTDevice(
                mac=r["mac"],
                # device_type = ISE identity group (the real class, e.g.
                # Wipro_CCTV); endpoint_profile kept in ise_profile.
                device_type=igroup or profile,
                site=site,
                ip=r.get("ip") or None,
                ise_profile=profile,
                ise_identity_group=igroup,
                authorization_profile=rule,
                correlation="Matched",
                ise_endpoint_mac=r["mac"],
                last_seen=now,
            ))

        # 3. bulk UPSERT with a progress bar
        total = len(objs)
        chunk = 2000
        written = 0
        tw = time.time()
        for i in range(0, total, chunk):
            part = objs[i:i + chunk]
            if opts["additive"]:
                IoTDevice.objects.bulk_create(part, ignore_conflicts=True)
            else:
                IoTDevice.objects.bulk_create(
                    part, update_conflicts=True, unique_fields=["mac"],
                    update_fields=["device_type", "site", "ip", "ise_profile",
                                   "ise_identity_group", "authorization_profile",
                                   "correlation", "last_seen"])
            written += len(part)
            pct = int(100 * written / total)
            fill = pct * 30 // 100
            rate = written / (time.time() - tw) if time.time() > tw else 0
            sys.stdout.write(
                f"\rwriting [{'#' * fill}{'.' * (30 - fill)}] {pct:3d}%  "
                f"{written:,}/{total:,}  {rate:,.0f}/s   ")
            sys.stdout.flush()

        self.stdout.write(self.style.SUCCESS(
            f"\n{'added' if opts['additive'] else 'upserted'} {total:,} devices "
            f"({quarantined:,} quarantined) in {round(time.time()-t0,1)}s"))

        self.stdout.write(f"delta: +{len(added_macs):,} added / "
                          f"-{len(removed_macs):,} removed vs {len(cur):,} existing")

        # 4. guarded prune — delete devices no longer in the IoT set, UNLESS the
        #    discovery looks partial (kept < guard * current), which would wipe
        #    good devices on a transient query failure.
        pruned = 0
        if opts["prune"]:
            guard = opts["prune_guard"]
            if guard > 0 and cur and len(discovered) < guard * len(cur):
                self.stdout.write(self.style.WARNING(
                    f"prune SKIPPED: discovered {len(discovered):,} < "
                    f"{guard:.0%} of {len(cur):,} existing - looks partial, "
                    f"not deleting. (use --prune-guard 0 to force.)"))
                removed_macs, removed_ips = set(), set()   # nothing removed
            else:
                stale = IoTDevice.objects.exclude(mac__in=discovered)
                pruned = stale.count()
                if pruned:
                    stale.delete()
                self.stdout.write(self.style.SUCCESS(
                    f"pruned {pruned:,} device(s) not in the baseline "
                    f"(kept {len(discovered):,})"))

        # 5. targeted re-map — re-stamp ONLY the events tied to devices added or
        #    removed this run (by MAC or flow IP), from the now-current baseline.
        if opts["remap"]:
            self._remap_delta(added_macs | removed_macs, added_ips | removed_ips)

    def _remap_delta(self, macs, ips):
        from django.db.models import Q

        from dashboard import event_store
        from dashboard.models import SecurityEvent

        if not macs and not ips:
            self.stdout.write("re-map: no device changes, nothing to re-map")
            return
        q = Q()
        if macs:
            q |= Q(device_mac__in=macs)
        if ips:
            q |= Q(source_ip__in=ips) | Q(dest_ip__in=ips) | Q(device_ip__in=ips)

        ise_map = event_store.ise_identity_map()
        ip_map = event_store.ise_ip_map()
        fields = event_store.REMAP_FIELDS
        qs = SecurityEvent.objects.filter(q)
        total = qs.count()
        self.stdout.write(f"re-mapping {total:,} events tied to "
                          f"{len(macs):,} changed MACs / {len(ips):,} IPs ...")
        done = changed = 0
        buf = []
        for ev in qs.only("id", *fields, "source_ip", "dest_ip").iterator(
                chunk_size=5000):
            if event_store.remap_row(ev, ise_map, ip_map):
                buf.append(ev)
            done += 1
            if len(buf) >= 5000:
                SecurityEvent.objects.bulk_update(buf, fields)
                changed += len(buf)
                buf = []
        if buf:
            SecurityEvent.objects.bulk_update(buf, fields)
            changed += len(buf)
        self.stdout.write(self.style.SUCCESS(
            f"re-mapped {done:,} delta events, {changed:,} rows changed"))
