"""
Role gate for admin-only views. With the session auth framework, an ADMIN is a
staff user (``is_staff``); any other authenticated user is view-only. The
LoginRequiredMiddleware guarantees the user is already authenticated here, so we
only need to check the role.
"""
from __future__ import annotations

from functools import wraps

from django.http import HttpResponseForbidden


def is_admin(user) -> bool:
    return bool(user and user.is_authenticated and user.is_staff)


def admin_required(view):
    """Allow only admin (staff) users; view-only users get 403."""
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if is_admin(request.user):
            return view(request, *args, **kwargs)
        return HttpResponseForbidden(
            "<h3>403 — Admin access required</h3>"
            "<p>Your account is view-only. Ask an administrator for access.</p>"
            "<p><a href='/'>Back to dashboard</a></p>")
    return wrapped
