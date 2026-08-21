import hashlib
import json
import secrets
import threading
from datetime import datetime, timezone
from uuid import uuid4

from flask import Blueprint, jsonify, request

from . import observer_control as oc
from .observer_edge import AGENT_VERSION, BOOTSTRAP_TOKEN_HASH, _augment_live_snapshot

bp = Blueprint(
    "universe_observer_edge_memory",
    __name__,
    url_prefix="/observer/edge",
)

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


def _clone(value):
    try:
        return json.loads(json.dumps(value, ensure_ascii=False))
    except Exception:
        return value


def _admin_require():
    oc._require_admin()
    oc._check_csrf()


def _agent_authorized():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    token = auth[7:].strip()
    if not token:
        return False
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return bool(
        BOOTSTRAP_TOKEN_HASH
        and secrets.compare_digest(digest, BOOTSTRAP_TOKEN_HASH)
    )


def _require_agent():
    if not _agent_authorized():
        return jsonify({
            "ok": False,
            "error": "unauthorized",
            "transport": "memory-v1",
        }), 401
    return None


def _parse_time(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
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

    received = _parse_time(heartbeat.get("received_at"))
    if received is None:
        received = _parse_time(heartbeat.get("time"))

    age = None
    online = False
    if received is not None:
        age = max(
            0.0,
            (datetime.now(timezone.utc) - received).total_seconds(),
        )
        online = age <= 25.0

    pending = [
        item for item in commands
        if isinstance(item, dict)
        and item.get("status", "pending") == "pending"
    ]

    return {
        "ok": True,
        "version": AGENT_VERSION,
        "transport": "memory-v1",
        "online": online,
        "heartbeat_age_s": age,
        "heartbeat": heartbeat,
        "status": status,
        "latest_trade": latest_trade,
        "checkpoint": {
            "exists": bool(checkpoint),
            "saved_at": checkpoint.get("saved_at") or checkpoint.get("received_at"),
            "state_id": checkpoint.get("state_id"),
        },
        "pending_commands": pending[-20:],
    }


@bp.errorhandler(Exception)
def _json_error(exc):
    code = getattr(exc, "code", None)
    if not isinstance(code, int):
        code = 503
    return jsonify({
        "ok": False,
        "error": "observer_edge_memory_error",
        "detail": f"{type(exc).__name__}: {exc}"[:500],
        "http_status": code,
        "transport": "memory-v1",
    }), code


@bp.get("/status")
def admin_status():
    oc._require_admin()
    return jsonify(_augment_live_snapshot(_snapshot()))


@bp.post("/command")
def create_command():
    global _next_event_id

    _admin_require()
    data = request.get_json(silent=True) or {}
    command = str(data.get("command") or "").lower().strip()

    if command not in {"start", "stop", "close", "status"}:
        return jsonify({"ok": False, "error": "invalid command"}), 400

    params = data.get("params") if isinstance(data.get("params"), dict) else {}

    if command == "start":
        live = _snapshot()
        live_status = live.get("status") or {}
        pending = live.get("pending_commands") or []

        if live.get("online") and live_status.get("active"):
            return jsonify({
                "ok": False,
                "error": "already_running",
                "message": "Testnet runtime is already running.",
            }), 409

        if any(item.get("command") == "start" for item in pending):
            return jsonify({
                "ok": False,
                "error": "start_already_queued",
                "message": "START is already queued.",
            }), 409

        try:
            max_order = float(params.get("max_order_usdt", 10))
            max_fills = int(params.get("max_fills", 20))
            max_minutes = int(params.get("max_minutes", 120))
        except Exception:
            return jsonify({"ok": False, "error": "invalid limits"}), 400

        if not (1 <= max_order <= 25):
            return jsonify({"ok": False, "error": "max_order_usdt must be 1..25"}), 400
        if not (1 <= max_fills <= 100):
            return jsonify({"ok": False, "error": "max_fills must be 1..100"}), 400
        if not (5 <= max_minutes <= 360):
            return jsonify({"ok": False, "error": "max_minutes must be 5..360"}), 400

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
        row = {"event_id": event_id, **payload}
        _memory["commands"].append(row)
        _memory["commands"] = _memory["commands"][-100:]

    return jsonify({
        "ok": True,
        "transport": "memory-v1",
        "event_id": event_id,
        **payload,
    })


@bp.get("/agent/poll")
def agent_poll():
    denied = _require_agent()
    if denied:
        return denied

    with _lock:
        commands = [
            _clone(item)
            for item in _memory["commands"]
            if item.get("status", "pending") == "pending"
        ]

    return jsonify({
        "ok": True,
        "transport": "memory-v1",
        "server_time": _now_iso(),
        "commands": commands[-20:],
    })


@bp.post("/agent/report")
def agent_report():
    denied = _require_agent()
    if denied:
        return denied

    data = request.get_json(silent=True) or {}
    kind = str(data.get("kind") or "heartbeat").lower().strip()
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    payload = {
        **payload,
        "received_at": _now_iso(),
        "agent_version": AGENT_VERSION,
    }

    if kind not in {"heartbeat", "status", "trade", "checkpoint"}:
        return jsonify({"ok": False, "error": "invalid report kind"}), 400

    ack_ids = set()
    ack = data.get("ack") if isinstance(data.get("ack"), list) else []
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
                    row_id = int(row.get("event_id", -1))
                except Exception:
                    row_id = -1
                if row_id in ack_ids:
                    row["status"] = "acked"
                    row["acked_at"] = _now_iso()

    return jsonify({
        "ok": True,
        "transport": "memory-v1",
        "server_time": _now_iso(),
    })
