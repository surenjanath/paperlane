from django.db.models import Prefetch


def RUNS_PREFETCH():  # noqa: N802 — used like a constant in prefetch_related()
    """Runs with their group and the teammate who took them, for document rows (no per-row queries)."""
    from .models import StepRun

    return Prefetch("runs", queryset=StepRun.objects.select_related("assignee_group", "claimed_by"))
