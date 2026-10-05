"""Custom template filters for the dashboard."""
import json

from django import template

register = template.Library()


@register.filter(name="comma")
def comma(value):
    """Thousands separators: 1962065 -> '1,962,065'. Non-numbers pass through."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return value
    return f"{int(f):,}" if f == int(f) else f"{f:,.1f}"


@register.filter(name="compact")
def compact(value):
    """Compact large numbers for tiles: 1962065 -> '1.96M', 662737 -> '662.7K',
    19787 -> '19,787'. Keeps small numbers exact (with commas)."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return value
    a = abs(n)
    if a >= 1_000_000_000:
        return f"{n / 1e9:.2f}B"
    if a >= 1_000_000:
        return f"{n / 1e6:.2f}M"
    if a >= 100_000:
        return f"{n / 1e3:.0f}K"
    return f"{int(n):,}" if n == int(n) else f"{n:,.1f}"


@register.filter(name="dictkey")
def dictkey(mapping, key):
    """Look up ``mapping[key]`` in templates where the key is a variable.

    Dicts/lists are rendered as compact JSON so nested attributes (e.g. ISE
    custom attributes) stay readable in a table cell.
    """
    if not isinstance(mapping, dict):
        return ""
    value = mapping.get(key, "")
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value
