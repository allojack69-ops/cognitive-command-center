
import base64
import gzip
import hashlib
import hmac
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, jsonify, request
from sqlalchemy import select

from db import Event, SessionLocal
from . import observer_control as oc

bp = Blueprint("universe_observer_testnet", __name__, url_prefix="/observer/testnet")

TESTNET_BASE = "https://testnet.binance.vision"
TESTNET_WS = "wss://stream.testnet.binance.vision/ws/btcusdt@kline_1m"
TESTNET_KLINES = TESTNET_BASE + "/api/v3/klines"
TESTNET_RUNTIME = oc._runtime_dir()
TESTNET_LOG = oc.LOG_FILE
TESTNET_META = Path(os.getenv("OBSERVER_TESTNET_META_FILE", "/tmp/universe_observer_testnet_meta.json"))
CHECKPOINT_EVENT = "observer_testnet_checkpoint_v29"
TRADE_EVENT = "observer_testnet_trade_v29"

MAX_ORDER_HARD = max(1.0, float(os.getenv("OBSERVER_TESTNET_HARD_MAX_ORDER_USDT", "100")))
MAX_FILLS_HARD = max(1, int(os.getenv("OBSERVER_TESTNET_HARD_MAX_TRADES", "100")))
MAX_MINUTES_HARD = max(10, int(os.getenv("OBSERVER_TESTNET_HARD_MAX_MINUTES", "360")))

_watch_thread = None
_watch_stop = threading.Event()
_last_checkpoint_state = None
_persisted_trade_keys = set()

# Protect the production/main-market checkpoint stream from a TESTNET session.
# observer_control's normal STOP/RESTART can still kill the process, but while
# META_FILE says TESTNET_LIVE it must not write Testnet runtime into the main DB.
_original_main_persist = oc._persist_checkpoint


def _guarded_main_persist(force=False):
    try:
        meta = json.loads(oc.META_FILE.read_text(encoding="utf-8"))
        if meta.get("profile") == "TESTNET_LIVE":
            return False
    except Exception:
        pass
    return _original_main_persist(force=force)


oc._persist_checkpoint = _guarded_main_persist


def _require():
    oc._require_admin()
    oc._check_csrf()


def _creds():
    return (
        os.getenv("BINANCE_TESTNET_API_KEY", "").strip(),
        os.getenv("BINANCE_TESTNET_API_SECRET", "").strip(),
    )


def _creds_ready():
    key, secret = _creds()
    return bool(key and secret)


def _read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {} if default is None else default


def _write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _meta():
    return _read_json(TESTNET_META, {})


def _pid_alive(pid):
    try:
        pid = int(pid or 0)
        if pid <= 1:
            return False
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _testnet_alive():
    return _pid_alive(_meta().get("pid"))


