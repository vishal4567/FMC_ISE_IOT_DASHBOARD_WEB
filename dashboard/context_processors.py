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
