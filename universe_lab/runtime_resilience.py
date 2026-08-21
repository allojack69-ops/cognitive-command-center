import threading
import time

from db import init_db
from .models import init_lab_models

_bootstrap_started = False
_bootstrap_lock = threading.Lock()


def _bootstrap_schema_worker():
    for attempt in range(1, 4):
        try:
            init_db()
            init_lab_models()
            print(
                f"[DB-BOOTSTRAP] schema ready on attempt {attempt}",
                flush=True,
            )
            return
        except Exception as exc:
            print(
                f"[DB-BOOTSTRAP] attempt {attempt}/3 failed: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            if attempt < 3:
                time.sleep(min(8, attempt * 2))

    print(
        "[DB-BOOTSTRAP] DB unavailable; web and Observer edge remain online.",
        flush=True,
    )


def start_background_schema_bootstrap():
    global _bootstrap_started

    with _bootstrap_lock:
        if _bootstrap_started:
            return
        _bootstrap_started = True

    threading.Thread(
        target=_bootstrap_schema_worker,
        name="universe-db-bootstrap",
        daemon=True,
    ).start()
