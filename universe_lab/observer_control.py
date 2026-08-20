import base64
import gzip
import hashlib
import hmac
import math
import urllib.parse
import urllib.request
import urllib.error
import json
import os
import re
import threading
import secrets
import signal
import subprocess
import sys
import time
import atexit
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, abort, jsonify, render_template, request, session, redirect, url_for
from sqlalchemy import select, func

from db import SessionLocal
from .models import LabExternalResult, ObserverStateRecord, ObserverRuntimeCheckpoint

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

CHECKPOINT_INTERVAL_SECONDS = max(1.0, float(os.getenv("OBSERVER_CHECKPOINT_INTERVAL_SECONDS", "2")))
MAX_RUNTIME_CHECKPOINTS = max(6, min(96, int(os.getenv("OBSERVER_MAX_RUNTIME_CHECKPOINTS", "24"))))
CANARY_MAX_ORDER_HARD_CAP = max(1.0, float(os.getenv("OBSERVER_CANARY_HARD_MAX_ORDER_USDT", "25")))
CANARY_MAX_TRADES_HARD_CAP = max(1, int(os.getenv("OBSERVER_CANARY_HARD_MAX_TRADES", "10")))
CANARY_MAX_MINUTES_HARD_CAP = max(10, int(os.getenv("OBSERVER_CANARY_HARD_MAX_MINUTES", "180")))
LIVE_CANARY_ENABLED = os.getenv("OBSERVER_LIVE_CANARY_ENABLED", "0") == "1"
_checkpoint_thread = None
_checkpoint_stop = threading.Event()
_checkpoint_lock = threading.Lock()
_checkpoint_last_key = None


TESTNET_BASE_URL = "https://testnet.binance.vision"
TESTNET_MAX_ORDER_HARD_CAP = max(1.0, float(os.getenv("OBSERVER_TESTNET_HARD_MAX_ORDER_USDT", "100")))
TESTNET_MAX_TRADES_HARD_CAP = max(1, int(os.getenv("OBSERVER_TESTNET_HARD_MAX_TRADES", "100")))
TESTNET_MAX_MINUTES_HARD_CAP = max(10, int(os.getenv("OBSERVER_TESTNET_HARD_MAX_MINUTES", "360")))
_testnet_cache = {"at": 0.0, "value": None}
_testnet_cache_lock = threading.Lock()


def _testnet_credentials():
    return (
        os.getenv("BINANCE_TESTNET_API_KEY", "").strip(),
        os.getenv("BINANCE_TESTNET_API_SECRET", "").strip(),
    )


def _testnet_credentials_ready():
    key, secret = _testnet_credentials()
    return bool(key and secret)


