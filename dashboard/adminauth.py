"""
Lightweight HTTP Basic auth for the admin Config page - the project has no auth
framework, so this guards just the config views without pulling in
django.contrib.auth/sessions.

Protection is ENFORCED only when ``DASHBOARD_ADMIN_PASSWORD`` is set; if it's
blank the page stays open (as it was before), so a fresh deploy is never locked
out before the password is configured.
"""
from __future__ import annotations

import base64
import hmac
from functools import wraps

from django.conf import settings
from django.http import HttpResponse


def _parse_basic(request):
    header = request.META.get("HTTP_AUTHORIZATION", "")
    if not header.startswith("Basic "):
        return "", ""
    try:
        raw = base64.b64decode(header[6:]).decode("utf-8")
    except Exception:
        return "", ""
    user, _, passwd = raw.partition(":")
    return user, passwd


def admin_required(view):
    """Gate a view behind HTTP Basic auth (admin user + DASHBOARD_ADMIN_PASSWORD)."""
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        expected = settings.ADMIN_PASSWORD
        if not expected:                       # not configured -> open
            return view(request, *args, **kwargs)
        user, passwd = _parse_basic(request)
        if user == settings.ADMIN_USER and hmac.compare_digest(passwd, expected):
            return view(request, *args, **kwargs)
        resp = HttpResponse("Authentication required.", status=401,
                            content_type="text/plain")
        resp["WWW-Authenticate"] = 'Basic realm="IoT Dashboard Config"'
        return resp
    return wrapped
