"""Record config changes to the AuditLog (best-effort, never breaks the view)."""


def log(request, action, target="", detail=""):
    from dashboard.models import AuditLog
    try:
        AuditLog.objects.create(
            username=getattr(getattr(request, "user", None), "username", "") or "",
            action=action, target=str(target)[:200], detail=str(detail)[:500])
    except Exception:
        pass
