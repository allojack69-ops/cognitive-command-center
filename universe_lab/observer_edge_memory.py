import hashlib
import json
import os
import secrets
import threading
from datetime import datetime, timezone
from uuid import uuid4

from flask import Blueprint, abort, jsonify, request, session

bp = Blueprint(
    "universe_observer_edge_memory",
    __name__,
    url_prefix="/observer/edge",
)

AGENT_VERSION = "1.1"
DEFAULT_AGENT_TOKEN_HASH = "533dfddcdbbe010162fdb2d0f4ff6bdb959bd8880fd5fa6c6b88d2581db3063d"
AGENT_TOKEN_HASH = os.getenv(
    "OBSERVER_AGENT_TOKEN_HASH",
    DEFAULT_AGENT_TOKEN_HASH,
).strip()

_lock = threading.RLock()
_next_event_id = 1
_memory = {
    "heartbeat": {},
    "status": {},
    "latest_trade": None,
    "checkpoint": {},
    "commands": [],
}


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _json_error(name, status=500, detail=None):
    payload = {
        "ok": False,
        "error": name,
        "http_status": int(status),
        "transport": "memory-v2",
    }
    if detail:
        payload["detail"] = str(detail)[:500]
    return jsonify(payload), int(status)


def _clone(value):
    try:
        return json.loads(json.dumps(value, ensure_ascii=False))
    except Exception:
        return value


def _require_admin():
    if not session.get("admin"):
        abort(403)


def _check_csrf():
    expected = session.get("observer_csrf")
    supplied = (
        request.headers.get("X-CSRF-Token")
        or request.form.get("csrf_token")
    )
    if (
        not expected
        or not supplied
        or not secrets.compare_digest(str(expected), str(supplied))
    ):
        abort(403)


def _agent_authorized():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    token = auth[7:].strip()
    if not token or not AGENT_TOKEN_HASH:
        return False
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return secrets.compare_digest(digest, AGENT_TOKEN_HASH)


