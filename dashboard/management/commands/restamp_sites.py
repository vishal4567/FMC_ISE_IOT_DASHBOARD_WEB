"""
Re-resolve every device's site from the NAD hostname (current SiteCode map) and
write it onto the existing inventory - and, with --events, propagate it to the
already-ingested SecurityEvent rows. Use after editing the Config mapping so
current data shows the friendly sites immediately, without waiting for a sync.

    manage.py restamp_sites            # update IoTDevice.site only
    manage.py restamp_sites --events   # also bulk-update SecurityEvent.site
"""
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Re-stamp IoTDevice.site (and optionally events) from the NAD-hostname map."

    def add_arguments(self, parser):
        parser.add_argument(
            "--events", action="store_true",
            help="also bulk-update existing SecurityEvent.site from the inventory")

    def handle(self, *args, **opts):
        from dashboard.models import IoTDevice
        from dashboard.services import get_dataconnect_client
        from dashboard.site_mapping import db_site_matcher

        dc_cfg = settings.DATACONNECT
        macs = list(IoTDevice.objects.values_list("mac", flat=True))
        if not macs:
            self.stdout.write(self.style.WARNING("No IoTDevice rows — run sync first."))
            return

        self.stdout.write(f"Resolving site via NAD hostname for {len(macs)} devices...")
        dc = get_dataconnect_client()
        dc.log = lambda m: (self.stdout.write(m), self.stdout.flush())
        if dc_cfg.get("LOC_HOST_COL"):
            loc = dc.location_by_device_name(
                macs, matcher=db_site_matcher(), view=dc_cfg["LOCATION_VIEW"],
                mac_col=dc_cfg["COL_LOC_MAC"], host_col=dc_cfg["LOC_HOST_COL"],
                time_col=dc_cfg["COL_LOC_TIME"], days=dc_cfg["LOCATION_DAYS"])
        else:
            loc = dc.location_by_nad_hostname(
                macs, matcher=db_site_matcher(), nd_view=dc_cfg["ND_VIEW"],
                nd_name_col=dc_cfg["ND_NAME_COL"], nd_ip_col=dc_cfg["ND_IP_COL"],
                radius_view=dc_cfg["LOCATION_VIEW"], mac_col=dc_cfg["COL_LOC_MAC"],
                nas_col=dc_cfg["COL_NAS_IP"])

        updated = []
        for d in IoTDevice.objects.all():
            site = loc.get(d.mac.upper()) or loc.get(d.mac)
            if site and site != d.site:
                d.site = site
                updated.append(d)
        if updated:
            IoTDevice.objects.bulk_update(updated, ["site", "updated_at"],
                                          batch_size=1000)
        self.stdout.write(self.style.SUCCESS(
            f"Updated {len(updated)} IoTDevice site(s)."))

        if opts["events"]:
            self.stdout.write("Propagating to SecurityEvent (bulk SQL)...")
            with connection.cursor() as cur:
                cur.execute("""
                    UPDATE dashboard_securityevent e
                    SET site = d.site
                    FROM dashboard_iotdevice d
                    WHERE UPPER(e.device_mac) = UPPER(d.mac)
                      AND e.site IS DISTINCT FROM d.site
                """)
                self.stdout.write(self.style.SUCCESS(
                    f"Updated {cur.rowcount} SecurityEvent row(s)."))
