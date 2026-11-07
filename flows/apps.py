import os
import sys
import threading

from django.apps import AppConfig


class FlowsConfig(AppConfig):
    name = 'flows'

    def ready(self):
        from . import history  # noqa: F401  (field-level history signals)
        start_scheduler()


_started = False


def start_scheduler():
    """Run tick() every N seconds in a daemon thread — only in the serving process (not tests, migrations, shells)."""
    global _started
    from django.conf import settings

    if _started or not settings.PAPERLANE_SCHEDULER:
        return
    argv = " ".join(sys.argv)
    serving = ("runserver" in argv and os.environ.get("RUN_MAIN") == "true") or "gunicorn" in argv or "uvicorn" in argv \
        or os.environ.get("PAPERLANE_SCHEDULER_FORCE") == "1"
    if not serving:
        return
    _started = True
    threading.Thread(target=_loop, args=(settings.PAPERLANE_SCHEDULER_SECONDS,), name="paperlane-scheduler", daemon=True).start()


def _loop(seconds):
    import time

    from flows.jobs import run_forever

    time.sleep(5)
    run_forever(seconds, 3)
