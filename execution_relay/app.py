import base64
import gzip
import hashlib
import hmac
import json
import math
import os
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

from flask import Flask, jsonify, request

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "observer_runtime"
RUNNER = ROOT / "execution_relay" / "run_testnet_runtime.py"
STORAGE = RUNTIME / "storage"
RUNTIME_STATE = STORAGE / "runtime_state.json"
STATUS_FILE = STORAGE / "observer_status.json"
TRADES_FILE = STORAGE / "exchange_trades_erl1.jsonl"
LOG_FILE = Path(os.getenv("RELAY_OBSERVER_LOG_FILE", "/tmp/universe_observer_relay.log"))
META_FILE = Path(os.getenv("RELAY_META_FILE", "/tmp/universe_observer_relay_meta.json"))

TESTNET_BASE = "https://testnet.binance.vision"
TESTNET_KLINES = TESTNET_BASE + "/api/v3/klines"
TESTNET_WS = "wss://stream.testnet.binance.vision/ws/btcusdt@kline_1m"

RELAY_VERSION = "3.0"
RELAY_REGION = os.getenv("RELAY_REGION", "frankfurt")
SHARED_SECRET = os.getenv("RELAY_SHARED_SECRET", "").strip()
CALLBACK_URL = os.getenv("CONTROL_CALLBACK_URL", "").strip()
TESTNET_API_KEY = os.getenv("BINANCE_TESTNET_API_KEY", "").strip()
TESTNET_API_SECRET = os.getenv("BINANCE_TESTNET_API_SECRET", "").strip()

MAX_ORDER_HARD = max(1.0, float(os.getenv("RELAY_MAX_ORDER_USDT", "100")))
MAX_FILLS_HARD = max(1, int(os.getenv("RELAY_MAX_FILLS", "100")))
MAX_MINUTES_HARD = max(10, int(os.getenv("RELAY_MAX_MINUTES", "360")))
AUTH_SKEW_SECONDS = max(15, int(os.getenv("RELAY_AUTH_SKEW_SECONDS", "60")))

app = Flask(__name__)

_watch_thread = None
_watch_stop = threading.Event()
_last_checkpoint_fingerprint = None
_sent_trade_keys = set()
_callback_error = None
_state_lock = threading.RLock()


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {} if default is None else default


def _write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


def _meta():
    return _read_json(META_FILE, {})


def _set_meta(obj):
    _write_json(META_FILE, obj)


def _pid_alive(pid):
    try:
        pid = int(pid or 0)
        if pid <= 1:
            return False
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _alive():
    return _pid_alive(_meta().get("pid"))


