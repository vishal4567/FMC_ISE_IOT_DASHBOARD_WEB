"""
Record every Celery task execution to TaskRun (for the admin Activity page),
via task signals. Imported from config/celery.py so the worker registers them.
Models are imported lazily inside the handlers to avoid app-registry issues at
import time. All handlers are best-effort and never raise into the task.
"""
from celery.signals import task_failure, task_postrun, task_prerun


@task_prerun.connect
def _prerun(task_id=None, task=None, **kwargs):
    try:
        from dashboard.models import TaskRun
        TaskRun.objects.create(
            task_id=task_id or "", name=getattr(task, "name", "") or "",
            status="started")
    except Exception:
        pass


@task_postrun.connect
def _postrun(task_id=None, task=None, state=None, retval=None, **kwargs):
    try:
        from django.utils import timezone
        from dashboard.models import TaskRun
        now = timezone.now()
        status = ("success" if state == "SUCCESS"
                  else "failure" if state == "FAILURE"
                  else (state or "done").lower())
        detail = "" if retval is None else str(retval)[:500]
        run = (TaskRun.objects.filter(task_id=task_id, finished__isnull=True)
               .order_by("-started").first())
        if run:
            run.finished = now
            run.status = status
            run.runtime_ms = int((now - run.started).total_seconds() * 1000)
            run.detail = detail
            run.save(update_fields=["finished", "status", "runtime_ms", "detail"])
        else:
            TaskRun.objects.create(
                task_id=task_id or "", name=getattr(task, "name", "") or "",
                status=status, finished=now, detail=detail)
        # light retention: keep the most recent 2000 rows
        ids = list(TaskRun.objects.order_by("-started")
                   .values_list("id", flat=True)[2000:2000 + 500])
        if ids:
            TaskRun.objects.filter(id__in=ids).delete()
    except Exception:
        pass


@task_failure.connect
def _failure(task_id=None, exception=None, **kwargs):
    try:
        from dashboard.models import TaskRun
        run = (TaskRun.objects.filter(task_id=task_id)
               .order_by("-started").first())
        if run:
            run.status = "failure"
            run.detail = str(exception)[:500]
            run.save(update_fields=["status", "detail"])
    except Exception:
        pass
