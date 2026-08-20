"""
DB-backed NAD-hostname -> site matcher, built from the editable ``SiteCode``
table (managed on the Config page). Falls back to the built-in code list in
integrations/location_map when the table is empty.
"""
from __future__ import annotations


def db_site_matcher():
    """[(normalised_code, site_name)] from the active SiteCode rows, ordered
    longest-code-first (via location_map.build_matcher)."""
    from integrations.location_map import HOSTNAME_SITE_MAP, build_matcher
    from dashboard.models import SiteCode

    rows = list(SiteCode.objects.filter(active=True).values_list("site", "code"))
    if not rows:
        return build_matcher(HOSTNAME_SITE_MAP)  # built-in fallback

    mapping: dict[str, list] = {}
    for site, code in rows:
        mapping.setdefault(site, []).append(code)
    return build_matcher(list(mapping.items()))
