"""
Azure AD / Entra ID single sign-on (OpenID Connect authorization-code flow via
MSAL). Two views:

  azure_login    -> builds the auth-code flow, stashes it in the session, and
                    redirects the browser to Microsoft's sign-in page.
  azure_callback -> Microsoft redirects back here with a code; we exchange it for
                    an ID token, map the claims to a Django user (creating one if
                    needed), set the admin/viewer role, and log them in.

Local username/password login stays available; SSO is additive. Requires the
`msal` package and AZURE_AD settings to be configured.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth import get_user_model, login
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect
from django.urls import reverse

log = logging.getLogger("dashboard")
_SCOPES: list[str] = []  # we only need the ID token (OIDC); no Graph scopes


def _cfg():
    return settings.AZURE_AD


def _authority():
    return f"https://login.microsoftonline.com/{_cfg()['TENANT_ID']}"


def _redirect_uri(request):
    base = (_cfg().get("REDIRECT_BASE") or "").rstrip("/")
    path = reverse("dashboard:azure_callback")
    return f"{base}{path}" if base else request.build_absolute_uri(path)


def _msal_app():
    import msal
    c = _cfg()
    return msal.ConfidentialClientApplication(
        client_id=c["CLIENT_ID"], client_credential=c["CLIENT_SECRET"],
        authority=_authority())


def _enabled():
    c = _cfg()
    return bool(c.get("ENABLED") and c.get("CLIENT_ID")
                and c.get("CLIENT_SECRET") and c.get("TENANT_ID"))


def azure_login(request):
    if not _enabled():
        return HttpResponseBadRequest("Azure AD SSO is not configured.")
    try:
        flow = _msal_app().initiate_auth_code_flow(
            _SCOPES, redirect_uri=_redirect_uri(request))
    except Exception as exc:  # msal not installed / bad config
        log.error("azure_login init failed: %s", exc)
        return HttpResponseBadRequest(f"SSO unavailable: {exc}")
    request.session["az_flow"] = flow
    # remember where to go after login
    request.session["az_next"] = request.GET.get("next") or reverse("dashboard:index")
    return redirect(flow["auth_uri"])


def azure_callback(request):
    if not _enabled():
        return HttpResponseBadRequest("Azure AD SSO is not configured.")
    flow = request.session.pop("az_flow", None)
    if not flow:
        return redirect("dashboard:login")
    try:
        result = _msal_app().acquire_token_by_auth_code_flow(
            flow, dict(request.GET.items()))
    except Exception as exc:
        log.error("azure token exchange failed: %s", exc)
        return _login_error(request, "Sign-in failed. Please try again.")

    if "error" in result:
        log.error("azure auth error: %s / %s", result.get("error"),
                  result.get("error_description"))
        return _login_error(request, result.get("error_description")
                            or "Microsoft sign-in was denied.")

    claims = result.get("id_token_claims", {}) or {}
    email = (claims.get("preferred_username") or claims.get("email")
             or claims.get("upn") or "").strip()
    if not email:
        return _login_error(request, "No username returned by Microsoft.")

    c = _cfg()
    domain = (c.get("ALLOWED_DOMAIN") or "").strip().lower()
    if domain and not email.lower().endswith("@" + domain):
        return _login_error(request, f"Only @{domain} accounts may sign in.")

    User = get_user_model()
    username = email.lower()
    user = User.objects.filter(username=username).first()
    if user is None:
        user = User.objects.filter(email__iexact=email).first()
    if user is None:
        if not c.get("AUTO_CREATE", True):
            return _login_error(request, "Your account is not provisioned. "
                                "Ask an administrator to add you.")
        user = User(username=username, email=email)
        user.set_unusable_password()   # SSO-only account, no local password

    # name from claims
    name = (claims.get("name") or "").strip()
    if name and not user.first_name:
        user.first_name = name[:30]

    # role mapping: admin group membership (or app role) -> is_staff
    admin_group = (c.get("ADMIN_GROUP") or "").strip()
    if admin_group:
        groups = claims.get("groups") or []
        roles = claims.get("roles") or []
        user.is_staff = admin_group in groups or admin_group in roles
    # (if no ADMIN_GROUP configured, leave existing is_staff; new users = viewer)

    user.is_active = True
    user.save()

    login(request, user)
    try:
        from dashboard import audit
        audit.log(request, "user.sso_login", username,
                  "admin" if user.is_staff else "viewer")
    except Exception:
        pass
    return redirect(request.session.pop("az_next", None)
                    or reverse("dashboard:index"))


def _login_error(request, message):
    from urllib.parse import urlencode
    return redirect(reverse("dashboard:login") + "?"
                    + urlencode({"sso_error": message}))
