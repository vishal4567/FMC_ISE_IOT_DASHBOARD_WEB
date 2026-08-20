"""
Site/location mapping from a NAD (network access device) HOSTNAME.

The ISE RADIUS ``location`` hierarchy is unreliable in this deployment (most
devices resolve to the bare "All Locations" root), but every NAD hostname
carries the site code - e.g. ``INBLRKOD-ACC-SW01`` -> Kodathi. This maps those
codes to friendly site names.

Matching is on a NORMALISED key (upper-cased, non-alphanumerics stripped) so
``INBLR-SJP1`` and ``INBLRSJP1`` both match, and the MOST SPECIFIC (longest)
code wins to avoid a short code shadowing a longer one.
"""
from __future__ import annotations

# (site name, [hostname codes that identify it]). Straight from the site-code
# table. Multiple codes per site are all accepted.
HOSTNAME_SITE_MAP = [
    ("Candor, Haryana",    ["INNCR-CNDR"]),
    ("Gurugram",           ["INNCR-GDC"]),
    ("Noida",              ["INGNDC", "INDELGNDC"]),
    ("Bhubaneswar",        ["INBHU"]),
    ("Kolkata",            ["INKDC"]),
    ("Sarjapur Bengaluru", ["INBLR-SJP1", "INBLRSJP1"]),
    ("Mysore",             ["INBLRMYS", "IN-MYSORE", "INMYS", "MYS"]),
    ("Airoli Mumbai",      ["IN-MUMAIR", "AIROLI", "IN-AIROLI", "INBOMAIRL"]),
    ("Pune",               ["INPNQPDC2", "INPNQPDC1", "INPDC1", "PDC"]),
    ("Chennai",            ["INCHNCDC5"]),
    ("Ahmedabad",          ["INAHMGC"]),
    ("Jaipur",             ["INJAI1"]),
    ("Manjakudi",          ["INDMAA"]),
    ("Kodathi",            ["INBLRKOD"]),
    ("Manyata",            ["INBLRMTP"]),
    ("Cochi",              ["INCOC"]),
    ("Hyderabad",          ["INHYDHTECH", "INHYDHYD", "INHYDGPY"]),
    ("Coimbatore",         ["INCOI"]),
]


def _norm(s) -> str:
    return "".join(ch for ch in str(s or "").upper() if ch.isalnum())


def build_matcher(mapping=None):
    """[(normalised_code, site_name)] ordered longest-code-first so the most
    specific code wins (e.g. INBLRSJP1 is tried before a shared 3-letter code)."""
    mapping = HOSTNAME_SITE_MAP if mapping is None else mapping
    pairs = []
    for name, codes in mapping:
        for c in codes:
            key = _norm(c)
            if key:
                pairs.append((key, name))
    pairs.sort(key=lambda x: len(x[0]), reverse=True)
    return pairs


def site_from_hostname(hostname, matcher=None) -> str:
    """Return the site name whose code appears in the NAD hostname, else ''."""
    h = _norm(hostname)
    if not h:
        return ""
    for code, name in (matcher if matcher is not None else build_matcher()):
        if code in h:
            return name
    return ""