def _testnet_http_json(method, path, params=None, signed=False):
    params = dict(params or {})
    key, secret = _testnet_credentials()
    if signed and not (key and secret):
        raise RuntimeError("BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET missing")

    if signed:
        server_req = urllib.request.Request(
            TESTNET_BASE_URL + "/api/v3/time",
            headers={"User-Agent": "UniverseLab-Observer/2.9"},
        )
        with urllib.request.urlopen(server_req, timeout=10) as r:
            server_time = int(json.loads(r.read().decode("utf-8"))["serverTime"])
        params["timestamp"] = server_time
        params["recvWindow"] = 5000

    query = urllib.parse.urlencode(params)
    if signed:
        sig = hmac.new(
            secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        query = f"{query}&signature={sig}" if query else f"signature={sig}"

    url = TESTNET_BASE_URL + path
    body = None
    if method.upper() == "GET":
        if query:
            url += "?" + query
    else:
        body = query.encode("utf-8")

    headers = {"User-Agent": "UniverseLab-Observer/2.9"}
    if signed:
        headers["X-MBX-APIKEY"] = key
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    req = urllib.request.Request(url, data=body, method=method.upper(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


def _testnet_symbol_rules():
    info = _testnet_http_json(
        "GET",
        "/api/v3/exchangeInfo",
        {"symbol": "BTCUSDT"},
        signed=False,
    )
    symbols = info.get("symbols") or []
    if not symbols:
        raise RuntimeError("BTCUSDT exchangeInfo missing")
    filters = {f.get("filterType"): f for f in symbols[0].get("filters", [])}
    lot = filters.get("LOT_SIZE") or {}
    notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
    return {
        "step_size": float(lot.get("stepSize", "0.00000001")),
        "min_qty": float(lot.get("minQty", "0")),
        "min_notional": float(notional.get("minNotional", "0")),
    }


def _floor_step(value, step):
    if step <= 0:
        return value
    return math.floor((value + 1e-15) / step) * step


def _testnet_account_snapshot(force=False):
    now = time.time()
    with _testnet_cache_lock:
        if (
            not force
            and _testnet_cache["value"] is not None
            and now - float(_testnet_cache["at"]) < 8.0
        ):
            return dict(_testnet_cache["value"])

    if not _testnet_credentials_ready():
        return {
            "credentials_ready": False,
            "ok": False,
            "reason": "MISSING_TESTNET_CREDENTIALS",
        }

    try:
        account = _testnet_http_json("GET", "/api/v3/account", signed=True)
        ticker = _testnet_http_json(
            "GET",
            "/api/v3/ticker/price",
            {"symbol": "BTCUSDT"},
            signed=False,
        )
        price = float(ticker.get("price", 0) or 0)
        balances = {
            x.get("asset"): float(x.get("free", 0) or 0)
            for x in account.get("balances", [])
            if x.get("asset") in ("BTC", "USDT")
        }
        value = {
            "credentials_ready": True,
            "ok": True,
            "usdt": balances.get("USDT", 0.0),
            "btc": balances.get("BTC", 0.0),
            "price": price,
            "equity_usdt": balances.get("USDT", 0.0) + balances.get("BTC", 0.0) * price,
        }
    except Exception as exc:
        value = {
            "credentials_ready": True,
            "ok": False,
            "reason": str(exc)[:300],
        }

    with _testnet_cache_lock:
        _testnet_cache["at"] = now
        _testnet_cache["value"] = dict(value)
    return value


def _filled_testnet_count():
    path = _runtime_dir() / "storage" / "exchange_trades_erl1.jsonl"
    return sum(
        1
        for row in _jsonl_rows(path)
        if row.get("status") == "FILLED_TESTNET"
    )


def _testnet_status():
    meta = _read_meta()
    cfg = meta.get("canary") or {}
    profile = meta.get("profile") or "PAPER"
    snap = _testnet_account_snapshot()
    baseline = cfg.get("testnet_baseline") or {}
    base_btc = float(baseline.get("btc", 0) or 0)
    base_equity = baseline.get("equity_usdt")
    btc = float(snap.get("btc", 0) or 0) if snap.get("ok") else None
    price = float(snap.get("price", 0) or 0) if snap.get("ok") else None
    bot_btc = max(0.0, btc - base_btc) if btc is not None else None
    bot_value = bot_btc * price if bot_btc is not None and price is not None else None
    pnl = (
        float(snap.get("equity_usdt")) - float(base_equity)
        if snap.get("ok") and base_equity is not None
        else None
    )
    baseline_fills = int(cfg.get("baseline_testnet_fills", 0) or 0)
    return {
        **snap,
        "profile": profile,
        "active": profile == "TESTNET_LIVE" and _process_info().get("alive", False),
        "max_order_usdt": cfg.get("max_order_usdt"),
        "max_trades": cfg.get("max_trades"),
        "max_minutes": cfg.get("max_minutes"),
        "fills": max(0, _filled_testnet_count() - baseline_fills) if cfg else 0,
        "baseline_btc": base_btc if cfg else None,
        "bot_position_btc": bot_btc,
        "bot_position_value_usdt": bot_value,
        "session_pnl_usdt": pnl,
        "position_open": bool(bot_value is not None and bot_value >= 5.0),
    }


def _testnet_flatten(reason="MANUAL"):
    # Close only BTC accumulated above the Testnet session baseline.
    if not _testnet_credentials_ready():
        return False, "Testnet credentials missing."

    meta = _read_meta()
    cfg = meta.get("canary") or {}
    baseline = cfg.get("testnet_baseline") or {}
    base_btc = float(baseline.get("btc", 0) or 0)

    try:
        snap = _testnet_account_snapshot(force=True)
        if not snap.get("ok"):
            return False, f"Testnet account read failed: {snap.get('reason','unknown')}"
        current_btc = float(snap.get("btc", 0) or 0)
        price = float(snap.get("price", 0) or 0)
        bot_qty = max(0.0, current_btc - base_btc)
        rules = _testnet_symbol_rules()
        qty = _floor_step(bot_qty, float(rules["step_size"]))
        if qty <= 0 or qty * price < float(rules["min_notional"]):
            return True, "No bot-opened testnet BTC position to close."

        qty_text = (f"{qty:.8f}").rstrip("0").rstrip(".")
        params = {
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "MARKET",
            "quantity": qty_text,
            "newOrderRespType": "FULL",
        }
        _testnet_http_json("POST", "/api/v3/order/test", params, signed=True)
        result = _testnet_http_json("POST", "/api/v3/order", params, signed=True)

        event = {
            "time": datetime.now(timezone.utc).isoformat(),
            "state_id": "CONTROL",
            "strategy": "TESTNET_POSITION_MANAGER",
            "action": "SELL",
            "status": "FILLED_TESTNET",
            "mode": "TESTNET",
            "reason": reason,
            "order_id": result.get("orderId"),
            "executed_qty": result.get("executedQty"),
            "cummulative_quote_qty": result.get("cummulativeQuoteQty"),
            "exchange_status": result.get("status"),
        }
        path = _runtime_dir() / "storage" / "exchange_trades_erl1.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        _testnet_account_snapshot(force=True)
        return True, f"Testnet position closed: {qty_text} BTC."
    except Exception as exc:
        return False, f"Testnet flatten failed: {type(exc).__name__}: {exc}"


def _gzip_text(text):
    raw = (text or "").encode("utf-8")
    return base64.b64encode(gzip.compress(raw, compresslevel=6)).decode("ascii")


def _gunzip_text(encoded):
    return gzip.decompress(base64.b64decode(encoded.encode("ascii"))).decode("utf-8")


def _read_meta():
    try:
        return json.loads(META_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _jsonl_rows(path):
    rows = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    except Exception:
        pass
    return rows


def _filled_live_count():
    path = _runtime_dir() / "storage" / "exchange_trades_erl1.jsonl"
    return sum(1 for row in _jsonl_rows(path) if row.get("status") == "FILLED_LIVE")


def _latest_checkpoint():
    try:
        with SessionLocal() as db:
            return db.scalar(
                select(ObserverRuntimeCheckpoint)
                .order_by(ObserverRuntimeCheckpoint.created_at.desc())
                .limit(1)
            )
    except Exception:
        return None


def _restore_latest_checkpoint():
    row = _latest_checkpoint()
    if not row:
        return None
    try:
        runtime = _runtime_dir()
        storage = runtime / "storage"
        storage.mkdir(parents=True, exist_ok=True)
        runtime_text = _gunzip_text(row.runtime_gzip_b64)
        json.loads(runtime_text)
        (storage / "runtime_state.json").write_text(runtime_text, encoding="utf-8")
        status_obj = json.loads(row.status_json)
        (storage / "observer_status.json").write_text(
            json.dumps(status_obj, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {
            "state_id": row.state_id,
            "session_id": row.session_id,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
    except Exception as exc:
        print("OBSERVER RESTORE warning:", type(exc).__name__, exc)
        return None


def _persist_checkpoint(force=False):
    global _checkpoint_last_key
    if not _checkpoint_lock.acquire(blocking=False):
        return False
    try:
        runtime = _runtime_dir()
        status_path = runtime / "storage" / "observer_status.json"
        runtime_path = runtime / "storage" / "runtime_state.json"
        if not status_path.exists() or not runtime_path.exists():
            return False

        status = _read_json(status_path)
        if not isinstance(status, dict):
            return False
        current = status.get("current_state") or {}
        state_id = str(current.get("state_id") or "").strip()
        if not state_id:
            return False
        session_id = str(current.get("session_id") or "unknown")
        key = (session_id, state_id)
        if not force and key == _checkpoint_last_key:
            return False

        runtime_text = runtime_path.read_text(encoding="utf-8")
        if not runtime_text.strip():
            return False
        json.loads(runtime_text)

        with SessionLocal() as db:
            existing = db.scalar(
                select(ObserverStateRecord).where(
                    ObserverStateRecord.session_id == session_id,
                    ObserverStateRecord.state_id == state_id,
                )
            )
            if not existing:
                db.add(ObserverStateRecord(
                    session_id=session_id,
                    state_id=state_id,
                    market_time=current.get("market_time") or current.get("time"),
                    price=float(current["price"]) if current.get("price") is not None else None,
                    action=current.get("action"),
                    regime=current.get("regime"),
                    payload_json=json.dumps(status, ensure_ascii=False),
                ))

            # Full compressed checkpoint. Keep a rolling recovery window.
            db.add(ObserverRuntimeCheckpoint(
                session_id=session_id,
                state_id=state_id,
                runtime_gzip_b64=_gzip_text(runtime_text),
                status_json=json.dumps(status, ensure_ascii=False),
            ))
            db.flush()

            old = db.scalars(
                select(ObserverRuntimeCheckpoint)
                .order_by(ObserverRuntimeCheckpoint.created_at.desc())
                .offset(MAX_RUNTIME_CHECKPOINTS)
            ).all()
            for row in old:
                db.delete(row)
            db.commit()

        _checkpoint_last_key = key
        return True
    except Exception as exc:
        print("OBSERVER CHECKPOINT warning:", type(exc).__name__, exc)
        return False
    finally:
        _checkpoint_lock.release()


def _backup_status():
    db_url = os.getenv("DATABASE_URL", "")
    persistent_db = bool(db_url and not db_url.startswith("sqlite"))
    try:
        with SessionLocal() as db:
            latest = db.scalar(
                select(ObserverRuntimeCheckpoint)
                .order_by(ObserverRuntimeCheckpoint.created_at.desc())
                .limit(1)
            )
            state_count = db.scalar(select(func.count()).select_from(ObserverStateRecord)) or 0
            return {
                "persistent": persistent_db,
                "state_count": int(state_count),
                "latest_state": latest.state_id if latest else None,
                "latest_at": latest.created_at.isoformat() if latest and latest.created_at else None,
                "checkpoint_window": MAX_RUNTIME_CHECKPOINTS,
            }
    except Exception as exc:
        return {
            "persistent": False,
            "state_count": 0,
            "latest_state": None,
            "latest_at": None,
            "checkpoint_window": MAX_RUNTIME_CHECKPOINTS,
            "error": str(exc)[:160],
        }


def _watchdog_worker():
    while not _checkpoint_stop.wait(CHECKPOINT_INTERVAL_SECONDS):
        info = _process_info()
        if not info.get("alive"):
            _persist_checkpoint(force=True)
            break
        _persist_checkpoint()
        meta = _read_meta()
        profile = meta.get("profile")
        if profile not in ("LIVE_CANARY", "TESTNET_LIVE"):
            continue
        canary = meta.get("canary") or {}
        started_at = meta.get("started_at")
        try:
            started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
            elapsed_min = (datetime.now(timezone.utc) - started).total_seconds() / 60.0
        except Exception:
            elapsed_min = 0.0

        if profile == "TESTNET_LIVE":
            baseline = int(canary.get("baseline_testnet_fills", 0) or 0)
            fills = max(0, _filled_testnet_count() - baseline)
        else:
            baseline = int(canary.get("baseline_live_fills", 0) or 0)
            fills = max(0, _filled_live_count() - baseline)
        max_trades = int(canary.get("max_trades", 0) or 0)
        max_minutes = int(canary.get("max_minutes", 0) or 0)

        if max_trades and fills >= max_trades:
            if profile == "TESTNET_LIVE":
                _testnet_flatten(reason="AUTO_MAX_FILLS")
                _stop_process(reason=f"TESTNET auto-stop: {fills}/{max_trades} fills reached.")
            else:
                _stop_process(reason=f"LIVE CANARY auto-stop: {fills}/{max_trades} fills reached.")
            break
        if max_minutes and elapsed_min >= max_minutes:
            if profile == "TESTNET_LIVE":
                _testnet_flatten(reason="AUTO_TIME_LIMIT")
                _stop_process(reason=f"TESTNET auto-stop: {max_minutes} minute limit reached.")
            else:
                _stop_process(reason=f"LIVE CANARY auto-stop: {max_minutes} minute limit reached.")
            break


def _ensure_checkpoint_worker():
    global _checkpoint_thread
    if _checkpoint_thread and _checkpoint_thread.is_alive():
        return
    _checkpoint_stop.clear()
    _checkpoint_thread = threading.Thread(
        target=_watchdog_worker,
        name="observer-checkpoint-watchdog",
        daemon=True,
    )
    _checkpoint_thread.start()


def _canary_status():
    meta = _read_meta()
    cfg = meta.get("canary") or {}
    baseline = int(cfg.get("baseline_live_fills", 0) or 0)
    return {
        "profile": meta.get("profile") or "PAPER",
        "credentials_ready": bool(
            os.getenv("BINANCE_API_KEY", "").strip()
            and os.getenv("BINANCE_API_SECRET", "").strip()
        ),
        "max_order_usdt": cfg.get("max_order_usdt"),
        "max_trades": cfg.get("max_trades"),
        "max_minutes": cfg.get("max_minutes"),
        "live_fills": max(0, _filled_live_count() - baseline) if cfg else 0,
    }


def _parse_geo_log(lines):
    support = resistance = atr_pct = None
    pivot_low = pivot_high = None
    for line in reversed(lines or []):
        if support is None or resistance is None:
            m = re.search(r"GEO LEVELS:\s*SUP=([0-9.]+).*?\|\s*RES=([0-9.]+)", line)
            if m:
                support, resistance = float(m.group(1)), float(m.group(2))
        if atr_pct is None:
            m = re.search(r"SR1:\s*ATR14=([0-9.]+)%", line)
            if m:
                atr_pct = float(m.group(1))
        if pivot_low is None:
            m = re.search(r"GEO ZONE:\s*\S+\s+([0-9.]+)-([0-9.]+)\s+", line)
            if m:
                pivot_low, pivot_high = float(m.group(1)), float(m.group(2))
        if support is not None and resistance is not None and atr_pct is not None:
            break
    return {
        "support": support,
        "resistance": resistance,
        "atr_pct": atr_pct,
        "pivot_low": pivot_low,
        "pivot_high": pivot_high,
    }


def _entry_opportunity(current, recent_states, logs):
    try:
        price = float(current.get("price"))
    except Exception:
        return {"available": False}

    action = str(current.get("action") or "HOLD").upper()
    geo = _parse_geo_log(logs)
    source = "GEO3"
    target = None
    role = None

    if action == "BUY" and geo.get("support"):
        target, role = float(geo["support"]), "SUPPORT"
    elif action == "SELL" and geo.get("resistance"):
        target, role = float(geo["resistance"]), "RESISTANCE"

    prices = []
    for item in recent_states or []:
        try:
            prices.append(float(item.get("price")))
        except Exception:
            pass

    if target is None and prices:
        source = "MICRO_RANGE_PROXY"
        if action == "BUY":
            target, role = min(prices[-30:]), "RANGE_LOW"
        elif action == "SELL":
            target, role = max(prices[-30:]), "RANGE_HIGH"

    if target is None:
        return {
            "available": False,
            "action": action,
            "source": source,
        }

    atr_pct = geo.get("atr_pct")
    atr_abs = price * float(atr_pct) / 100.0 if atr_pct else max(price * 0.001, 1.0)
    half = max(atr_abs * 0.50, price * 0.00025)
    zone_low, zone_high = target - half, target + half

    if zone_low <= price <= zone_high:
        status = "IN_ZONE"
        distance_abs = 0.0
    elif action == "BUY" and price < zone_low:
        status = "SUPPORT_BROKEN"
        distance_abs = zone_low - price
    elif action == "SELL" and price > zone_high:
        status = "RESISTANCE_BROKEN"
        distance_abs = price - zone_high
    else:
        distance_abs = min(abs(price - zone_low), abs(price - zone_high))
        distance_atr = distance_abs / max(atr_abs, 1e-9)
        if distance_atr <= 1.0:
            status = "APPROACHING"
        elif distance_atr <= 2.5:
            status = "NEAR"
        else:
            status = "FAR"

    distance_pct = distance_abs / price * 100.0
    distance_atr = distance_abs / max(atr_abs, 1e-9)

    scale_values = [price, zone_low, zone_high]
    if geo.get("support"):
        scale_values.append(float(geo["support"]))
    if geo.get("resistance"):
        scale_values.append(float(geo["resistance"]))
    if prices:
        scale_values.extend(prices[-30:])
    lo, hi = min(scale_values), max(scale_values)
    pad = max((hi - lo) * 0.08, atr_abs)
    lo -= pad
    hi += pad
    span = max(hi - lo, 1e-9)

    return {
        "available": True,
        "action": action,
        "role": role,
        "source": source,
        "current_price": price,
        "target": target,
        "zone_low": zone_low,
        "zone_high": zone_high,
        "distance_abs": distance_abs,
        "distance_pct": distance_pct,
        "distance_atr": distance_atr,
        "status": status,
        "atr_pct": atr_pct,
        "support": geo.get("support"),
        "resistance": geo.get("resistance"),
        "scale_min": lo,
        "scale_max": hi,
        "marker_pct": (price - lo) / span * 100.0,
        "zone_left_pct": (zone_low - lo) / span * 100.0,
        "zone_width_pct": (zone_high - zone_low) / span * 100.0,
    }


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
            return [sys.executable, "-u", name]
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



def _safe_observer_env(profile="PAPER", canary=None):
    env = os.environ.copy()
    profile = str(profile or "PAPER").upper()
    canary = dict(canary or {})

    # Android download paths don't exist on Render.
    env["MOR_EXPORT_DOWNLOAD_PATH"] = "storage/MOR_latest_export.json"
    env["MOR_EXPORT_FULL_DOWNLOAD_PATH"] = "storage/MOR_export_full.json"
    env["MOR_OBSERVER_STATUS_FILE"] = "storage/observer_status.json"
    env["MOR_GAP_MAX_MINUTES"] = "120"
    env["PYTHONUNBUFFERED"] = "1"

    # Research bypasses stay OFF in every mode.
    env["MOR_TESTNET_RELAX_GATES"] = "0"
    env["MOR_TESTNET_GEOMETRY_ACTIONS"] = "0"
    env["MOR_TESTNET_ACTION_ARBITRATION"] = "0"
    env["MOR_PREFLIGHT_ORDER_TEST"] = "0"

    if profile == "LIVE_CANARY":
        env["MOR_EXECUTION_MODE"] = "LIVE"
        env["MOR_LIVE_ARM"] = "I_ACCEPT_REAL_MONEY_EXECUTION"
        env["MOR_MAX_ORDER_USDT"] = str(canary["max_order_usdt"])
        env["MOR_ORDER_COOLDOWN_SECONDS"] = str(max(300, int(canary.get("cooldown_seconds", 300))))
        # API credentials are inherited from Render secrets only.
    elif profile == "TESTNET_LIVE":
        env["MOR_EXECUTION_MODE"] = "TESTNET"
        env["MOR_LIVE_ARM"] = ""
        env["BINANCE_API_KEY"] = os.getenv("BINANCE_TESTNET_API_KEY", "").strip()
        env["BINANCE_API_SECRET"] = os.getenv("BINANCE_TESTNET_API_SECRET", "").strip()
        env["MOR_MAX_ORDER_USDT"] = str(canary["max_order_usdt"])
        env["MOR_ORDER_COOLDOWN_SECONDS"] = str(max(60, int(canary.get("cooldown_seconds", 60))))
        env["MOR_TESTNET_BASELINE_BTC"] = str((canary.get("testnet_baseline") or {}).get("btc", 0) or 0)
        env["MOR_MARKET_REST_URL"] = "https://testnet.binance.vision/api/v3/klines"
        env["MOR_MARKET_WS_URL"] = "wss://stream.testnet.binance.vision/ws/btcusdt@kline_1m"
    elif profile == "TESTNET":
        env["MOR_EXECUTION_MODE"] = "TESTNET"
        env["MOR_LIVE_ARM"] = ""
    else:
        env["MOR_EXECUTION_MODE"] = "PAPER"
        env["MOR_LIVE_ARM"] = ""
        env.pop("BINANCE_API_KEY", None)
        env.pop("BINANCE_API_SECRET", None)

    return env


def _start_process(profile="PAPER", canary=None):
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

    profile = str(profile or "PAPER").upper()
    canary = dict(canary or {})
    if profile == "LIVE_CANARY":
        if not (
            os.getenv("BINANCE_API_KEY", "").strip()
            and os.getenv("BINANCE_API_SECRET", "").strip()
        ):
            return False, "LIVE CANARY blocked: Binance API credentials are missing in Render environment variables."

    if profile == "TESTNET_LIVE" and not _testnet_credentials_ready():
        return False, "TESTNET blocked: add BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET."

    restored = _restore_latest_checkpoint()

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
            env=_safe_observer_env(profile=profile, canary=canary),
        )
    except Exception as exc:
        log_handle.close()
        return False, f"Failed to start Observer: {type(exc).__name__}: {exc}"

    if profile == "LIVE_CANARY":
        canary["baseline_live_fills"] = _filled_live_count()

    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    META_FILE.write_text(
        json.dumps(
            {
                "pid": proc.pid,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "command": command,
                "runtime_dir": str(runtime),
                "profile": profile,
                "canary": canary,
                "restored_checkpoint": restored,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    _ensure_checkpoint_worker()

    restored_note = f" Restored {restored['state_id']} from DB." if restored else ""
    return True, f"Observer started (PID {proc.pid}) · {profile}.{restored_note}"


def _stop_process(reason=None):
    pid = _read_pid()
    if not _pid_alive(pid):
        PID_FILE.unlink(missing_ok=True)
        _persist_checkpoint(force=True)
        return True, reason or "Observer is already stopped."

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

        _persist_checkpoint(force=True)
        PID_FILE.unlink(missing_ok=True)
        _checkpoint_stop.set()
        return True, reason or f"Observer stopped (PID {pid})."
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
    opportunity = _entry_opportunity(current, states, _tail_log(180))

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
        "opportunity": opportunity,
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
        "backup": _backup_status(),
        "canary": _canary_status(),
        "testnet": _testnet_status(),
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


@bp.get("/control")
def control():
    if not session.get("admin"):
        return redirect(url_for("admin_login", next="/observer/control"))
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


@bp.post("/api/start-testnet")
def start_testnet_api():
    _require_admin()
    _check_csrf()
    data = request.get_json(silent=True) or {}

    if str(data.get("confirm", "")).strip().upper() != "TESTNET":
        return jsonify({"ok": False, "message": "Type TESTNET to start fake-money exchange execution."}), 400

    try:
        max_order = float(data.get("max_order_usdt"))
        max_trades = int(data.get("max_trades"))
        max_minutes = int(data.get("max_minutes"))
    except Exception:
        return jsonify({"ok": False, "message": "Fill max order, max trades and duration."}), 400

    if not (1.0 <= max_order <= TESTNET_MAX_ORDER_HARD_CAP):
        return jsonify({"ok": False, "message": f"Max test order must be 1..{TESTNET_MAX_ORDER_HARD_CAP:.0f} USDT."}), 400
    if not (1 <= max_trades <= TESTNET_MAX_TRADES_HARD_CAP):
        return jsonify({"ok": False, "message": f"Max test fills must be 1..{TESTNET_MAX_TRADES_HARD_CAP}."}), 400
    if not (5 <= max_minutes <= TESTNET_MAX_MINUTES_HARD_CAP):
        return jsonify({"ok": False, "message": f"Duration must be 5..{TESTNET_MAX_MINUTES_HARD_CAP} minutes."}), 400
    if not _testnet_credentials_ready():
        return jsonify({"ok": False, "message": "Add BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET to Render first."}), 409

    if _process_info().get("alive"):
        ok_stop, msg_stop = _stop_process()
        if not ok_stop:
            return jsonify({"ok": False, "message": msg_stop}), 500
        time.sleep(0.5)

    baseline = _testnet_account_snapshot(force=True)
    if not baseline.get("ok"):
        return jsonify({"ok": False, "message": f"Testnet account preflight failed: {baseline.get('reason','unknown')}"}), 409

    cfg = {
        "max_order_usdt": max_order,
        "max_trades": max_trades,
        "max_minutes": max_minutes,
        "cooldown_seconds": 60,
        "baseline_testnet_fills": _filled_testnet_count(),
        "testnet_baseline": {
            "usdt": baseline.get("usdt"),
            "btc": baseline.get("btc"),
            "equity_usdt": baseline.get("equity_usdt"),
            "price": baseline.get("price"),
        },
    }
    ok, message = _start_process(profile="TESTNET_LIVE", canary=cfg)
    payload = _status_payload()
    payload["ok"] = ok
    payload["message"] = message
    return jsonify(payload), (200 if ok else 409)


@bp.post("/api/close-testnet-position")
def close_testnet_position_api():
    _require_admin()
    _check_csrf()
    ok, message = _testnet_flatten(reason="MANUAL_CLOSE")
    payload = _status_payload()
    payload["ok"] = ok
    payload["message"] = message
    return jsonify(payload), (200 if ok else 409)


@bp.post("/api/stop-testnet")
def stop_testnet_api():
    _require_admin()
    _check_csrf()
    close_ok, close_message = _testnet_flatten(reason="STOP_FLATTEN")
    ok, message = _stop_process(reason="Testnet Observer stopped.")
    payload = _status_payload()
    payload["ok"] = bool(ok and close_ok)
    payload["message"] = f"{close_message} {message}"
    return jsonify(payload), (200 if ok and close_ok else 409)


@bp.post("/api/start-live-canary")
def start_live_canary_api():
    _require_admin()
    _check_csrf()
    if not LIVE_CANARY_ENABLED:
        return jsonify({"ok": False, "message": "Real-money execution is disabled. Use Termux Binance Spot Testnet."}), 403
    data = request.get_json(silent=True) or {}

    if data.get("confirm") != "LIVE":
        return jsonify({"ok": False, "message": "Type LIVE to arm real-money canary."}), 400

    try:
        max_order = float(data.get("max_order_usdt"))
        max_trades = int(data.get("max_trades"))
        max_minutes = int(data.get("max_minutes"))
    except Exception:
        return jsonify({"ok": False, "message": "Fill max order, max trades and duration."}), 400

    if not (1.0 <= max_order <= CANARY_MAX_ORDER_HARD_CAP):
        return jsonify({
            "ok": False,
            "message": f"Max order must be 1..{CANARY_MAX_ORDER_HARD_CAP:.0f} USDT.",
        }), 400
    if not (1 <= max_trades <= CANARY_MAX_TRADES_HARD_CAP):
        return jsonify({
            "ok": False,
            "message": f"Max trades must be 1..{CANARY_MAX_TRADES_HARD_CAP}.",
        }), 400
    if not (5 <= max_minutes <= CANARY_MAX_MINUTES_HARD_CAP):
        return jsonify({
            "ok": False,
            "message": f"Duration must be 5..{CANARY_MAX_MINUTES_HARD_CAP} minutes.",
        }), 400

    canary = {
        "max_order_usdt": max_order,
        "max_trades": max_trades,
        "max_minutes": max_minutes,
        "cooldown_seconds": 300,
    }
    ok, message = _start_process(profile="LIVE_CANARY", canary=canary)
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
