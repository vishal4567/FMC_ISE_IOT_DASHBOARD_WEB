"""
Run the ISE IoT inventory sync on demand (same code the Celery beat runs).

    manage.py sync_ise              # refresh reference data, then sync endpoints
    manage.py sync_ise --sync-only  # skip the daily reference refresh
    manage.py sync_ise --ref-only   # only refresh reference data (profiles + NADs)
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Sync the IoT endpoint inventory from ISE into IoTDevice."

    def add_arguments(self, parser):
        parser.add_argument("--sync-only", action="store_true",
                            help="skip the daily reference refresh")
        parser.add_argument("--ref-only", action="store_true",
                            help="only refresh reference data (profiles + NAD map)")

    def handle(self, *args, **opts):
        from dashboard.tasks import refresh_ise_reference, sync_iot_endpoints

        if not opts["sync_only"]:
            self.stdout.write("→ refresh_ise_reference ...", ending=" ")
            self.stdout.flush()
            self.stdout.write(self.style.SUCCESS(str(refresh_ise_reference())))
        if opts["ref_only"]:
            return
        self.stdout.write("→ sync_iot_endpoints ...", ending=" ")
        self.stdout.flush()
        res = sync_iot_endpoints()
        style = self.style.SUCCESS if not res.get("error") else self.style.ERROR
        self.stdout.write(style(str(res)))
