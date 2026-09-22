"""Template context processors."""


def device_search(request):
    """Expose device identifiers (MAC / hostname / IP) for the navbar search
    autocomplete. Cheap - the underlying event feed is cached. Never raises."""
    try:
        from dashboard import analytics

        options = []
        for d in analytics.device_inventory():
            options.extend([d["mac"], d["hostname"], str(d["ip"])])
        return {"nav_device_options": options}
    except Exception:
        return {"nav_device_options": []}


def sso_flags(request):
    """Expose whether Azure AD SSO is configured, for the login page button."""
    from django.conf import settings
    az = getattr(settings, "AZURE_AD", {})
    return {"azure_sso_enabled": bool(az.get("ENABLED") and az.get("CLIENT_ID"))}
