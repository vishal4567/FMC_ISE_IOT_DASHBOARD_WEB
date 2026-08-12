"""
Map an eNcore / eStreamer JSON event onto the internal event dict that the
analytics + ``SecurityEvent`` model consume.

*** THIS IS THE FILE YOU CUSTOMISE ***
eNcore's exact field names depend on its version and output handler config. The
mapper below reads the common eStreamer record types (intrusion / connection /
file / malware / security-intelligence) with sensible fallbacks. Run
``python manage.py probe_apis`` and capture a few real eNcore records, hand the
output back, and these mappings get tuned to your environment.

Reference: eStreamer Integration Guide - event record types & fields.
"""
from __future__ import annotations

import datetime as _dt

from django.utils import timezone

# eStreamer record-type code -> our event_type label. Codes vary by FMC version;
# eNcore usually also emits a human "type"/"recordType" string we prefer.
RECORD_TYPE_LABELS = {
    "intrusion": "Intrusion",
    "connection": "Connection",
    "file": "File",
    "malware": "Malware",
    "filemalware": "Malware",
    "security_intelligence": "Security Intelligence",
    "securityintelligence": "Security Intelligence",
    "impact": "Intrusion",
}

# FMC intrusion "impact flag" (1=highest) -> severity label. Tune to policy.
IMPACT_SEVERITY = {1: "Critical", 2: "High", 3: "Medium", 4: "Low", 0: "Informational"}

# Clear-text protocols/ports flagged as insecure.
INSECURE_PORTS = {21: "FTP", 23: "Telnet", 80: "HTTP", 69: "TFTP", 161: "SNMP",
                  445: "SMB", 513: "rlogin", 110: "POP3", 143: "IMAP"}


def _first(raw: dict, *keys, default=""):
    for k in keys:
        if k in raw and raw[k] not in (None, ""):
            return raw[k]
    return default


def _event_type(raw: dict) -> str:
    t = str(_first(raw, "event_type", "recordType", "record_type", "type")).lower()
    t = t.replace(" ", "_").replace("-", "_")
    return RECORD_TYPE_LABELS.get(t, "Connection")


def _severity(raw: dict, event_type: str) -> str:
    # explicit severity wins
    sev = _first(raw, "severity", "impactDescription")
    if sev in ("Critical", "High", "Medium", "Low", "Informational"):
        return sev
    impact = _first(raw, "impact", "impactFlag", "impact_flag", default=None)
    try:
        return IMPACT_SEVERITY.get(int(impact), "Medium")
    except (TypeError, ValueError):
        return "Medium" if event_type in ("Intrusion", "Malware") else "Informational"


def _ts(raw):
    ts = _first(raw, "timestamp", "eventSecond", "event_time", default=None)
    if isinstance(ts, (int, float)):
        return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc)
    if isinstance(ts, str):
        try:
            parsed = _dt.datetime.fromisoformat(ts)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=_dt.timezone.utc)
            return parsed
        except ValueError:
            pass
    return timezone.now()


def map_event(raw: dict) -> dict:
    """eNcore/eStreamer JSON record -> internal event dict (pre-ISE-enrichment)."""
    et = _event_type(raw)
    ts = _ts(raw)
    port = _first(raw, "destinationPort", "dstPort", "port", default=None)
    try:
        port = int(port) if port not in (None, "") else None
    except (TypeError, ValueError):
        port = None
    app = _first(raw, "application", "applicationProtocol", "app",
                 default=(INSECURE_PORTS.get(port, "")))
    sent = int(_first(raw, "initiatorBytes", "bytes_sent", "sentBytes", default=0) or 0)
    recv = int(_first(raw, "responderBytes", "bytes_received", "recvBytes", default=0) or 0)
    action = _first(raw, "action", "ruleAction", "aclAction", default="")
    src_zone = _first(raw, "ingressZone", "sourceZone", "src_zone")
    dst_zone = _first(raw, "egressZone", "destinationZone", "dst_zone")

    return {
        "_ts": ts,
        "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"),
        "hour": ts.strftime("%Y-%m-%d %H:00"),
        "event_type": et,
        "severity": _severity(raw, et),
        "impact": int(_first(raw, "impact", "impactFlag", default=0) or 0),
        "device_mac": str(_first(raw, "sourceMac", "srcMac", "device_mac",
                                 "clientMac")).upper(),
        "device_ip": _first(raw, "sourceIp", "srcIp", "initiatorIp", "device_ip"),
        "hostname": _first(raw, "hostname", "clientHost"),
        "source_ip": _first(raw, "sourceIp", "srcIp", "initiatorIp"),
        "dest_ip": _first(raw, "destinationIp", "dstIp", "responderIp"),
        "dest_country": _first(raw, "destinationCountry", "dstCountry", "geolocation"),
        "application": app,
        "protocol": _first(raw, "protocol", "transportProtocol", "ipProtocol"),
        "port": port,
        "insecure_protocol": port in INSECURE_PORTS,
        "bytes_sent": sent,
        "bytes_received": recv,
        "total_bytes": sent + recv,
        "action": _norm_action(action),
        "rule_matched": _first(raw, "ruleName", "accessControlRuleName", "rule"),
        "ips_policy": _first(raw, "ipsPolicy", "intrusionPolicy", "policy"),
        "src_zone": src_zone,
        "dst_zone": dst_zone,
        "firewall": _first(raw, "device", "sensor", "managedDevice", "firewall"),
        "threat_name": _first(raw, "ruleMessage", "message", "threatName",
                              "malwareEventType", "genericMessage"),
        "threat_category": _first(raw, "classification", "category", "classDescription"),
        "classtype": _first(raw, "classtype", "ruleClass"),
        "zone_violation": bool(src_zone and dst_zone and src_zone != dst_zone
                               and _norm_action(action) in ("Blocked", "Would Block")
                               and str(dst_zone).lower() in ("outside", "dmz", "untrust")),
        # device_type / site / in_ise are stamped by the ingester from ISE.
    }


def _norm_action(action: str) -> str:
    a = str(action).strip().lower()
    if a in ("block", "blocked", "deny", "drop"):
        return "Blocked"
    if a in ("would_block", "would block", "wouldblock", "monitor+block"):
        return "Would Block"
    if a in ("allow", "allowed", "trust", "pass"):
        return "Allowed"
    return action or "Allowed"
