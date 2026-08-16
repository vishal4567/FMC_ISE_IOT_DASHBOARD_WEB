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
    c = raw.get("@computed") or {}
    s = " ".join(
        str(x) for x in (
            c.get("recordTypeDescription"), c.get("recordTypeCategory"),
            raw.get("eventDescription"), raw.get("event_type"),
            raw.get("recordType"), raw.get("type"),
        ) if x
    ).lower()
    if "intrusion" in s:
        return "Intrusion"
    if "malware" in s:
        return "Malware"
    if "file" in s:
        return "File"
    if "security intelligence" in s or "security_intelligence" in s:
        return "Security Intelligence"
    return "Connection"


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


# Placeholder / unset values eNcore emits for absent IPv6/MAC.
_NULL_IPS = {"", "::", "0.0.0.0", "::0"}
_NULL_MAC = {"", "00:00:00:00:00:00", "NONE"}


def candidate_ids(raw: dict):
    """Cheap (MAC, [IPs]) extraction for the IoT pre-filter, so the ingester can
    drop non-IoT flows WITHOUT running the full map_event() on them. At FMC's
    full event rate (~thousands/sec) the vast majority of records are non-IoT
    traffic; this keeps the reject path to a couple of dict lookups.

    eNcore connection events name the endpoints initiator/responder; other record
    types may use source/destination — we check both. IPs live at top level (not
    under @computed)."""
    mac = str(raw.get("macAddress") or raw.get("sourceMac") or "").upper()
    if mac in _NULL_MAC:
        mac = ""
    ips = []
    for k in ("initiatorIpAddress", "responderIpAddress", "sourceIp",
              "destinationIp", "device_ip"):
        v = raw.get(k)
        if v and str(v) not in _NULL_IPS:
            ips.append(str(v))
    return mac, ips


def map_event(raw: dict) -> dict:
    """eNcore/eStreamer JSON record -> internal event dict (pre-ISE-enrichment).

    eNcore keeps friendly enum strings (action, protocol, zones, app, event type)
    under a nested ``@computed`` object; the raw endpoints/ports/bytes are at top
    level. We merge the two into one flat lookup (``@computed`` wins on conflicts)
    so a single set of field names resolves both."""
    m = raw
    computed = raw.get("@computed")
    if computed:
        m = {**raw, **computed}  # friendly enums override raw numeric codes

    et = _event_type(raw)
    ts = _ts(m)
    # responderPort is the SERVICE port (e.g. 53=DNS); initiatorPort is ephemeral.
    port = _first(m, "responderPort", "destinationPort", "dstPort", "port",
                  default=None)
    try:
        port = int(port) if port not in (None, "") else None
    except (TypeError, ValueError):
        port = None
    app = _first(m, "applicationProtocol", "clientApplication", "webApplication",
                 "application", "app", default=(INSECURE_PORTS.get(port, "")))
    sent = int(_first(m, "initiatorTransmittedBytes", "initiatorBytes",
                      "bytes_sent", "sentBytes", default=0) or 0)
    recv = int(_first(m, "responderTransmittedBytes", "responderBytes",
                      "bytes_received", "recvBytes", default=0) or 0)
    action = _first(m, "firewallRuleAction", "action", "ruleAction", "aclAction",
                    default="")
    src_zone = _first(m, "ingressSecurityZone", "ingressZone", "sourceZone", "src_zone")
    dst_zone = _first(m, "egressSecurityZone", "egressZone", "destinationZone", "dst_zone")

    mac = str(_first(m, "macAddress", "sourceMac", "srcMac", "device_mac",
                     "clientMac")).upper()
    if mac in _NULL_MAC:
        mac = ""
    src_ip = _first(m, "initiatorIpAddress", "sourceIp", "srcIp", "device_ip")
    dst_ip = _first(m, "responderIpAddress", "destinationIp", "dstIp")

    return {
        "_ts": ts,
        "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"),
        "hour": ts.strftime("%Y-%m-%d %H:00"),
        "event_type": et,
        "severity": _severity(m, et),
        "impact": int(_first(m, "impact", "impactFlag", default=0) or 0),
        "device_mac": mac,
        "device_ip": src_ip,
        "hostname": _first(m, "hostname", "clientHost"),
        "source_ip": src_ip,
        "dest_ip": dst_ip,
        "dest_country": _first(m, "destinationIpCountry", "destinationCountry",
                               "dstCountry", "geolocation"),
        "application": app,
        "protocol": _first(m, "transportProtocol", "protocol", "ipProtocol"),
        "port": port,
        "insecure_protocol": port in INSECURE_PORTS,
        "bytes_sent": sent,
        "bytes_received": recv,
        "total_bytes": sent + recv,
        "action": _norm_action(action),
        "rule_matched": _first(m, "firewallRuleReason", "ruleName",
                               "accessControlRuleName", "rule"),
        "ips_policy": _first(m, "ipsPolicy", "intrusionPolicy", "policy"),
        "src_zone": src_zone,
        "dst_zone": dst_zone,
        "firewall": _first(m, "sensor", "device", "managedDevice", "firewall"),
        "threat_name": _first(m, "ruleMessage", "message", "threatName",
                              "malwareEventType", "genericMessage"),
        "threat_category": _first(m, "classification", "category", "classDescription"),
        "classtype": _first(m, "classtype", "ruleClass"),
        "zone_violation": bool(src_zone and dst_zone and src_zone != dst_zone
                               and _norm_action(action) in ("Blocked", "Would Block")
                               and str(dst_zone).lower() in (
                                   "outside", "dmz", "untrust", "iot-outside")),
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
