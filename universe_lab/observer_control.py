import json
import os
import secrets
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, abort, jsonify, render_template, request, session
from sqlalchemy import select

from db import SessionLocal
from .models import LabExternalResult

bp = Blueprint(
    "observer_control",
    __name__,
    url_prefix="/observer",
    template_folder="templates",
)

BASE = Path(__file__).resolve().parent
STATIC_SNAPSHOT = BASE / "data" / "observer_control_snapshot.json"

PID_FILE = Path(os.getenv("OBSERVER_PID_FILE", "/tmp/universe_observer.pid"))
META_FILE = Path(os.getenv("OBSERVER_META_FILE", "/tmp/universe_observer_meta.json"))
LOG_FILE = Path(os.getenv("OBSERVER_LOG_FILE", "/tmp/universe_observer.log"))


def _require_admin():
    if not session.get("admin"):
        abort(403)


def _csrf_token():
    token = session.get("observer_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["observer_csrf"] = token
    return token


def _check_csrf():
    supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
    expected = session.get("observer_csrf")
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        abort(403)


def _runtime_dir():
    raw = os.getenv("OBSERVER_RUNTIME_DIR", "observer_runtime")
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def _configured_command():
    raw_json = os.getenv("OBSERVER_COMMAND_JSON", "").strip()
    if raw_json:
        try:
            cmd = json.loads(raw_json)
            if isinstance(cmd, list) and cmd and all(isinstance(x, str) and x for x in cmd):
                return cmd
        except Exception:
            return None

    runtime = _runtime_dir()
    for name in ("run_observer.py", "main.py", "observer.py", "trader.py", "app.py"):
        candidate = runtime / name
        if candidate.exists():
            return [sys.executable, name]
    return None


def _read_pid():
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _pid_alive(pid):
    if not pid or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _process_info():
    pid = _read_pid()
    alive = _pid_alive(pid)
    meta = {}
    try:
        meta = json.loads(META_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass

    if pid and not alive:
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    started_at = meta.get("started_at")
    uptime_sec = None
    if alive and started_at:
        try:
            dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            uptime_sec = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
        except Exception:
            pass

    command = _configured_command()
    runtime = _runtime_dir()
    return {
        "pid": pid if alive else None,
        "alive": alive,
        "started_at": started_at if alive else None,
        "uptime_sec": uptime_sec,
        "runtime_dir": str(runtime),
        "runtime_dir_exists": runtime.exists(),
        "command": command,
        "engine_attached": bool(command and runtime.exists()),
    }



def _safe_observer_env():
    env = os.environ.copy()

    # Phase 1 safety lock: public market observation + paper simulation only.
    # The Observer child receives no exchange credentials.
    env["MOR_EXECUTION_MODE"] = "PAPER"
    env["MOR_TESTNET_RELAX_GATES"] = "0"
    env["MOR_TESTNET_GEOMETRY_ACTIONS"] = "0"
    env["MOR_TESTNET_ACTION_ARBITRATION"] = "0"
    env["MOR_PREFLIGHT_ORDER_TEST"] = "0"
    env["MOR_LIVE_ARM"] = ""
    env.pop("BINANCE_API_KEY", None)
    env.pop("BINANCE_API_SECRET", None)

    # Android download paths don't exist on Render.
    env["MOR_EXPORT_DOWNLOAD_PATH"] = "storage/MOR_latest_export.json"
    env["MOR_EXPORT_FULL_DOWNLOAD_PATH"] = "storage/MOR_export_full.json"
    env["MOR_OBSERVER_STATUS_FILE"] = "storage/observer_status.json"
    return env


def _start_process():
    info = _process_info()
    if info["alive"]:
        return True, "Observer is already running."

    runtime = _runtime_dir()
    command = _configured_command()
    if not runtime.exists():
        return False, f"Observer runtime directory not found: {runtime}"
    if not command:
        return False, (
            "Observer engine entrypoint is not attached. Expected "
            "run_observer.py/main.py/observer.py/trader.py/app.py or OBSERVER_COMMAND_JSON."
        )

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log_handle = open(LOG_FILE, "ab", buffering=0)

    try:
        proc = subprocess.Popen(
            command,
            cwd=str(runtime),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=_safe_observer_env(),
        )
    except Exception as exc:
        log_handle.close()
        return False, f"Failed to start Observer: {type(exc).__name__}: {exc}"

    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    META_FILE.write_text(
        json.dumps(
            {
                "pid": proc.pid,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "command": command,
                "runtime_dir": str(runtime),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return True, f"Observer started (PID {proc.pid})."


def _stop_process():
    pid = _read_pid()
    if not _pid_alive(pid):
        PID_FILE.unlink(missing_ok=True)
        return True, "Observer is already stopped."

    try:
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGINT)
        except Exception:
            os.kill(pid, signal.SIGINT)

        deadline = time.time() + 12
        while time.time() < deadline:
            if not _pid_alive(pid):
                break
            time.sleep(0.2)

        if _pid_alive(pid):
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGKILL)
            except Exception:
                os.kill(pid, signal.SIGKILL)

        PID_FILE.unlink(missing_ok=True)
        return True, f"Observer stopped (PID {pid})."
    except Exception as exc:
        return False, f"Failed to stop Observer: {type(exc).__name__}: {exc}"


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _snapshot_from_runtime():
    runtime = _runtime_dir()
    candidates = [
        runtime / "storage" / "observer_status.json",
        runtime / "storage" / "mor_analysis_export_latest.json",
        runtime / "mor_analysis_export_latest.json",
        runtime / "storage" / "MOR_latest_export.json",
        runtime / "MOR_latest_export.json",
    ]
    for path in candidates:
        if path.exists():
            payload = _read_json(path)
            if isinstance(payload, dict):
                return payload, f"runtime:{path.name}"
    return None, None


def _snapshot_from_database():
    try:
        with SessionLocal() as db:
            row = db.scalar(
                select(LabExternalResult)
                .where(LabExternalResult.result_type == "MOR_OBSERVER")
                .order_by(LabExternalResult.created_at.desc())
                .limit(1)
            )
            if row:
                payload = row.payload()
                if isinstance(payload, dict):
                    return payload, "database:latest_import"
    except Exception:
        pass
    return None, None


def _load_snapshot():
    payload, source = _snapshot_from_runtime()
    if payload:
        return payload, source

    payload, source = _snapshot_from_database()
    if payload:
        return payload, source

    payload = _read_json(STATIC_SNAPSHOT)
    if isinstance(payload, dict):
        return payload, "bundled_snapshot"

    return {}, "none"


def _tail_log(limit=80):
    try:
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-limit:]
    except Exception:
        return []


def _bool_gate(value):
    if isinstance(value, dict):
        if "allowed" in value:
            return bool(value.get("allowed"))
        if "ready" in value:
            return bool(value.get("ready"))
    return False


def _module_row(name, value):
    if not isinstance(value, dict):
        return {"name": name, "ready": False, "status": "NO DATA", "score": None, "action": None}
    status = (
        value.get("status")
        or value.get("verdict")
        or value.get("mode")
        or ("READY" if value.get("ready") else "OBSERVE")
    )
    score = value.get("score")
    if score is None:
        score = value.get("reliability")
    action = (
        value.get("action")
        or value.get("final_action")
        or value.get("selected_action")
        or value.get("execution_action")
    )
    return {
        "name": name,
        "ready": bool(value.get("ready", True)),
        "status": str(status),
        "score": score,
        "action": action,
    }


def _summarize(snapshot, source):
    current = snapshot.get("current_state") or {}
    derived = snapshot.get("derived") or {}
    counts = snapshot.get("runtime_counts") or {}
    metrics = snapshot.get("metrics") or {}
    recent = snapshot.get("recent") or {}

    state_features = current.get("state_features") or {}
    trade_gate = current.get("tradeability_gate") or {}
    edge_gate = current.get("edge_gate") or {}
    execution = current.get("execution_readiness") or {}
    gsr1 = execution.get("gsr1") or derived.get("GSR1") or {}
    eh1 = execution.get("eh1") or derived.get("EH1") or {}

    states = recent.get("states") or []
    series = []
    for item in states[-60:]:
        if not isinstance(item, dict):
            continue
        price = item.get("price")
        if not isinstance(price, (int, float)):
            continue
        series.append(
            {
                "state_id": item.get("state_id"),
                "time": item.get("market_time") or item.get("time"),
                "price": float(price),
                "trend_pct": item.get("trend_pct"),
                "volatility_pct": item.get("volatility_pct"),
                "action": item.get("action"),
            }
        )

    tradeability = []
    for h in (5, 15, 30, 60):
        score = state_features.get(f"tradeability_score_h{h}")
        prob = state_features.get(f"p_tradeable_h{h}")
        motion = state_features.get(f"motion_budget_h{h}_pct")
        if score is not None or prob is not None or motion is not None:
            tradeability.append(
                {
                    "horizon": h,
                    "score": score,
                    "probability": prob,
                    "motion_budget_pct": motion,
                }
            )

    modules = []
    preferred_order = [
        "PFL1", "EFS1", "BPM1", "EH1", "GSR1", "CGE1",
        "AAL1", "ERL1", "GDX1", "SCR1", "GAP1", "GRC1", "RES1",
    ]
    for name in preferred_order:
        if name in derived:
            modules.append(_module_row(name, derived.get(name)))

    pipeline = [
        {
            "name": "Strategy",
            "ok": current.get("action") not in (None, "HOLD", "NONE"),
            "value": current.get("action") or "HOLD",
            "detail": current.get("chosen_strategy") or "NONE",
        },
        {
            "name": "Tradeability",
            "ok": _bool_gate(trade_gate),
            "value": "PASS" if _bool_gate(trade_gate) else "BLOCK",
            "detail": trade_gate.get("reason") or "NO DATA",
        },
        {
            "name": "Edge",
            "ok": _bool_gate(edge_gate),
            "value": "VALID" if _bool_gate(edge_gate) else "UNVALIDATED",
            "detail": edge_gate.get("reason") or edge_gate.get("source") or "NO DATA",
        },
        {
            "name": "Geometry",
            "ok": bool(gsr1.get("strict_safe") or gsr1.get("testnet_safe")),
            "value": gsr1.get("verdict") or "NO DATA",
            "detail": gsr1.get("geometry_preferred_action") or "NONE",
        },
        {
            "name": "Execution",
            "ok": bool(execution.get("strict_ready") or execution.get("testnet_ready")),
            "value": execution.get("action") or execution.get("execution_action") or "HOLD",
            "detail": f"score={execution.get('score', 0)}",
        },
    ]

    residual = metrics.get("model_residual_tracker") or derived.get("RES1") or {}

    return {
        "source": source,
        "export_version": snapshot.get("export_version"),
        "trader_version": snapshot.get("trader_version"),
        "generated_at": snapshot.get("generated_at"),
        "symbol": snapshot.get("symbol") or current.get("symbol"),
        "interval": snapshot.get("interval"),
        "state": {
            "state_id": current.get("state_id"),
            "market_time": current.get("market_time") or current.get("time"),
            "price": current.get("price"),
            "regime": current.get("regime"),
            "trend_pct": current.get("trend_pct"),
            "volatility_pct": current.get("volatility_pct"),
            "strategy": current.get("chosen_strategy"),
            "action": current.get("action"),
            "p_success": current.get("p_success"),
            "horizon": current.get("prediction_horizon"),
        },
        "counts": counts,
        "pipeline": pipeline,
        "tradeability": tradeability,
        "modules": modules,
        "series": series,
        "residual": {
            "comparisons": residual.get("comparisons", 0),
            "total_error": residual.get("ema_total_error"),
            "direction_error": residual.get("ema_direction_error"),
            "reliability": residual.get("reliability"),
        },
        "execution": {
            "mode": execution.get("mode"),
            "score": execution.get("score"),
            "strict_ready": execution.get("strict_ready"),
            "testnet_ready": execution.get("testnet_ready"),
            "live_armed": execution.get("live_armed", False),
            "blockers": execution.get("blockers") or [],
        },
        "eh1": {
            "selected_action": eh1.get("selected_action"),
            "selected_horizon": eh1.get("selected_horizon"),
            "selected_score": eh1.get("selected_score"),
            "status": eh1.get("status"),
            "direction_conflict": eh1.get("direction_conflict"),
        },
    }


def _status_payload():
    process = _process_info()
    snapshot, source = _load_snapshot()
    summary = _summarize(snapshot, source)
    return {
        "ok": True,
        "process": process,
        "observer": summary,
        "logs": _tail_log(),
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


@bp.get("/control")
def control():
    _require_admin()
    return render_template(
        "observer_control.html",
        csrf_token=_csrf_token(),
        initial=_status_payload(),
    )


@bp.get("/api/status")
def status_api():
    _require_admin()
    return jsonify(_status_payload())


@bp.post("/api/start")
def start_api():
    _require_admin()
    _check_csrf()
    ok, message = _start_process()
    payload = _status_payload()
    payload["ok"] = ok
    payload["message"] = message
    return jsonify(payload), (200 if ok else 409)


@bp.post("/api/stop")
def stop_api():
    _require_admin()
    _check_csrf()
    ok, message = _stop_process()
    payload = _status_payload()
    payload["ok"] = ok
    payload["message"] = message
    return jsonify(payload), (200 if ok else 500)


@bp.post("/api/restart")
def restart_api():
    _require_admin()
    _check_csrf()
    ok_stop, msg_stop = _stop_process()
    if not ok_stop:
        payload = _status_payload()
        payload["ok"] = False
        payload["message"] = msg_stop
        return jsonify(payload), 500

    time.sleep(0.4)
    ok_start, msg_start = _start_process()
    payload = _status_payload()
    payload["ok"] = ok_start
    payload["message"] = f"{msg_stop} {msg_start}"
    return jsonify(payload), (200 if ok_start else 409)
