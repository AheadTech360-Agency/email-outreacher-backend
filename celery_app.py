"""Celery: background work runs in worker processes, and Celery beat schedules it. Redis is the broker.

Run them next to the API (see README):
    celery -A celery_app worker --loglevel=info      (on Windows add --pool=solo)
    celery -A celery_app beat --loglevel=info        (exactly one beat, or checks are scheduled twice)

Beat starts the follow-up check every FOLLOWUP_CHECK_SECONDS (30 minutes by default).
"""

import sys
from pathlib import Path

# The `celery` command puts this folder on the import path only while it loads this module, then takes
# that entry out again. A job imports `main` later, when it first runs (background.py), so a second entry
# is added at the end: Celery removes only its own (the first), and this one stays.
sys.path.append(str(Path(__file__).resolve().parent))

from celery import Celery  # noqa: E402

from config import get_settings  # noqa: E402

settings = get_settings()

app = Celery("coldbrew", broker=settings.broker_url, include=["tasks"])
app.conf.update(
    timezone="UTC",
    task_ignore_result=True,  # results are logged, not stored
    task_acks_late=True,  # a check interrupted by a restart runs again
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    beat_schedule={
        "follow-ups": {
            "task": "coldbrew.check_follow_ups",
            "schedule": float(settings.followup_check_seconds),
            # A check still queued when the next one is due is dropped, so a backlog never piles up.
            "options": {"expires": float(settings.followup_check_seconds)},
        },
        # Coldbrew's own sending: the scheduler (dry run unless OWN_SENDING_LIVE) and the inbox reader.
        "own-send": {
            "task": "coldbrew.own_send",
            "schedule": float(settings.own_sending_tick_seconds),
            "options": {"expires": float(settings.own_sending_tick_seconds)},
        },
        "own-warm": {
            "task": "coldbrew.own_warm",
            "schedule": float(settings.own_sending_tick_seconds),
            "options": {"expires": float(settings.own_sending_tick_seconds)},
        },
        "own-read": {
            "task": "coldbrew.own_read",
            "schedule": float(settings.own_sending_tick_seconds),
            "options": {"expires": float(settings.own_sending_tick_seconds)},
        },        # Apify settles a run's cost a little after it finishes: correct the logged figures (free reads).
        "apify-costs": {
            "task": "coldbrew.settle_apify_costs",
            "schedule": 600.0,
            "options": {"expires": 600.0},
        },
    },
)

if settings.broker_url.startswith("filesystem://"):
    # Local development without Redis (CELERY_BROKER_URL=filesystem://): a folder stands in for the
    # broker, so a separate worker and beat can still talk. Never for production.
    folder = Path(__file__).with_name(".celery-broker")
    folder.mkdir(exist_ok=True)
    app.conf.broker_transport_options = {
        "data_folder_in": str(folder), "data_folder_out": str(folder), "control_folder": str(folder / "control"),
        "store_processed": False,
    }  # fmt: skip