def _parse_time(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _snapshot():
    with _lock:
        heartbeat = _clone(_memory["heartbeat"] or {})
        status = _clone(_memory["status"] or {})
        latest_trade = _clone(_memory["latest_trade"])
        checkpoint = _clone(_memory["checkpoint"] or {})
        commands = _clone(_memory["commands"] or [])

    received = (
        _parse_time(heartbeat.get("received_at"))
        or _parse_time(heartbeat.get("time"))
    )

    age = None
    online = False
    if received is not None:
        age = max(
            0.0,
            (datetime.now(timezone.utc) - received).total_seconds(),
        )
        online = age <= 25.0

    pending = [
        row
        for row in commands
        if isinstance(row, dict)
        and row.get("status", "pending") == "pending"
    ]

    return {
        "ok": True,
        "version": AGENT_VERSION,
        "transport": "memory-v2",
        "online": online,
        "heartbeat_age_s": age,
        "heartbeat": heartbeat,
        "status": status,
        "latest_trade": latest_trade,
        "checkpoint": {
            "exists": bool(checkpoint),
            "saved_at": (
                checkpoint.get("saved_at")
                or checkpoint.get("received_at")
            ),
            "state_id": checkpoint.get("state_id"),
        },
        "pending_commands": pending[-20:],
    }


def _augment_snapshot(data):
    # Human-readable Observer layer is best-effort only.
    # It is never allowed to break Agent <-> Site transport.
    try:
        status_payload = data.get("status")
        if not isinstance(status_payload, dict):
            return data

        runtime_snapshot = status_payload.get("runtime_status")
        if not isinstance(runtime_snapshot, dict) or not runtime_snapshot:
            return data

        from . import observer_control as oc

        logs = status_payload.get("runtime_log_tail")
        if not isinstance(logs, list):
            logs = []
        logs = [str(x).rstrip("\n") for x in logs[-100:]]

        observer = oc._summarize(
            runtime_snapshot,
            "termux:observer_status.json",
        )
        current = runtime_snapshot.get("current_state") or {}
        states = (
            (runtime_snapshot.get("recent") or {}).get("states")
            or []
        )
        observer["opportunity"] = oc._entry_opportunity(
            current,
            states,
            logs,
        )

        data["observer"] = observer
        data["logs"] = logs
    except Exception as exc:
        data["summary_error"] = (
            f"{type(exc).__name__}: {exc}"
        )[:240]

    return data


@bp.errorhandler(Exception)
def _edge_error(exc):
    code = getattr(exc, "code", None)
    if not isinstance(code, int):
        code = 500
    return _json_error(
        "observer_edge_error",
        code,
        f"{type(exc).__name__}: {exc}",
    )


@bp.get("/ping")
def ping():
    return jsonify({
        "ok": True,
        "transport": "memory-v2",
        "version": AGENT_VERSION,
        "server_time": _now_iso(),
    })


@bp.get("/status")
def admin_status():
    try:
        _require_admin()
        return jsonify(_augment_snapshot(_snapshot()))
    except Exception as exc:
        code = getattr(exc, "code", 500)
        return _json_error(
            "status_failed",
            code if isinstance(code, int) else 500,
            f"{type(exc).__name__}: {exc}",
        )


@bp.post("/command")
def create_command():
    global _next_event_id

    try:
        _require_admin()
        _check_csrf()

        data = request.get_json(silent=True) or {}
        command = str(
            data.get("command") or ""
        ).lower().strip()

        if command not in {
            "start",
            "stop",
            "close",
            "status",
        }:
            return _json_error("invalid_command", 400)

        params = (
            data.get("params")
            if isinstance(data.get("params"), dict)
            else {}
        )

        if command == "start":
            live = _snapshot()
            live_status = live.get("status") or {}
            pending = live.get("pending_commands") or []

            if live.get("online") and live_status.get("active"):
                return _json_error("already_running", 409)

            if any(
                item.get("command") == "start"
                for item in pending
            ):
                return _json_error(
                    "start_already_queued",
                    409,
                )

            try:
                max_order = float(
                    params.get("max_order_usdt", 10)
                )
                max_fills = int(
                    params.get("max_fills", 20)
                )
                max_minutes = int(
                    params.get("max_minutes", 120)
                )
            except Exception:
                return _json_error("invalid_limits", 400)

            if not (1 <= max_order <= 25):
                return _json_error(
                    "max_order_usdt_must_be_1_25",
                    400,
                )
            if not (1 <= max_fills <= 100):
                return _json_error(
                    "max_fills_must_be_1_100",
                    400,
                )
            if not (5 <= max_minutes <= 360):
                return _json_error(
                    "max_minutes_must_be_5_360",
                    400,
                )

            params = {
                "max_order_usdt": max_order,
                "max_fills": max_fills,
                "max_minutes": max_minutes,
                "testnet_only": True,
            }

        payload = {
            "command_id": uuid4().hex,
            "command": command,
            "params": params,
            "status": "pending",
            "created_at": _now_iso(),
        }

        with _lock:
            event_id = _next_event_id
            _next_event_id += 1

            _memory["commands"].append({
                "event_id": event_id,
                **payload,
            })
            _memory["commands"] = (
                _memory["commands"][-100:]
            )

        return jsonify({
            "ok": True,
            "transport": "memory-v2",
            "event_id": event_id,
            **payload,
        })

    except Exception as exc:
        code = getattr(exc, "code", 500)
        return _json_error(
            "command_failed",
            code if isinstance(code, int) else 500,
            f"{type(exc).__name__}: {exc}",
        )


@bp.get("/agent/poll")
def agent_poll():
    try:
        if not _agent_authorized():
            return _json_error("unauthorized", 401)

        with _lock:
            commands = [
                _clone(row)
                for row in _memory["commands"]
                if row.get("status", "pending") == "pending"
            ]

        return jsonify({
            "ok": True,
            "transport": "memory-v2",
            "server_time": _now_iso(),
            "commands": commands[-20:],
        })

    except Exception as exc:
        return _json_error(
            "agent_poll_failed",
            500,
            f"{type(exc).__name__}: {exc}",
        )


@bp.post("/agent/report")
def agent_report():
    try:
        if not _agent_authorized():
            return _json_error("unauthorized", 401)

        data = request.get_json(silent=True) or {}
        kind = str(
            data.get("kind") or "heartbeat"
        ).lower().strip()

        if kind not in {
            "heartbeat",
            "status",
            "trade",
            "checkpoint",
        }:
            return _json_error(
                "invalid_report_kind",
                400,
            )

        payload = (
            data.get("payload")
            if isinstance(data.get("payload"), dict)
            else {}
        )
        payload = {
            **payload,
            "received_at": _now_iso(),
            "agent_version": AGENT_VERSION,
        }

        ack_ids = set()
        ack = (
            data.get("ack")
            if isinstance(data.get("ack"), list)
            else []
        )
        for item in ack:
            try:
                ack_ids.add(int(item))
            except Exception:
                pass

        with _lock:
            if kind == "heartbeat":
                _memory["heartbeat"] = payload
            elif kind == "status":
                _memory["status"] = payload
            elif kind == "trade":
                _memory["latest_trade"] = payload
            elif kind == "checkpoint":
                _memory["checkpoint"] = payload

            if ack_ids:
                for row in _memory["commands"]:
                    try:
                        row_id = int(
                            row.get("event_id", -1)
                        )
                    except Exception:
                        row_id = -1

                    if row_id in ack_ids:
                        row["status"] = "acked"
                        row["acked_at"] = _now_iso()

        return jsonify({
            "ok": True,
            "transport": "memory-v2",
            "server_time": _now_iso(),
        })

    except Exception as exc:
        return _json_error(
            "agent_report_failed",
            500,
            f"{type(exc).__name__}: {exc}",
        )
