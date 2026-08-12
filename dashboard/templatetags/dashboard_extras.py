"""Custom template filters for the dashboard."""
import json

from django import template

register = template.Library()


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
