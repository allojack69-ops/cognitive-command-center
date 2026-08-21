import threading
import time

from flask import jsonify
from werkzeug.exceptions import HTTPException

from db import init_db
from .models import init_lab_models
from .observer_edge import bp as observer_edge_bp


_bootstrap_started = False
_bootstrap_lock = threading.Lock()


def _edge_json_error(exc):
    """Never return an HTML error page to Observer edge clients."""
    if isinstance(exc, HTTPException):
        return jsonify({
            "ok": False,
            "error": exc.name.lower().replace(" ", "_"),
            "detail": exc.description,
            "http_status": exc.code,
        }), exc.code

    return jsonify({
        "ok": False,
        "error": "observer_edge_unavailable",
        "detail": f"{type(exc).__name__}: {exc}"[:500],
        "http_status": 503,
    }), 503


observer_edge_bp.register_error_handler(Exception, _edge_json_error)


def _bootstrap_schema_worker():
    """Create missing DB tables after web boot, with bounded retries."""
    for attempt in range(1, 6):
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
                f"[DB-BOOTSTRAP] attempt {attempt}/5 failed: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            if attempt < 5:
                time.sleep(min(10, attempt * 2))

    print(
        "[DB-BOOTSTRAP] schema still unavailable; web stays online and "
        "Observer edge APIs return structured JSON errors.",
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