def _tail_jsonl(path):
    rows = []
    try:
        for line in Path(path).read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines():
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
    raw = "|".join(
        str(row.get(k, ""))
        for k in ("order_id", "time", "state_id", "action", "status")
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _testnet_fills():
    return [
        row
        for row in _tail_jsonl(TRADES_FILE)
        if row.get("status") == "FILLED_TESTNET"
    ]


def _request_digest(body):
    return hashlib.sha256(body or b"").hexdigest()


def _signature(secret, timestamp, method, path, body):
    canonical = "\n".join(
        [
            str(timestamp),
            method.upper(),
            path,
            _request_digest(body),
        ]
    )
    return hmac.new(
        secret.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _auth_ok():
    if not SHARED_SECRET:
        return False, "RELAY_SHARED_SECRET missing"

    timestamp = request.headers.get("X-Relay-Timestamp", "")
    signature = request.headers.get("X-Relay-Signature", "")
    try:
        ts = int(timestamp)
    except Exception:
        return False, "invalid timestamp"

    if abs(int(time.time()) - ts) > AUTH_SKEW_SECONDS:
        return False, "stale request"

    expected = _signature(
        SHARED_SECRET,
        timestamp,
        request.method,
        request.path,
        request.get_data(cache=True) or b"",
    )
    if not hmac.compare_digest(expected, signature):
        return False, "invalid signature"
    return True, "ok"


@app.before_request
def _protect_v1():
    if not request.path.startswith("/v1/"):
        return None
    ok, reason = _auth_ok()
    if not ok:
        return jsonify({"ok": False, "error": "unauthorized", "reason": reason}), 401
    return None


def _api(method, path, params=None, signed=False):
    params = dict(params or {})
    if signed and not (TESTNET_API_KEY and TESTNET_API_SECRET):
        raise RuntimeError("Testnet API credentials missing on relay")

    if signed:
        req = urllib.request.Request(
            TESTNET_BASE + "/api/v3/time",
            headers={"User-Agent": "UniverseLab-Relay/3.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            params["timestamp"] = int(
                json.loads(response.read().decode("utf-8"))["serverTime"]
            )
        params["recvWindow"] = 5000

    query = urllib.parse.urlencode(params)
    if signed:
        signature = hmac.new(
            TESTNET_API_SECRET.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        query = (
            query + "&signature=" + signature
            if query
            else "signature=" + signature
        )

    url = TESTNET_BASE + path
    body = None
    if method.upper() == "GET":
        if query:
            url += "?" + query
    else:
        body = query.encode("utf-8")

    headers = {"User-Agent": "UniverseLab-Relay/3.0"}
    if signed:
        headers["X-MBX-APIKEY"] = TESTNET_API_KEY
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    req = urllib.request.Request(
        url,
        data=body,
        method=method.upper(),
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(
            "utf-8",
            errors="replace",
        )[:700]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


def _connectivity():
    try:
        data = _api("GET", "/api/v3/time")
        return {
            "ok": True,
            "server_time": data.get("serverTime"),
        }
    except Exception as exc:
        return {
            "ok": False,
            "reason": f"{type(exc).__name__}: {exc}"[:700],
        }


def _account():
    if not (TESTNET_API_KEY and TESTNET_API_SECRET):
        return {
            "ok": False,
            "credentials_ready": False,
            "reason": "TESTNET_KEYS_MISSING_ON_RELAY",
        }
    try:
        account = _api("GET", "/api/v3/account", signed=True)
        ticker = _api(
            "GET",
            "/api/v3/ticker/price",
            {"symbol": "BTCUSDT"},
        )
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
    info = _api(
        "GET",
        "/api/v3/exchangeInfo",
        {"symbol": "BTCUSDT"},
    )
    symbols = info.get("symbols") or []
    if not symbols:
        raise RuntimeError("BTCUSDT missing from Testnet exchangeInfo")
    filters = {
        item.get("filterType"): item
        for item in symbols[0].get("filters", [])
    }
    lot = filters.get("LOT_SIZE") or {}
    notional = (
        filters.get("NOTIONAL")
        or filters.get("MIN_NOTIONAL")
        or {}
    )
    return {
        "step": float(lot.get("stepSize", "0.00000001")),
        "min_qty": float(lot.get("minQty", "0")),
        "min_notional": float(notional.get("minNotional", "0")),
    }


def _floor_step(value, step):
    if step <= 0:
        return value
    return math.floor((value + 1e-15) / step) * step


def _clear_storage():
    if STORAGE.exists():
        import shutil

        shutil.rmtree(STORAGE)
    STORAGE.mkdir(parents=True, exist_ok=True)


def _restore_checkpoint(payload):
    if not isinstance(payload, dict):
        return None
    encoded = payload.get("runtime_gzip_b64")
    if not encoded:
        return None

    try:
        text = gzip.decompress(
            base64.b64decode(encoded)
        ).decode("utf-8")
        json.loads(text)
        STORAGE.mkdir(parents=True, exist_ok=True)
        RUNTIME_STATE.write_text(text, encoding="utf-8")

        status = payload.get("status")
        if isinstance(status, dict):
            _write_json(STATUS_FILE, status)

        return {
            "state_id": payload.get("state_id"),
            "saved_at": payload.get("saved_at"),
            "relay_session": payload.get("relay_session"),
        }
    except Exception:
        return None


def _callback(kind, payload, idempotency_key):
    global _callback_error

    if not CALLBACK_URL:
        _callback_error = "CONTROL_CALLBACK_URL missing"
        return False

    body_obj = {
        "version": RELAY_VERSION,
        "kind": kind,
        "idempotency_key": idempotency_key,
        "payload": payload,
        "relay_region": RELAY_REGION,
        "relay_time": _now_iso(),
    }
    body = json.dumps(
        body_obj,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    parsed = urllib.parse.urlsplit(CALLBACK_URL)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    timestamp = str(int(time.time()))
    signature = _signature(
        SHARED_SECRET,
        timestamp,
        "POST",
        parsed.path or "/",
        body,
    )
    req = urllib.request.Request(
        CALLBACK_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "UniverseLab-Relay/3.0",
            "X-Relay-Timestamp": timestamp,
            "X-Relay-Signature": signature,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            if int(response.status) >= 300:
                raise RuntimeError(f"callback HTTP {response.status}")
        _callback_error = None
        return True
    except Exception as exc:
        _callback_error = f"{type(exc).__name__}: {exc}"[:500]
        return False


def _persist_checkpoint(force=False):
    global _last_checkpoint_fingerprint

    if not RUNTIME_STATE.exists():
        return False

    status = _read_json(STATUS_FILE, {})
    current = status.get("current_state") or {}
    state_id = str(current.get("state_id") or "")
    if not state_id:
        return False

    try:
        runtime_text = RUNTIME_STATE.read_text(encoding="utf-8")
        json.loads(runtime_text)
    except Exception:
        return False

    fingerprint = hashlib.sha256(
        runtime_text.encode("utf-8")
    ).hexdigest()[:24]
    unique = f"{state_id}:{fingerprint}"
    if not force and unique == _last_checkpoint_fingerprint:
        return False

    meta = _meta()
    safe_session = {
        key: meta.get(key)
        for key in (
            "baseline_btc",
            "baseline_usdt",
            "baseline_equity",
            "baseline_fill_count",
            "max_order_usdt",
            "max_fills",
            "max_minutes",
            "started_at",
        )
    }
    payload = {
        "version": RELAY_VERSION,
        "state_id": state_id,
        "saved_at": _now_iso(),
        "status": status,
        "runtime_gzip_b64": base64.b64encode(
            gzip.compress(runtime_text.encode("utf-8"), 6)
        ).decode("ascii"),
        "relay_session": safe_session,
    }
    key = hashlib.sha1(
        ("checkpoint|" + unique).encode("utf-8")
    ).hexdigest()

    if _callback("checkpoint", payload, key):
        _last_checkpoint_fingerprint = unique
        return True
    return False


def _persist_new_trades():
    added = 0
    for row in _testnet_fills():
        key = _trade_key(row)
        if key in _sent_trade_keys:
            continue
        if _callback("trade", row, key):
            _sent_trade_keys.add(key)
            added += 1
    return added


def _status():
    meta = _meta()
    account = _account()
    alive = _alive()
    fills_all = _testnet_fills()
    baseline_fill_count = int(
        meta.get("baseline_fill_count", 0) or 0
    )
    session_fills = max(
        0,
        len(fills_all) - baseline_fill_count,
    )
    baseline_btc = float(
        meta.get("baseline_btc", 0) or 0
    )

    bot_btc = None
    bot_value = None
    pnl = None
    if account.get("ok"):
        bot_btc = max(
            0.0,
            float(account.get("btc", 0) or 0) - baseline_btc,
        )
        bot_value = (
            bot_btc * float(account.get("price", 0) or 0)
        )
        if meta.get("baseline_equity") is not None:
            pnl = (
                float(account.get("equity_usdt", 0) or 0)
                - float(meta.get("baseline_equity", 0) or 0)
            )

    status_file = _read_json(STATUS_FILE, {})
    current = status_file.get("current_state") or {}
    latest_trade = fills_all[-1] if fills_all else None

    return {
        **account,
        "active": alive,
        "profile": "TESTNET_LIVE" if alive else "OFF",
        "relay": True,
        "relay_version": RELAY_VERSION,
        "relay_region": RELAY_REGION,
        "callback_configured": bool(CALLBACK_URL),
        "callback_error": _callback_error,
        "max_order_usdt": meta.get("max_order_usdt"),
        "max_fills": meta.get("max_fills"),
        "max_minutes": meta.get("max_minutes"),
        "fills": session_fills,
        "bot_position_btc": bot_btc,
        "bot_position_value_usdt": bot_value,
        "position_open": bool(
            bot_value is not None and bot_value >= 5.0
        ),
        "session_pnl_usdt": pnl,
        "state_id": current.get("state_id"),
        "strategy": current.get("chosen_strategy"),
        "action": current.get("action"),
        "latest_trade": latest_trade,
        "restored": meta.get("restored"),
        "started_at": meta.get("started_at"),
    }


def _start(max_order, max_fills, max_minutes, checkpoint=None):
    with _state_lock:
        if _alive():
            return False, "Relay Testnet Observer is already running."

        if not (TESTNET_API_KEY and TESTNET_API_SECRET):
            return False, "Testnet API keys are missing on Frankfurt relay."

        connectivity = _connectivity()
        if not connectivity.get("ok"):
            return (
                False,
                "Frankfurt relay cannot reach Binance Spot Testnet: "
                + str(connectivity.get("reason", "unknown")),
            )

        account = _account()
        if not account.get("ok"):
            return (
                False,
                "Frankfurt relay Testnet preflight failed: "
                + str(account.get("reason", "unknown")),
            )

        _clear_storage()
        restored = _restore_checkpoint(checkpoint)

        baseline_btc = float(account.get("btc", 0) or 0)
        baseline_usdt = float(account.get("usdt", 0) or 0)
        baseline_equity = float(
            account.get("equity_usdt", 0) or 0
        )

        # If a crashed session is being restored and its virtual BTC baseline
        # is still plausible, preserve it so the bot can close its own position.
        if restored and isinstance(
            restored.get("relay_session"),
            dict,
        ):
            old = restored["relay_session"]
            old_base = old.get("baseline_btc")
            try:
                old_base = float(old_base)
                if float(account.get("btc", 0) or 0) + 1e-12 >= old_base:
                    baseline_btc = old_base
                    baseline_usdt = float(
                        old.get("baseline_usdt", baseline_usdt)
                        or baseline_usdt
                    )
                    baseline_equity = float(
                        old.get("baseline_equity", baseline_equity)
                        or baseline_equity
                    )
            except Exception:
                pass

        cfg = {
            "max_order_usdt": float(max_order),
            "max_fills": int(max_fills),
            "max_minutes": int(max_minutes),
            "baseline_btc": baseline_btc,
            "baseline_usdt": baseline_usdt,
            "baseline_equity": baseline_equity,
            "baseline_fill_count": len(_testnet_fills()),
            "started_at": _now_iso(),
            "pid": None,
            "profile": "TESTNET_LIVE",
            "restored": restored,
        }

        env = os.environ.copy()
        env["BINANCE_API_KEY"] = TESTNET_API_KEY
        env["BINANCE_API_SECRET"] = TESTNET_API_SECRET
        env["MOR_EXECUTION_MODE"] = "TESTNET"
        env["MOR_LIVE_ARM"] = ""
        env["MOR_TESTNET_RELAX_GATES"] = "0"
        env["MOR_TESTNET_GEOMETRY_ACTIONS"] = "0"
        env["MOR_TESTNET_ACTION_ARBITRATION"] = "0"
        env["MOR_PREFLIGHT_ORDER_TEST"] = "1"
        env["MOR_MARKET_REST_URL"] = TESTNET_KLINES
        env["MOR_MARKET_WS_URL"] = TESTNET_WS
        env["MOR_MAX_ORDER_USDT"] = str(float(max_order))
        env["MOR_ORDER_COOLDOWN_SECONDS"] = "60"
        env["MOR_OBSERVER_STATUS_FILE"] = "storage/observer_status.json"
        env["MOR_EXPORT_DOWNLOAD_PATH"] = "storage/MOR_latest_export.json"
        env["MOR_EXPORT_FULL_DOWNLOAD_PATH"] = "storage/MOR_export_full.json"
        env["MOR_TESTNET_BASELINE_BTC"] = str(baseline_btc)
        env["MOR_GAP_MAX_MINUTES"] = "120"
        env["PYTHONUNBUFFERED"] = "1"

        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        log_handle = open(LOG_FILE, "ab", buffering=0)
        try:
            log_handle.write(
                (
                    "\n=== UNIVERSE LAB · FRANKFURT TESTNET RELAY v3.0 ===\n"
                ).encode("utf-8")
            )
            proc = subprocess.Popen(
                [sys.executable, "-u", str(RUNNER)],
                cwd=str(RUNTIME),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=env,
            )
        except Exception as exc:
            log_handle.close()
            return (
                False,
                f"Failed to start relay Observer: {type(exc).__name__}: {exc}",
            )

        cfg["pid"] = proc.pid
        _set_meta(cfg)
        _ensure_watchdog()
        note = (
            f" Restored {restored.get('state_id')}."
            if restored
            else ""
        )
        return (
            True,
            f"Frankfurt Testnet relay started (PID {proc.pid}).{note}",
        )


def _kill():
    with _state_lock:
        meta = _meta()
        pid = meta.get("pid")
        if not _pid_alive(pid):
            _persist_checkpoint(force=True)
            _persist_new_trades()
            return True, "Relay Observer is already stopped."

        try:
            try:
                os.killpg(os.getpgid(int(pid)), signal.SIGINT)
            except Exception:
                os.kill(int(pid), signal.SIGINT)

            deadline = time.time() + 12
            while time.time() < deadline and _pid_alive(pid):
                time.sleep(0.2)

            if _pid_alive(pid):
                try:
                    os.killpg(os.getpgid(int(pid)), signal.SIGKILL)
                except Exception:
                    os.kill(int(pid), signal.SIGKILL)

            _persist_checkpoint(force=True)
            _persist_new_trades()
            meta["pid"] = None
            meta["stopped_at"] = _now_iso()
            _set_meta(meta)
            return True, "Relay Observer stopped."
        except Exception as exc:
            return (
                False,
                f"Relay stop failed: {type(exc).__name__}: {exc}",
            )


def _flatten(reason):
    if not (TESTNET_API_KEY and TESTNET_API_SECRET):
        return False, "Testnet keys missing on relay."

    meta = _meta()
    baseline_btc = float(meta.get("baseline_btc", 0) or 0)
    account = _account()
    if not account.get("ok"):
        return (
            False,
            "Relay account read failed: "
            + str(account.get("reason", "unknown")),
        )

    session_btc = max(
        0.0,
        float(account.get("btc", 0) or 0) - baseline_btc,
    )
    price = float(account.get("price", 0) or 0)
    rules = _exchange_rules()
    qty = _floor_step(
        session_btc,
        float(rules["step"]),
    )

    if (
        qty <= 0
        or qty * price < float(rules["min_notional"])
    ):
        return True, "No bot-opened Testnet BTC position to close."

    qty_text = f"{qty:.8f}".rstrip("0").rstrip(".")
    params = {
        "symbol": "BTCUSDT",
        "side": "SELL",
        "type": "MARKET",
        "quantity": qty_text,
        "newOrderRespType": "FULL",
    }

    _api(
        "POST",
        "/api/v3/order/test",
        params,
        signed=True,
    )
    result = _api(
        "POST",
        "/api/v3/order",
        params,
        signed=True,
    )
    row = {
        "time": _now_iso(),
        "state_id": "CONTROL",
        "strategy": "RELAY_POSITION_MANAGER",
        "action": "SELL",
        "status": "FILLED_TESTNET",
        "mode": "TESTNET",
        "reason": reason,
        "order_id": result.get("orderId"),
        "executed_qty": result.get("executedQty"),
        "cummulative_quote_qty": result.get(
            "cummulativeQuoteQty"
        ),
        "exchange_status": result.get("status"),
    }
    STORAGE.mkdir(parents=True, exist_ok=True)
    with TRADES_FILE.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(row, ensure_ascii=False) + "\n"
        )
    _persist_new_trades()
    return True, f"Closed relay Testnet position: {qty_text} BTC."


def _watchdog():
    while not _watch_stop.wait(2.0):
        if not _alive():
            _persist_checkpoint(force=True)
            _persist_new_trades()
            break

        _persist_checkpoint()
        _persist_new_trades()

        meta = _meta()
        started = meta.get("started_at")
        try:
            started_dt = datetime.fromisoformat(
                str(started).replace("Z", "+00:00")
            )
            elapsed = (
                datetime.now(timezone.utc) - started_dt
            ).total_seconds() / 60.0
        except Exception:
            elapsed = 0.0

        fills = max(
            0,
            len(_testnet_fills())
            - int(meta.get("baseline_fill_count", 0) or 0),
        )
        max_fills = int(meta.get("max_fills", 0) or 0)
        max_minutes = int(meta.get("max_minutes", 0) or 0)

        if max_fills and fills >= max_fills:
            _flatten("AUTO_MAX_FILLS")
            _kill()
            break

        if max_minutes and elapsed >= max_minutes:
            _flatten("AUTO_TIME_LIMIT")
            _kill()
            break


def _ensure_watchdog():
    global _watch_thread
    if _watch_thread and _watch_thread.is_alive():
        return
    _watch_stop.clear()
    _watch_thread = threading.Thread(
        target=_watchdog,
        name="observer-relay-v30",
        daemon=True,
    )
    _watch_thread.start()


@app.get("/")
def root():
    return jsonify(
        {
            "service": "Universe Observer Execution Relay",
            "version": RELAY_VERSION,
            "region": RELAY_REGION,
            "testnet_only": True,
        }
    )


@app.get("/health")
def health():
    connectivity = _connectivity()
    return jsonify(
        {
            "ok": connectivity.get("ok", False),
            "service": "observer-execution-relay",
            "version": RELAY_VERSION,
            "region": RELAY_REGION,
            "binance_testnet": connectivity,
            "credentials_ready": bool(
                TESTNET_API_KEY and TESTNET_API_SECRET
            ),
            "relay_secret_ready": bool(SHARED_SECRET),
            "callback_ready": bool(CALLBACK_URL),
            "observer_running": _alive(),
        }
    ), (200 if connectivity.get("ok") else 503)


@app.get("/v1/testnet/status")
def status():
    return jsonify({"ok": True, "testnet": _status()})


@app.post("/v1/testnet/start")
def start():
    data = request.get_json(silent=True) or {}
    try:
        max_order = float(data.get("max_order_usdt"))
        max_fills = int(data.get("max_fills"))
        max_minutes = int(data.get("max_minutes"))
    except Exception:
        return jsonify(
            {"ok": False, "message": "Invalid limits."}
        ), 400

    if not (1 <= max_order <= MAX_ORDER_HARD):
        return jsonify(
            {
                "ok": False,
                "message": f"max_order must be 1..{MAX_ORDER_HARD:.0f} USDT",
            }
        ), 400
    if not (1 <= max_fills <= MAX_FILLS_HARD):
        return jsonify(
            {
                "ok": False,
                "message": f"max_fills must be 1..{MAX_FILLS_HARD}",
            }
        ), 400
    if not (5 <= max_minutes <= MAX_MINUTES_HARD):
        return jsonify(
            {
                "ok": False,
                "message": f"max_minutes must be 5..{MAX_MINUTES_HARD}",
            }
        ), 400

    ok, message = _start(
        max_order,
        max_fills,
        max_minutes,
        checkpoint=data.get("checkpoint"),
    )
    return jsonify(
        {
            "ok": ok,
            "message": message,
            "testnet": _status(),
        }
    ), (200 if ok else 409)


@app.post("/v1/testnet/close")
def close():
    ok, message = _flatten("MANUAL_CLOSE")
    return jsonify(
        {
            "ok": ok,
            "message": message,
            "testnet": _status(),
        }
    ), (200 if ok else 409)


@app.post("/v1/testnet/stop")
def stop():
    close_ok, close_message = _flatten("STOP_FLATTEN")
    stop_ok, stop_message = _kill()
    return jsonify(
        {
            "ok": bool(close_ok and stop_ok),
            "message": f"{close_message} {stop_message}",
            "testnet": _status(),
        }
    ), (200 if close_ok and stop_ok else 409)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