def _api(method, path, params=None, signed=False):
    params = dict(params or {})
    key, secret = _creds()
    if signed and not (key and secret):
        raise RuntimeError("Testnet API credentials missing")

    if signed:
        req = urllib.request.Request(
            TESTNET_BASE + "/api/v3/time",
            headers={"User-Agent": "UniverseLab-Observer-Testnet/2.9"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            params["timestamp"] = int(json.loads(response.read().decode("utf-8"))["serverTime"])
        params["recvWindow"] = 5000

    query = urllib.parse.urlencode(params)
    if signed:
        signature = hmac.new(
            secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        query = query + "&signature=" + signature if query else "signature=" + signature

    url = TESTNET_BASE + path
    body = None
    if method.upper() == "GET":
        if query:
            url += "?" + query
    else:
        body = query.encode("utf-8")

    headers = {"User-Agent": "UniverseLab-Observer-Testnet/2.9"}
    if signed:
        headers["X-MBX-APIKEY"] = key
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    req = urllib.request.Request(url, data=body, method=method.upper(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:600]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


def _account():
    if not _creds_ready():
        return {"ok": False, "credentials_ready": False, "reason": "KEYS_MISSING"}
    try:
        account = _api("GET", "/api/v3/account", signed=True)
        ticker = _api("GET", "/api/v3/ticker/price", {"symbol": "BTCUSDT"})
        price = float(ticker.get("price", 0) or 0)
        balances = {}
        for item in account.get("balances", []):
            asset = item.get("asset")
            if asset in ("BTC", "USDT"):
                balances[asset] = {
                    "free": float(item.get("free", 0) or 0),
                    "locked": float(item.get("locked", 0) or 0),
                }
        btc = balances.get("BTC", {}).get("free", 0.0)
        usdt = balances.get("USDT", {}).get("free", 0.0)
        return {
            "ok": True,
            "credentials_ready": True,
            "btc": btc,
            "usdt": usdt,
            "price": price,
            "equity_usdt": usdt + btc * price,
        }
    except Exception as exc:
        return {
            "ok": False,
            "credentials_ready": True,
            "reason": f"{type(exc).__name__}: {exc}"[:700],
        }


def _exchange_rules():
    info = _api("GET", "/api/v3/exchangeInfo", {"symbol": "BTCUSDT"})
    symbols = info.get("symbols") or []
    if not symbols:
        raise RuntimeError("BTCUSDT missing from Testnet exchangeInfo")
    filters = {f.get("filterType"): f for f in symbols[0].get("filters", [])}
    lot = filters.get("LOT_SIZE") or {}
    notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
    return {
        "step": float(lot.get("stepSize", "0.00000001")),
        "min_qty": float(lot.get("minQty", "0")),
        "min_notional": float(notional.get("minNotional", "0")),
    }


def _floor_step(value, step):
    if step <= 0:
        return value
    return math.floor((value + 1e-15) / step) * step


def _runtime_paths():
    storage = TESTNET_RUNTIME / "storage"
    return {
        "storage": storage,
        "runtime": storage / "runtime_state.json",
        "status": storage / "observer_status.json",
        "trades": storage / "exchange_trades_erl1.jsonl",
    }


def _tail_jsonl(path):
    rows = []
    try:
        for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
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


def _trade_key(row):
    return "|".join(
        str(row.get(k, ""))
        for k in ("order_id", "time", "state_id", "action", "status")
    )


def _testnet_fills():
    return [
        x for x in _tail_jsonl(_runtime_paths()["trades"])
        if x.get("status") == "FILLED_TESTNET"
    ]


def _latest_checkpoint_event():
    try:
        with SessionLocal() as db:
            return db.scalar(
                select(Event)
                .where(Event.event_type == CHECKPOINT_EVENT)
                .order_by(Event.created_at.desc())
                .limit(1)
            )
    except Exception:
        return None


def _restore_testnet_checkpoint():
    event = _latest_checkpoint_event()
    if not event:
        return None
    try:
        payload = json.loads(event.payload_json or "{}")
        encoded = payload.get("runtime_gzip_b64")
        if not encoded:
            return None
        text = gzip.decompress(base64.b64decode(encoded)).decode("utf-8")
        json.loads(text)
        paths = _runtime_paths()
        paths["storage"].mkdir(parents=True, exist_ok=True)
        paths["runtime"].write_text(text, encoding="utf-8")
        status = payload.get("status")
        if isinstance(status, dict):
            _write_json(paths["status"], status)
        return {
            "state_id": payload.get("state_id"),
            "saved_at": payload.get("saved_at"),
        }
    except Exception:
        return None


def _persist_testnet_checkpoint(force=False):
    global _last_checkpoint_state
    paths = _runtime_paths()
    if not paths["runtime"].exists():
        return False

    status = _read_json(paths["status"], {})
    current = status.get("current_state") or {}
    state_id = str(current.get("state_id") or "")
    if not state_id:
        return False
    if not force and state_id == _last_checkpoint_state:
        return False

    try:
        runtime_text = paths["runtime"].read_text(encoding="utf-8")
        json.loads(runtime_text)
        payload = {
            "version": "2.9",
            "state_id": state_id,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "runtime_gzip_b64": base64.b64encode(
                gzip.compress(runtime_text.encode("utf-8"), 6)
            ).decode("ascii"),
        }
        with SessionLocal() as db:
            db.add(Event(
                event_type=CHECKPOINT_EVENT,
                payload_json=json.dumps(payload, ensure_ascii=False),
            ))
            db.flush()
            old_rows = db.scalars(
                select(Event)
                .where(Event.event_type == CHECKPOINT_EVENT)
                .order_by(Event.created_at.desc())
                .offset(24)
            ).all()
            for old in old_rows:
                db.delete(old)
            db.commit()
        _last_checkpoint_state = state_id
        return True
    except Exception:
        return False


def _persist_new_trades():
    rows = _testnet_fills()
    if not rows:
        return 0
    added = 0
    try:
        with SessionLocal() as db:
            for row in rows:
                key = _trade_key(row)
                if key in _persisted_trade_keys:
                    continue
                db.add(Event(
                    event_type=TRADE_EVENT,
                    payload_json=json.dumps(row, ensure_ascii=False),
                ))
                _persisted_trade_keys.add(key)
                added += 1
            if added:
                db.commit()
    except Exception:
        return 0
    return added


def _clear_runtime_storage():
    paths = _runtime_paths()
    if paths["storage"].exists():
        shutil.rmtree(paths["storage"])
    paths["storage"].mkdir(parents=True, exist_ok=True)


def _prepare_testnet_runtime():
    source = TESTNET_RUNTIME / "app.py"
    if not source.exists():
        raise RuntimeError(f"Observer runtime missing: {source}")

    wrapper = r"""
import asyncio
import os
import app as mor

BASELINE_BTC = max(0.0, float(os.getenv("MOR_TESTNET_BASELINE_BTC", "0")))
_original_governor = mor.exchange_risk_governor

def session_isolated_governor(action, market_price, mode=None):
    mode = (mode or mor.EXECUTION_MODE).upper()
    if mode != "TESTNET" or str(action).upper() != "SELL":
        return _original_governor(action, market_price, mode=mode)

    if not mor.execution_runtime.get("preflight_ok", False):
        return {"allowed": False, "reason": "EXCHANGE_PREFLIGHT_FAILED", "notional_usdt": 0.0}

    now_ms = int(mor.datetime.now(mor.timezone.utc).timestamp() * 1000)
    last_ms = int(mor.execution_runtime.get("last_order_epoch_ms", 0) or 0)
    if last_ms and (now_ms - last_ms) < mor.EXCHANGE_ORDER_COOLDOWN_SECONDS * 1000:
        return {"allowed": False, "reason": "ORDER_COOLDOWN", "notional_usdt": 0.0}

    try:
        account = mor.binance_account("TESTNET")
        usdt = mor.binance_free_balance(account, "USDT")
        btc = mor.binance_free_balance(account, "BTC")
        min_notional = mor.binance_symbol_min_notional("TESTNET")
    except Exception as exc:
        return {"allowed": False, "reason": "ACCOUNT_READ_FAILED: " + str(exc)[:160], "notional_usdt": 0.0}

    session_btc = max(0.0, btc - BASELINE_BTC)
    notional = max(0.0, session_btc * float(market_price) * 0.995)
    if notional + 1e-9 < min_notional:
        return {"allowed": False, "reason": "NO_SESSION_TESTNET_POSITION", "notional_usdt": 0.0}

    return {
        "allowed": True,
        "reason": "SESSION_POSITION_CLOSE",
        "notional_usdt": round(notional, 8),
        "free_usdt": round(usdt, 8),
        "free_btc": round(btc, 12),
        "session_btc": round(session_btc, 12),
        "min_notional": round(min_notional, 8),
    }

mor.exchange_risk_governor = session_isolated_governor

if __name__ == "__main__":
    try:
        asyncio.run(mor.main())
    except KeyboardInterrupt:
        mor.save_runtime()
        print("Testnet runtime saved.")
"""
    (TESTNET_RUNTIME / "run_testnet.py").write_text(wrapper.strip() + "\n", encoding="utf-8")


def _switch_from_main_to_testnet():
    # Preserve main-market runtime before replacing the ephemeral storage tree.
    if oc._process_info().get("alive"):
        # META still says PAPER/LIVE here, so the guarded writer delegates.
        _original_main_persist(force=True)
        ok, msg = oc._stop_process(reason="Switching Observer to Binance Spot Testnet.")
        if not ok:
            raise RuntimeError(msg)
        time.sleep(0.4)
    else:
        _original_main_persist(force=True)


def _restore_main_storage_after_testnet():
    # Main-market data lives durably in observer_runtime_checkpoints.
    try:
        return oc._restore_latest_checkpoint()
    except Exception:
        return None


def _start_testnet(max_order, max_fills, max_minutes):
    if not _creds_ready():
        return False, "Testnet keys are missing in Render."

    account = _account()
    if not account.get("ok"):
        return False, "Testnet preflight failed: " + str(account.get("reason", "unknown"))

    try:
        _switch_from_main_to_testnet()
        _clear_runtime_storage()
        restored = _restore_testnet_checkpoint()
        _prepare_testnet_runtime()
    except Exception as exc:
        _restore_main_storage_after_testnet()
        return False, f"Testnet runtime preparation failed: {type(exc).__name__}: {exc}"

    baseline = _account()
    cfg = {
        "max_order_usdt": float(max_order),
        "max_fills": int(max_fills),
        "max_minutes": int(max_minutes),
        "baseline_btc": float(baseline.get("btc", 0) or 0),
        "baseline_usdt": float(baseline.get("usdt", 0) or 0),
        "baseline_equity": float(baseline.get("equity_usdt", 0) or 0),
        "baseline_fill_count": len(_testnet_fills()),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }

    env = os.environ.copy()
    key, secret = _creds()
    env["BINANCE_API_KEY"] = key
    env["BINANCE_API_SECRET"] = secret
    env["MOR_EXECUTION_MODE"] = "TESTNET"
    env["MOR_LIVE_ARM"] = ""
    env["MOR_TESTNET_RELAX_GATES"] = "0"
    env["MOR_TESTNET_GEOMETRY_ACTIONS"] = "0"
    env["MOR_TESTNET_ACTION_ARBITRATION"] = "0"
    env["MOR_MARKET_REST_URL"] = TESTNET_KLINES
    env["MOR_MARKET_WS_URL"] = TESTNET_WS
    env["MOR_MAX_ORDER_USDT"] = str(float(max_order))
    env["MOR_ORDER_COOLDOWN_SECONDS"] = "60"
    env["MOR_OBSERVER_STATUS_FILE"] = "storage/observer_status.json"
    env["MOR_EXPORT_DOWNLOAD_PATH"] = "storage/MOR_latest_export.json"
    env["MOR_EXPORT_FULL_DOWNLOAD_PATH"] = "storage/MOR_export_full.json"
    env["MOR_TESTNET_BASELINE_BTC"] = str(cfg["baseline_btc"])
    env["MOR_GAP_MAX_MINUTES"] = "120"
    env["PYTHONUNBUFFERED"] = "1"

    TESTNET_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_handle = open(TESTNET_LOG, "ab", buffering=0)
    try:
        log_handle.write(
            ("\n=== UNIVERSE LAB · BINANCE SPOT TESTNET v2.9 ===\n").encode("utf-8")
        )
        proc = subprocess.Popen(
            [sys.executable, "-u", "run_testnet.py"],
            cwd=str(TESTNET_RUNTIME),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    except Exception as exc:
        log_handle.close()
        _restore_main_storage_after_testnet()
        return False, f"Failed to start Testnet Observer: {type(exc).__name__}: {exc}"

    meta = {
        **cfg,
        "pid": proc.pid,
        "profile": "TESTNET_LIVE",
        "runtime_dir": str(TESTNET_RUNTIME),
        "restored": restored,
    }
    _write_json(TESTNET_META, meta)

    oc.PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    _write_json(oc.META_FILE, {
        "pid": proc.pid,
        "started_at": cfg["started_at"],
        "command": [sys.executable, "-u", "run_testnet.py"],
        "runtime_dir": str(TESTNET_RUNTIME),
        "profile": "TESTNET_LIVE",
        "canary": {
            "max_order_usdt": float(max_order),
            "max_trades": int(max_fills),
            "max_minutes": int(max_minutes),
        },
        "restored_checkpoint": restored,
    })

    _ensure_watchdog()
    note = f" Restored {restored.get('state_id')} from Testnet DB." if restored else ""
    return True, f"Binance Spot Testnet started (PID {proc.pid}).{note}"


def _kill_testnet():
    m = _meta()
    pid = m.get("pid")
    if not _pid_alive(pid):
        return True, "Testnet Observer is already stopped."
    try:
        try:
            os.killpg(os.getpgid(int(pid)), signal.SIGINT)
        except Exception:
            os.kill(int(pid), signal.SIGINT)

        deadline = time.time() + 10
        while time.time() < deadline and _pid_alive(pid):
            time.sleep(0.2)

        if _pid_alive(pid):
            try:
                os.killpg(os.getpgid(int(pid)), signal.SIGKILL)
            except Exception:
                os.kill(int(pid), signal.SIGKILL)

        _persist_testnet_checkpoint(force=True)
        _persist_new_trades()
        try:
            oc.PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        m["stopped_at"] = datetime.now(timezone.utc).isoformat()
        m["pid"] = None
        _write_json(TESTNET_META, m)
        return True, "Testnet Observer stopped."
    except Exception as exc:
        return False, f"Stop failed: {type(exc).__name__}: {exc}"


def _stop_and_restore_main(reason="STOP"):
    close_ok, close_message = _flatten(reason + "_FLATTEN")
    stop_ok, stop_message = _kill_testnet()
    restored = _restore_main_storage_after_testnet()
    # Mark process profile as stopped PAPER so the old LIVE CANARY card cannot
    # misrepresent the just-finished Testnet session.
    _write_json(oc.META_FILE, {
        "pid": None,
        "started_at": None,
        "command": oc._configured_command(),
        "runtime_dir": str(oc._runtime_dir()),
        "profile": "PAPER",
        "canary": {},
        "restored_checkpoint": restored,
    })
    return close_ok and stop_ok, f"{close_message} {stop_message}"


def _flatten(reason):
    if not _creds_ready():
        return False, "Testnet keys missing."
    m = _meta()
    baseline_btc = float(m.get("baseline_btc", 0) or 0)

    account = _account()
    if not account.get("ok"):
        return False, "Account read failed: " + str(account.get("reason", "unknown"))
    session_btc = max(0.0, float(account.get("btc", 0) or 0) - baseline_btc)
    price = float(account.get("price", 0) or 0)
    rules = _exchange_rules()
    qty = _floor_step(session_btc, float(rules["step"]))
    if qty <= 0 or qty * price < float(rules["min_notional"]):
        return True, "No bot-opened Testnet BTC position to close."

    qty_text = f"{qty:.8f}".rstrip("0").rstrip(".")
    params = {
        "symbol": "BTCUSDT",
        "side": "SELL",
        "type": "MARKET",
        "quantity": qty_text,
        "newOrderRespType": "FULL",
    }
    _api("POST", "/api/v3/order/test", params, signed=True)
    result = _api("POST", "/api/v3/order", params, signed=True)
    row = {
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
    paths = _runtime_paths()
    paths["storage"].mkdir(parents=True, exist_ok=True)
    with paths["trades"].open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    _persist_new_trades()
    return True, f"Closed Testnet position: {qty_text} BTC."


def _status():
    m = _meta()
    account = _account()
    alive = _testnet_alive()
    fills_all = _testnet_fills()
    baseline_fill_count = int(m.get("baseline_fill_count", 0) or 0)
    session_fills = max(0, len(fills_all) - baseline_fill_count)
    baseline_btc = float(m.get("baseline_btc", 0) or 0)
    bot_btc = None
    bot_value = None
    pnl = None
    if account.get("ok"):
        bot_btc = max(0.0, float(account.get("btc", 0) or 0) - baseline_btc)
        bot_value = bot_btc * float(account.get("price", 0) or 0)
        if m.get("baseline_equity") is not None:
            pnl = float(account.get("equity_usdt", 0) or 0) - float(m.get("baseline_equity", 0) or 0)

    status_file = _read_json(_runtime_paths()["status"], {})
    current = status_file.get("current_state") or {}
    latest_trade = fills_all[-1] if fills_all else None

    return {
        **account,
        "active": alive,
        "profile": "TESTNET_LIVE" if alive else "OFF",
        "max_order_usdt": m.get("max_order_usdt"),
        "max_fills": m.get("max_fills"),
        "max_minutes": m.get("max_minutes"),
        "fills": session_fills,
        "bot_position_btc": bot_btc,
        "bot_position_value_usdt": bot_value,
        "position_open": bool(bot_value is not None and bot_value >= 5.0),
        "session_pnl_usdt": pnl,
        "state_id": current.get("state_id"),
        "strategy": current.get("chosen_strategy"),
        "action": current.get("action"),
        "latest_trade": latest_trade,
        "restored": m.get("restored"),
    }


def _watchdog():
    while not _watch_stop.wait(2.0):
        if not _testnet_alive():
            _persist_testnet_checkpoint(force=True)
            _persist_new_trades()
            break

        _persist_testnet_checkpoint()
        _persist_new_trades()
        m = _meta()
        started = m.get("started_at")
        try:
            started_dt = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
            elapsed = (datetime.now(timezone.utc) - started_dt).total_seconds() / 60.0
        except Exception:
            elapsed = 0.0

        fills = max(0, len(_testnet_fills()) - int(m.get("baseline_fill_count", 0) or 0))
        max_fills = int(m.get("max_fills", 0) or 0)
        max_minutes = int(m.get("max_minutes", 0) or 0)

        if max_fills and fills >= max_fills:
            _stop_and_restore_main("AUTO_MAX_FILLS")
            break
        if max_minutes and elapsed >= max_minutes:
            _stop_and_restore_main("AUTO_TIME_LIMIT")
            break


def _ensure_watchdog():
    global _watch_thread
    if _watch_thread and _watch_thread.is_alive():
        return
    _watch_stop.clear()
    _watch_thread = threading.Thread(
        target=_watchdog,
        name="observer-testnet-v29",
        daemon=True,
    )
    _watch_thread.start()


@bp.get("/api/status")
def status_api():
    oc._require_admin()
    return jsonify({"ok": True, "testnet": _status()})


@bp.post("/api/start")
def start_api():
    _require()
    data = request.get_json(silent=True) or {}
    if str(data.get("confirm", "")).strip().upper() != "TESTNET":
        return jsonify({"ok": False, "message": "Type TESTNET to start fake-money exchange trading."}), 400
    try:
        max_order = float(data.get("max_order_usdt"))
        max_fills = int(data.get("max_fills"))
        max_minutes = int(data.get("max_minutes"))
    except Exception:
        return jsonify({"ok": False, "message": "Fill max order, fills and duration."}), 400

    if not (1 <= max_order <= MAX_ORDER_HARD):
        return jsonify({"ok": False, "message": f"Max order must be 1..{MAX_ORDER_HARD:.0f} USDT."}), 400
    if not (1 <= max_fills <= MAX_FILLS_HARD):
        return jsonify({"ok": False, "message": f"Max fills must be 1..{MAX_FILLS_HARD}."}), 400
    if not (5 <= max_minutes <= MAX_MINUTES_HARD):
        return jsonify({"ok": False, "message": f"Duration must be 5..{MAX_MINUTES_HARD} minutes."}), 400

    ok, message = _start_testnet(max_order, max_fills, max_minutes)
    return jsonify({"ok": ok, "message": message, "testnet": _status()}), (200 if ok else 409)


@bp.post("/api/close")
def close_api():
    _require()
    ok, message = _flatten("MANUAL_CLOSE")
    return jsonify({"ok": ok, "message": message, "testnet": _status()}), (200 if ok else 409)


@bp.post("/api/stop")
def stop_api():
    _require()
    ok, message = _stop_and_restore_main("MANUAL_STOP")
    return jsonify({"ok": ok, "message": message, "testnet": _status()}), (200 if ok else 409)

# ===== Observer Testnet Remote Relay v3.0 START =====
RELAY_URL = os.getenv("OBSERVER_TESTNET_RELAY_URL", "").strip().rstrip("/")
RELAY_SECRET = os.getenv("OBSERVER_TESTNET_RELAY_SECRET", "").strip()
RELAY_AUTH_SKEW_SECONDS = max(
    15,
    int(os.getenv("OBSERVER_TESTNET_RELAY_AUTH_SKEW_SECONDS", "60")),
)

_local_status_v29 = _status
_local_start_testnet_v29 = _start_testnet
_local_flatten_v29 = _flatten
_local_stop_restore_v29 = _stop_and_restore_main
_local_creds_ready_v29 = _creds_ready
_local_testnet_alive_v29 = _testnet_alive


def _relay_enabled():
    return bool(RELAY_URL and RELAY_SECRET)


def _relay_digest(body):
    return hashlib.sha256(body or b"").hexdigest()


def _relay_signature(timestamp, method, path, body):
    canonical = "\n".join(
        [
            str(timestamp),
            method.upper(),
            path,
            _relay_digest(body),
        ]
    )
    return hmac.new(
        RELAY_SECRET.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _relay_call(method, path, payload=None, timeout=20):
    if not _relay_enabled():
        raise RuntimeError(
            "OBSERVER_TESTNET_RELAY_URL / OBSERVER_TESTNET_RELAY_SECRET missing"
        )

    body = b""
    if payload is not None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

    timestamp = str(int(time.time()))
    signature = _relay_signature(
        timestamp,
        method,
        path,
        body,
    )
    url = RELAY_URL + path
    req = urllib.request.Request(
        url,
        data=(body if method.upper() != "GET" else None),
        method=method.upper(),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "UniverseLab-Control/3.1",
            "Authorization": "Bearer " + RELAY_SECRET,
            "X-Relay-Timestamp": timestamp,
            "X-Relay-Signature": signature,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(
            "utf-8",
            errors="replace",
        )[:900]
        raise RuntimeError(
            f"Relay HTTP {exc.code}: {detail}"
        ) from exc


def _relay_health_debug():
    out = {
        "local_fingerprint": (
            hashlib.sha256(RELAY_SECRET.encode("utf-8")).hexdigest()[:10]
            if RELAY_SECRET
            else "missing"
        ),
        "remote_fingerprint": None,
        "health_ok": False,
        "health_reason": None,
    }
    if not RELAY_URL:
        out["health_reason"] = "relay URL missing"
        return out

    try:
        req = urllib.request.Request(
            RELAY_URL + "/health",
            headers={
                "Accept": "application/json",
                "User-Agent": "UniverseLab-Control/3.1",
            },
        )
        with urllib.request.urlopen(req, timeout=12) as response:
            data = json.loads(response.read().decode("utf-8"))
        out["health_ok"] = bool(data.get("ok"))
        out["remote_fingerprint"] = data.get("auth_fingerprint")
        out["health_reason"] = (
            (data.get("binance_testnet") or {}).get("reason")
            if not data.get("ok")
            else None
        )
    except Exception as exc:
        out["health_reason"] = f"{type(exc).__name__}: {exc}"[:300]
    return out


def _latest_relay_checkpoint_payload():
    try:
        event = _latest_checkpoint_event()
        if not event:
            return None
        payload = json.loads(event.payload_json or "{}")
        return payload if payload.get("runtime_gzip_b64") else None
    except Exception:
        return None


def _relay_verify_inbound():
    if not RELAY_SECRET:
        return False, "relay secret missing"

    auth = request.headers.get("Authorization", "")
    expected_bearer = "Bearer " + RELAY_SECRET
    if hmac.compare_digest(auth, expected_bearer):
        return True, "bearer"

    timestamp = request.headers.get("X-Relay-Timestamp", "")
    signature = request.headers.get("X-Relay-Signature", "")
    try:
        ts = int(timestamp)
    except Exception:
        return False, "invalid timestamp"

    if abs(int(time.time()) - ts) > RELAY_AUTH_SKEW_SECONDS:
        return False, "stale callback"

    expected = _relay_signature(
        timestamp,
        request.method,
        request.path,
        request.get_data(cache=True) or b"",
    )
    if not hmac.compare_digest(expected, signature):
        return False, "invalid callback signature"
    return True, "ok"


@bp.post("/relay/ingest")
def relay_ingest_api():
    ok, reason = _relay_verify_inbound()
    if not ok:
        return jsonify(
            {"ok": False, "error": "unauthorized", "reason": reason}
        ), 401

    data = request.get_json(silent=True) or {}
    kind = str(data.get("kind") or "")
    key = str(data.get("idempotency_key") or "")[:40]
    payload = data.get("payload")
    if kind not in ("checkpoint", "trade") or not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "invalid payload"}), 400

    event_type = CHECKPOINT_EVENT if kind == "checkpoint" else TRADE_EVENT
    if not key:
        key = hashlib.sha1(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    try:
        with SessionLocal() as db:
            existing = db.scalar(
                select(Event.id)
                .where(
                    Event.event_type == event_type,
                    Event.run_id == key,
                )
                .limit(1)
            )
            if existing:
                return jsonify({"ok": True, "duplicate": True})

            db.add(
                Event(
                    run_id=key,
                    event_type=event_type,
                    payload_json=json.dumps(
                        payload,
                        ensure_ascii=False,
                    ),
                )
            )
            db.commit()
        return jsonify({"ok": True, "duplicate": False})
    except Exception as exc:
        return jsonify(
            {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}"[:400],
            }
        ), 500


def _creds_ready():
    if _relay_enabled():
        return True
    return _local_creds_ready_v29()


def _testnet_alive():
    if not _relay_enabled():
        return _local_testnet_alive_v29()
    try:
        data = _relay_call(
            "GET",
            "/v1/testnet/status",
            timeout=10,
        )
        return bool(
            (data.get("testnet") or {}).get("active")
        )
    except Exception:
        return False


def _status():
    if not _relay_enabled():
        local = _local_status_v29()
        local["relay"] = False
        local["relay_configured"] = False
        return local

    try:
        data = _relay_call(
            "GET",
            "/v1/testnet/status",
            timeout=12,
        )
        status = dict(data.get("testnet") or {})
        status["relay"] = True
        status["relay_configured"] = True
        status["relay_url"] = RELAY_URL
        return status
    except Exception as exc:
        dbg = _relay_health_debug()
        local_fp = dbg.get("local_fingerprint")
        remote_fp = dbg.get("remote_fingerprint")
        if remote_fp and local_fp != remote_fp:
            diagnostic = (
                f" SECRET_MISMATCH main={local_fp} relay={remote_fp}. "
                "OBSERVER_TESTNET_RELAY_SECRET and RELAY_SHARED_SECRET are different."
            )
        elif remote_fp and local_fp == remote_fp:
            diagnostic = " SECRET_MATCH. Bearer/HMAC auth should pass after both v3.1 deploys."
        else:
            diagnostic = f" RELAY_HEALTH={dbg.get('health_reason') or 'unavailable'}."

        return {
            "ok": False,
            "credentials_ready": True,
            "active": False,
            "profile": "OFF",
            "relay": True,
            "relay_configured": True,
            "relay_url": RELAY_URL,
            "reason": (
                f"RELAY_ERROR: {type(exc).__name__}: {exc}{diagnostic}"
            )[:1200],
            "auth_local_fingerprint": local_fp,
            "auth_remote_fingerprint": remote_fp,
            "fills": 0,
        }


def _start_testnet(max_order, max_fills, max_minutes):
    if not _relay_enabled():
        return _local_start_testnet_v29(
            max_order,
            max_fills,
            max_minutes,
        )

    try:
        _switch_from_main_to_testnet()
        checkpoint = _latest_relay_checkpoint_payload()
        data = _relay_call(
            "POST",
            "/v1/testnet/start",
            {
                "max_order_usdt": float(max_order),
                "max_fills": int(max_fills),
                "max_minutes": int(max_minutes),
                "checkpoint": checkpoint,
            },
            timeout=30,
        )
        if not data.get("ok"):
            _restore_main_storage_after_testnet()
            return False, str(data.get("message") or "Relay start blocked.")

        _write_json(
            oc.META_FILE,
            {
                "pid": None,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "command": ["REMOTE", RELAY_URL],
                "runtime_dir": str(oc._runtime_dir()),
                "profile": "TESTNET_REMOTE",
                "canary": {
                    "max_order_usdt": float(max_order),
                    "max_trades": int(max_fills),
                    "max_minutes": int(max_minutes),
                },
                "restored_checkpoint": (
                    checkpoint.get("state_id")
                    if isinstance(checkpoint, dict)
                    else None
                ),
            },
        )
        return True, str(
            data.get("message")
            or "Frankfurt Testnet relay started."
        )
    except Exception as exc:
        _restore_main_storage_after_testnet()
        return (
            False,
            f"Relay start failed: {type(exc).__name__}: {exc}",
        )


def _flatten(reason):
    if not _relay_enabled():
        return _local_flatten_v29(reason)

    try:
        data = _relay_call(
            "POST",
            "/v1/testnet/close",
            {"reason": reason},
            timeout=25,
        )
        return bool(data.get("ok")), str(
            data.get("message") or "Relay close finished."
        )
    except Exception as exc:
        return (
            False,
            f"Relay close failed: {type(exc).__name__}: {exc}",
        )


def _stop_and_restore_main(reason="STOP"):
    if not _relay_enabled():
        return _local_stop_restore_v29(reason)

    try:
        data = _relay_call(
            "POST",
            "/v1/testnet/stop",
            {"reason": reason},
            timeout=30,
        )
        relay_ok = bool(data.get("ok"))
        relay_message = str(
            data.get("message") or "Relay stopped."
        )
    except Exception as exc:
        relay_ok = False
        relay_message = (
            f"Relay stop failed: {type(exc).__name__}: {exc}"
        )

    restored = _restore_main_storage_after_testnet()
    _write_json(
        oc.META_FILE,
        {
            "pid": None,
            "started_at": None,
            "command": oc._configured_command(),
            "runtime_dir": str(oc._runtime_dir()),
            "profile": "PAPER",
            "canary": {},
            "restored_checkpoint": restored,
        },
    )
    return relay_ok, relay_message
# ===== Observer Testnet Remote Relay v3.0 END =====
