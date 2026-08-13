"""
eStreamer event ingester.

    # live: pipe eNcore's JSON output straight in
    encore.sh foreground | python manage.py estreamer_ingest --source stdin

    # replay a captured file of eNcore JSON records (one per line)
    python manage.py estreamer_ingest --source file --path events.jsonl

    # capture the RAW incoming records to a file while ingesting (for parser
    # tuning). Stops after --capture-limit records (default 50); use 0 to keep
    # capturing everything. --capture-only writes the file without storing.
    encore.sh foreground | python manage.py estreamer_ingest \
        --capture encore-sample.jsonl --capture-limit 50

Each record is mapped (dashboard/estreamer/mapping.py), enriched with ISE
identity (device_type / site / in_ise, from the IoTDevice inventory that the
sync_iot_endpoints task keeps current), then bulk-written to SecurityEvent.
"""
import json
import sys
import time

from django.core.management.base import BaseCommand, CommandError

from dashboard import event_store
from dashboard.estreamer import mapping


class Command(BaseCommand):
    help = "Ingest FMC eStreamer (eNcore JSON) events into SecurityEvent."

    def add_arguments(self, parser):
        parser.add_argument("--source", choices=["stdin", "file"], default="stdin")
        parser.add_argument("--path", help="file source: path to JSON-lines file")
        parser.add_argument("--batch", type=int, default=500)
        parser.add_argument(
            "--capture", metavar="FILE",
            help="also write RAW incoming records (one JSON per line) to FILE "
                 "for parser tuning")
        parser.add_argument(
            "--capture-limit", type=int, default=50,
            help="stop capturing after N records (0 = capture everything). "
                 "Default 50")
        parser.add_argument(
            "--capture-only", action="store_true",
            help="capture to --capture FILE without writing to the database")

    def handle(self, *args, **opts):
        if opts["source"] == "file":
            if not opts["path"]:
                raise CommandError("--source file requires --path")
            stream = open(opts["path"], "r", encoding="utf-8")
        else:
            stream = sys.stdin

        capture_only = opts["capture_only"]
        capture_file = None
        capture_left = opts["capture_limit"]  # 0 means unlimited
        if opts["capture"] or capture_only:
            if not opts["capture"]:
                raise CommandError("--capture-only requires --capture FILE")
            capture_file = open(opts["capture"], "w", encoding="utf-8")

        ise_map = event_store.ise_identity_map()
        batch, total, captured = [], 0, 0
        last_refresh = time.time()

        try:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except ValueError:
                    continue

                # --- capture raw record (verbatim) ---
                if capture_file and (capture_left == 0 or captured < capture_left):
                    capture_file.write(json.dumps(raw) + "\n")
                    captured += 1
                    if capture_only and capture_left and captured >= capture_left:
                        break  # capture-only: stop once the sample is collected

                if capture_only:
                    continue  # don't touch the DB in capture-only mode

                ev = mapping.map_event(raw)
                event_store.enrich_with_ise(ev, ise_map)
                batch.append(ev)

                if len(batch) >= opts["batch"]:
                    total += event_store.bulk_ingest(batch)
                    batch = []
                    self.stdout.write(f"ingested {total}", ending="\r")

                if time.time() - last_refresh > 300:
                    ise_map = event_store.ise_identity_map()
                    last_refresh = time.time()
        finally:
            if opts["source"] == "file":
                stream.close()
            if capture_file:
                capture_file.close()

        if batch:
            total += event_store.bulk_ingest(batch)

        if capture_file:
            self.stdout.write(self.style.SUCCESS(
                f"Captured {captured} raw records to {opts['capture']}"))
        if not capture_only:
            self.stdout.write(self.style.SUCCESS(f"\nIngested {total} events."))
