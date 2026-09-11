"""
Require an authenticated session for every page, except an allowlist (the login
page, static assets, and a health check). Unauthenticated requests are redirected
to the login page with ?next= preserved.
"""
from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse


class LoginRequiredMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.user.is_authenticated and not self._exempt(request):
            login_url = reverse(settings.LOGIN_URL)
            return redirect(f"{login_url}?next={request.path}")
        return self.get_response(request)

    @staticmethod
    def _exempt(request):
        path = request.path
        static_url = settings.STATIC_URL or "/static/"
        if path.startswith(static_url) or path in ("/healthz", "/healthz/"):
            return True
        # the login/logout endpoints themselves
        for name in ("dashboard:login", "dashboard:logout"):
            try:
                if path == reverse(name):
                    return True
            except Exception:
                pass
        return False
