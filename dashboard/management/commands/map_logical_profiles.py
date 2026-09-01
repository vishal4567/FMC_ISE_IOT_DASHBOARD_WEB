"""
Update IoTDevice.logical_profile from each device's PROFILER policy
(IoTDevice.ise_profile) using the ISE profiler-policy -> logical-profile map
(logical_profiles.assigned_policies -> logical_profile).

No full re-sync and no per-device ISE fetch: read the small mapping view once,
then bulk-update the DB in place. Use to (back)fill or refresh the logical
profile for devices already onboarded.

    manage.py map_logical_profiles
    manage.py map_logical_profiles --all-logical   # map against ALL logical
                                                    # profiles, not just the IoT set
"""
from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Set IoTDevice.logical_profile from ise_profile via the ISE mapping."

    def add_arguments(self, parser):
        parser.add_argument(
            "--all-logical", action="store_true",
            help="map against every logical profile (default: only the IoT set "
                 "- ISE_IOT_LOGICAL_PROFILES + ISE_DC_LOGICAL_MATCH)")

    def handle(self, *args, **opts):
        from dashboard.models import IoTDevice
        from dashboard.services import get_dataconnect_client

        dc_cfg = settings.DATACONNECT
        ise_cfg = settings.ISE
        lp_name, lp_policy = dc_cfg["LP_NAME_COL"], dc_cfg["LP_POLICY_COL"]

        # Build the (IoT-scoped, unless --all-logical) mapping query.
        binds, conds = {}, []
        if not opts["all_logical"]:
            for i, name in enumerate(ise_cfg["IOT_LOGICAL_PROFILES"]):
                binds[f"l{i}"] = name
            if binds:
                conds.append(f"{lp_name} IN ({', '.join(f':{k}' for k in binds)})")
            if dc_cfg["LOGICAL_MATCH"]:
                binds["lpm"] = f"%{dc_cfg['LOGICAL_MATCH'].upper()}%"
                conds.append(f"UPPER({lp_name}) LIKE :lpm")
        where = f" WHERE ({' OR '.join(conds)})" if conds else ""

        dc = get_dataconnect_client()
        dc.log = lambda m: (self.stdout.write(m), self.stdout.flush())
        _, rows = dc.query(
            f"SELECT {lp_policy} AS policy, {lp_name} AS lp "
            f"FROM {dc_cfg['LP_VIEW']}{where}", binds)

        mapping = {}
        for r in rows:
            policy = str(r.get("policy") or "").strip()
            lp = str(r.get("lp") or "").strip()
            if policy and lp:
                mapping.setdefault(policy, lp)   # first logical profile wins
        self.stdout.write(f"mapping: {len(mapping)} profiler policies -> logical")
        if not mapping:
            self.stdout.write(self.style.WARNING("empty mapping - nothing to do"))
            return

        updated = []
        for d in IoTDevice.objects.all().only("id", "ise_profile",
                                              "logical_profile", "ise_identity_group"):
            lp = mapping.get((d.ise_profile or "").strip())
            if lp and lp != d.logical_profile:
                d.logical_profile = lp
                d.ise_identity_group = lp
                updated.append(d)
        if updated:
            IoTDevice.objects.bulk_update(
                updated, ["logical_profile", "ise_identity_group"], batch_size=2000)
        self.stdout.write(self.style.SUCCESS(
            f"updated logical_profile on {len(updated)} device(s)"))
