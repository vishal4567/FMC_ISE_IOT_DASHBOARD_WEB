"""
Fetch every external ISE/FMC dataset and write it to the DB Snapshot table, so
the web tier can serve it without any live API call. Same code Celery beat runs.

    manage.py snapshot_datasets
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Snapshot ISE/FMC datasets into the DB (web reads these, not the APIs)."

    def handle(self, *args, **opts):
        from dashboard import services

        self.stdout.write("Snapshotting ISE/FMC datasets ...")
        res = services.snapshot_all_datasets()
        self.stdout.write(self.style.SUCCESS(
            f"Done: {res['datasets']} datasets ({res['errors']} with errors) "
            f"+ connection status written to the DB."))
