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

SCALE: FMC streams its FULL flow (thousands/sec). By default this keeps ONLY
events that match an ISE IoT device (by MAC or src/dst IP) and drops everything
else BEFORE the DB — otherwise Postgres would take ~190M rows/day of non-IoT
traffic. Use --store-all to disable the filter, --keep-threats to also retain
non-IoT intrusion/malware events.
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
        parser.add_argument(
            "--store-all", action="store_true",
            help="store EVERY event. Default: keep ONLY events matching an ISE "
                 "IoT device (by MAC or src/dst IP) and drop the ~99%% non-IoT "
                 "flow before it reaches the DB — required at FMC's full rate.")
        parser.add_argument(
            "--keep-threats", action="store_true",
            help="also keep non-IoT Intrusion/Malware/Security-Intelligence "
                 "events even when they don't match an IoT device")
        parser.add_argument(
            "--progress", type=int, default=30,
            help="seconds between throughput lines (seen/stored/drop%%) on "
                 "stderr. 0 = off. Default 30.")

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

        store_all = opts["store_all"]
        keep_threats = opts["keep_threats"]
        progress = opts["progress"]
        _THREATS = ("Intrusion", "Malware", "Security Intelligence")

        ise_map = event_store.ise_identity_map()
        ip_map = event_store.ise_ip_map()
        batch, total, captured = [], 0, 0
        seen, dropped, no_ip = 0, 0, 0
        last_refresh = time.time()
        last_progress, last_seen = last_refresh, 0

        if not store_all and not ise_map and not ip_map:
            self.stderr.write(self.style.WARNING(
                "IoT filter is ON but the ISE inventory is EMPTY — every event "
                "will be dropped. Run sync_ise first, or pass --store-all."))

        try:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except ValueError:
                    continue
                seen += 1

                # --- capture raw record (verbatim, pre-filter) ---
                if capture_file and (capture_left == 0 or captured < capture_left):
                    capture_file.write(json.dumps(raw) + "\n")
                    captured += 1
                    if capture_only and capture_left and captured >= capture_left:
                        break  # capture-only: stop once the sample is collected

                if capture_only:
                    continue  # don't touch the DB in capture-only mode

                mac, ips = mapping.candidate_ids(raw)

                # --- drop records with NO IP: not attributable to any device ---
                if not ips:
                    no_ip += 1
                    dropped += 1
                    continue

                # --- IoT gate: drop non-IoT flow BEFORE the expensive mapping ---
                if not store_all:
                    match = (mac and mac != "NONE" and mac in ise_map) or \
                            any(ip in ip_map for ip in ips)
                    if not match and not (
                            keep_threats and mapping._event_type(raw) in _THREATS):
                        dropped += 1
                        continue

                ev = mapping.map_event(raw)
                event_store.enrich_with_ise(ev, ise_map, ip_map)
                batch.append(ev)

                if len(batch) >= opts["batch"]:
                    total += event_store.bulk_ingest(batch)
                    batch = []

                now = time.time()
                if progress and now - last_progress >= progress:
                    rate = (seen - last_seen) / (now - last_progress)
                    pct = (100.0 * dropped / seen) if seen else 0.0
                    self.stderr.write(
                        f"[ingest] seen={seen} stored={total} dropped={dropped} "
                        f"(no-ip={no_ip}, {pct:.1f}% filtered) in={rate:.0f}/s")
                    last_progress, last_seen = now, seen

                if now - last_refresh > 300:
                    ise_map = event_store.ise_identity_map()
                    ip_map = event_store.ise_ip_map()
                    last_refresh = now
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
            self.stdout.write(self.style.SUCCESS(
                f"\nIngested {total} IoT events (seen {seen}, dropped {dropped}: "
                f"{no_ip} no-IP + {dropped - no_ip} non-IoT)."))
