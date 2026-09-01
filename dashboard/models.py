"""
Production data model: ingested FMC events + hourly rollups + device inventory.

In POC mode (EVENT_BACKEND=memory) these tables are unused. In production
(EVENT_BACKEND=db) the ingester writes ``SecurityEvent`` rows, Celery rolls them
up into ``HourlyAggregate``, and the dashboard analytics query the DB.
"""
from django.db import models


class SecurityEvent(models.Model):
    """One FMC event (intrusion / connection / malware / file / SI).

    Field names mirror the event dicts the dashboard analytics already consume,
    so the DB backend is a drop-in for the in-memory feed.
    """

    ts = models.DateTimeField(db_index=True)
    hour = models.CharField(max_length=16, db_index=True)  # "YYYY-MM-DD HH:00"
    event_type = models.CharField(max_length=32, db_index=True)
    severity = models.CharField(max_length=16, db_index=True)
    impact = models.IntegerField(default=0)

    device_mac = models.CharField(max_length=32, db_index=True)
    device_ip = models.GenericIPAddressField(null=True, blank=True)
    device_type = models.CharField(max_length=48, db_index=True, blank=True)
    hostname = models.CharField(max_length=64, blank=True)
    site = models.CharField(max_length=48, db_index=True, blank=True)
    location = models.CharField(max_length=128, blank=True)

    in_ise = models.BooleanField(default=False)
    mapped_ise_mac = models.CharField(max_length=32, blank=True)

    source_ip = models.GenericIPAddressField(null=True, blank=True)
    dest_ip = models.GenericIPAddressField(null=True, blank=True)
    dest_country = models.CharField(max_length=48, blank=True)
    application = models.CharField(max_length=48, blank=True)
    protocol = models.CharField(max_length=16, blank=True)
    port = models.IntegerField(null=True, blank=True)
    insecure_protocol = models.BooleanField(default=False)

    bytes_sent = models.BigIntegerField(default=0)
    bytes_received = models.BigIntegerField(default=0)
    total_bytes = models.BigIntegerField(default=0)

    action = models.CharField(max_length=24, blank=True)
    rule_matched = models.CharField(max_length=128, blank=True)
    ips_policy = models.CharField(max_length=128, blank=True)
    src_zone = models.CharField(max_length=64, blank=True)
    dst_zone = models.CharField(max_length=64, blank=True)
    firewall = models.CharField(max_length=128, blank=True)

    threat_name = models.CharField(max_length=256, blank=True)
    threat_category = models.CharField(max_length=128, blank=True)
    classtype = models.CharField(max_length=64, blank=True)
    zone_violation = models.BooleanField(default=False)

    ingested_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["ts", "event_type"]),
            models.Index(fields=["device_type", "site"]),
            models.Index(fields=["device_mac", "ts"]),
        ]
        ordering = ["-ts"]

    def __str__(self):
        return f"{self.ts:%Y-%m-%d %H:%M} {self.event_type} {self.device_mac}"


class HourlyAggregate(models.Model):
    """Per-hour, per-(site, device_type) rollup for the trend/leaderboard
    widgets over the 90-day window - kept even after raw events are purged."""

    hour = models.CharField(max_length=16, db_index=True)
    site = models.CharField(max_length=48, db_index=True, blank=True)
    device_type = models.CharField(max_length=48, db_index=True, blank=True)

    events = models.IntegerField(default=0)
    threats = models.IntegerField(default=0)
    critical = models.IntegerField(default=0)
    blocked = models.IntegerField(default=0)
    allowed = models.IntegerField(default=0)
    total_bytes = models.BigIntegerField(default=0)
    devices = models.IntegerField(default=0)

    class Meta:
        unique_together = ("hour", "site", "device_type")
        indexes = [models.Index(fields=["hour", "site", "device_type"])]

    def __str__(self):
        return f"{self.hour} {self.site}/{self.device_type}"


class IoTDevice(models.Model):
    """Correlated device inventory (ISE identity + last-seen FMC activity),
    refreshed by the config-poll task."""

    mac = models.CharField(max_length=32, unique=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    hostname = models.CharField(max_length=64, blank=True)
    device_type = models.CharField(max_length=48, blank=True)
    site = models.CharField(max_length=48, blank=True)

    ise_identity_group = models.CharField(max_length=64, blank=True)
    ise_profile = models.CharField(max_length=64, blank=True)
    logical_profile = models.CharField(max_length=128, blank=True)
    correlation = models.CharField(max_length=32, blank=True)  # matched/manual/unmatched
    ise_endpoint_mac = models.CharField(max_length=32, blank=True)

    first_seen = models.DateTimeField(null=True, blank=True)
    last_seen = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.mac} ({self.device_type})"


class Snapshot(models.Model):
    """Scheduler-written cache of external (ISE/FMC) data and status. The web
    tier reads ONLY from here (never calls ISE/FMC live at request time); the
    Celery ``snapshot_datasets`` task refreshes it. One row per name, e.g.
    ``dataset:ise-network-devices`` or ``connection_status``."""

    name = models.CharField(max_length=80, unique=True)
    data = models.JSONField(default=dict)
    fetched_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.name} @ {self.fetched_at}"


class SiteCode(models.Model):
    """Editable NAD-hostname -> site mapping (the site-code table). The sync
    resolves each device's site by finding the row whose ``code`` appears in the
    device's NAD hostname; the longest matching code wins. Managed from the
    in-app Config page (dashboard:config_sites)."""

    code = models.CharField(
        max_length=64, unique=True,
        help_text="Substring found in the NAD hostname, e.g. INBLRKOD")
    site = models.CharField(
        max_length=64,
        help_text="Friendly site name shown in the dashboard, e.g. Kodathi")
    active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["site", "code"]

    def __str__(self):
        return f"{self.code} -> {self.site}"
