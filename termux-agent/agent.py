#!/usr/bin/env python3
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
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

VERSION = "1.0"
HOME = Path.home()
CONFIG_PATH = HOME / ".universe-observer-agent.json"
STATE_PATH = HOME / ".universe-observer-agent-state.json"
RUNTIME_LOG = HOME / ".universe-observer-runtime.log"
BINANCE = "https://testnet.binance.vision"
WS = "wss://stream.testnet.binance.vision/ws/btcusdt@kline_1m"


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {} if default is None else default


def write_json(path, obj):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    os.chmod(path, 0o600)


def load_config():
    cfg = read_json(CONFIG_PATH, {})
    required = (
        "site_url",
        "agent_token",
        "binance_api_key",
        "binance_api_secret",
        "repo",
    )
    missing = [k for k in required if not str(cfg.get(k) or "").strip()]
    if missing:
        raise SystemExit("Agent config missing: " + ", ".join(missing))
    cfg["site_url"] = str(cfg["site_url"]).rstrip("/")
    return cfg


CFG = load_config()
REPO = Path(CFG["repo"]).expanduser().resolve()
RUNTIME = REPO / "observer_runtime"
RUNNER = REPO / "execution_relay" / "run_testnet_runtime.py"
STORAGE = RUNTIME / "storage"
STATUS_FILE = STORAGE / "observer_status.json"
RUNTIME_STATE = STORAGE / "runtime_state.json"
TRADES_FILE = STORAGE / "exchange_trades_erl1.jsonl"


def load_state():
    return read_json(STATE_PATH, {
        "pid": None,
        "active": False,
        "processed_command_ids": [],
        "reported_trade_keys": [],
        "last_checkpoint_state": None,
        "baseline_btc": 0.0,
        "baseline_equity": 0.0,
        "baseline_fill_count": 0,
        "started_at": None,
        "limits": {},
    })


STATE = load_state()


def save_state():
    write_json(STATE_PATH, STATE)


def pid_alive(pid):
    try:
        pid = int(pid or 0)
        if pid <= 1:
            return False
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def http_json(method, url, body=None, headers=None, timeout=20):
    data = None
    h = {
        "Accept": "application/json",
        "User-Agent": f"UniverseLab-Termux-Agent/{VERSION}",
    }
    if headers:
        h.update(headers)
    if body is not None:
        data = json.dumps(
            body,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        h["Content-Type"] = "application/json"

    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers=h,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(
            "utf-8",
            errors="replace",
        )[:800]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


def site_request(method, path, body=None):
    return http_json(
        method,
        CFG["site_url"] + path,
        body=body,
        headers={
            "Authorization": "Bearer " + CFG["agent_token"],
        },
    )


def public_api(path, params=None):
    query = urllib.parse.urlencode(params or {})
    url = BINANCE + path + (("?" + query) if query else "")
    return http_json("GET", url)


def signed_api(method, path, params=None):
    params = dict(params or {})
    server = public_api("/api/v3/time")
    params["timestamp"] = int(server["serverTime"])
    params["recvWindow"] = 5000

    query = urllib.parse.urlencode(params)
    signature = hmac.new(
        CFG["binance_api_secret"].encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    query += "&signature=" + signature

    url = BINANCE + path
    body = None
    if method.upper() == "GET":
        url += "?" + query
    else:
        body = query.encode("utf-8")

    req = urllib.request.Request(
        url,
        data=body,
        method=method.upper(),
        headers={
            "X-MBX-APIKEY": CFG["binance_api_key"],
            "User-Agent": f"UniverseLab-Termux-Agent/{VERSION}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(
            "utf-8",
            errors="replace",
        )[:800]
        raise RuntimeError(
            f"Binance HTTP {exc.code}: {detail}"
        ) from exc


def account():
    data = signed_api("GET", "/api/v3/account")
    ticker = public_api(
        "/api/v3/ticker/price",
        {"symbol": "BTCUSDT"},
    )
    price = float(ticker.get("price", 0) or 0)

    balances = {}
    for item in data.get("balances", []):
        asset = item.get("asset")
        if asset in ("BTC", "USDT"):
            balances[asset] = float(item.get("free", 0) or 0)

    btc = balances.get("BTC", 0.0)
    usdt = balances.get("USDT", 0.0)
    return {
        "btc": btc,
        "usdt": usdt,
        "price": price,
        "equity_usdt": usdt + btc * price,
    }


def exchange_rules():
    info = public_api(
        "/api/v3/exchangeInfo",
        {"symbol": "BTCUSDT"},
    )
    symbol = (info.get("symbols") or [{}])[0]
    filters = {
        item.get("filterType"): item
        for item in symbol.get("filters", [])
    }
    lot = filters.get("LOT_SIZE") or {}
    notional = (
        filters.get("NOTIONAL")
        or filters.get("MIN_NOTIONAL")
        or {}
    )
    return {
        "step": float(lot.get("stepSize", "0.00000001")),
        "min_notional": float(notional.get("minNotional", "0")),
    }


def floor_step(value, step):
    if step <= 0:
        return value
    return math.floor((value + 1e-15) / step) * step


def tail_jsonl(path):
    rows = []
    try:
        for line in Path(path).read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    except Exception:
        pass
    return rows


def fills():
    return [
        row
        for row in tail_jsonl(TRADES_FILE)
        if row.get("status") == "FILLED_TESTNET"
    ]


def trade_key(row):
    raw = "|".join(
        str(row.get(k, ""))
        for k in ("order_id", "time", "state_id", "action", "status")
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def process_alive():
    return pid_alive(STATE.get("pid"))


def current_status():
    return read_json(STATUS_FILE, {})


def start_runtime(params):
    if process_alive():
        return "Observer Testnet runtime already running."

    if not RUNNER.exists() or not (RUNTIME / "app.py").exists():
        raise RuntimeError(f"Runtime missing under {REPO}")

    acct = account()
    limits = {
        "max_order_usdt": float(
            params.get("max_order_usdt", 10)
        ),
        "max_fills": int(params.get("max_fills", 20)),
        "max_minutes": int(params.get("max_minutes", 120)),
    }

    STORAGE.mkdir(parents=True, exist_ok=True)
    baseline_fill_count = len(fills())

    env = os.environ.copy()
    env.update({
        "BINANCE_API_KEY": CFG["binance_api_key"],
        "BINANCE_API_SECRET": CFG["binance_api_secret"],
        "MOR_EXECUTION_MODE": "TESTNET",
        "MOR_LIVE_ARM": "",
        "MOR_TESTNET_RELAX_GATES": "0",
        "MOR_TESTNET_GEOMETRY_ACTIONS": "0",
        "MOR_TESTNET_ACTION_ARBITRATION": "0",
        "MOR_PREFLIGHT_ORDER_TEST": "1",
        "MOR_MARKET_REST_URL": BINANCE + "/api/v3/klines",
        "MOR_MARKET_WS_URL": WS,
        "MOR_MAX_ORDER_USDT": str(limits["max_order_usdt"]),
        "MOR_ORDER_COOLDOWN_SECONDS": "60",
        "MOR_OBSERVER_STATUS_FILE": "storage/observer_status.json",
        "MOR_EXPORT_DOWNLOAD_PATH": "storage/MOR_latest_export.json",
        "MOR_EXPORT_FULL_DOWNLOAD_PATH": "storage/MOR_export_full.json",
        "MOR_TESTNET_BASELINE_BTC": str(acct["btc"]),
        "MOR_GAP_MAX_MINUTES": "120",
        "PYTHONUNBUFFERED": "1",
    })

    log = open(RUNTIME_LOG, "ab", buffering=0)
    log.write(
        (
            "\n=== TERMUX TESTNET SESSION "
            + now_iso()
            + " ===\n"
        ).encode("utf-8")
    )

    proc = subprocess.Popen(
        [sys.executable, "-u", str(RUNNER)],
        cwd=str(RUNTIME),
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )

    STATE.update({
        "pid": proc.pid,
        "active": True,
        "started_at": now_iso(),
        "baseline_btc": acct["btc"],
        "baseline_equity": acct["equity_usdt"],
        "baseline_fill_count": baseline_fill_count,
        "limits": limits,
        "last_checkpoint_state": None,
    })
    save_state()
    return f"Testnet Observer started locally (PID {proc.pid})."


def append_control_trade(row):
    STORAGE.mkdir(parents=True, exist_ok=True)
    with TRADES_FILE.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(row, ensure_ascii=False) + "\n"
        )


def flatten(reason="MANUAL_CLOSE"):
    acct = account()
    baseline = float(
        STATE.get("baseline_btc", acct["btc"]) or 0
    )
    session_btc = max(0.0, acct["btc"] - baseline)
    rules = exchange_rules()
    qty = floor_step(session_btc, rules["step"])

    if (
        qty <= 0
        or qty * acct["price"] < rules["min_notional"]
    ):
        return "No bot-opened Testnet BTC position to close."

    qty_text = f"{qty:.8f}".rstrip("0").rstrip(".")
    params = {
        "symbol": "BTCUSDT",
        "side": "SELL",
        "type": "MARKET",
        "quantity": qty_text,
        "newOrderRespType": "FULL",
    }

    signed_api("POST", "/api/v3/order/test", params)
    result = signed_api("POST", "/api/v3/order", params)

    row = {
        "time": now_iso(),
        "state_id": "CONTROL",
        "strategy": "TERMUX_POSITION_MANAGER",
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
    append_control_trade(row)
    return f"Closed {qty_text} BTC on Spot Testnet."


def stop_runtime(flatten_first=True, reason="MANUAL_STOP"):
    notes = []

    if flatten_first:
        try:
            notes.append(flatten(reason))
        except Exception as exc:
            notes.append(
                "Flatten error: "
                + f"{type(exc).__name__}: {exc}"[:250]
            )

    pid = STATE.get("pid")
    if pid_alive(pid):
        try:
            try:
                os.killpg(
                    os.getpgid(int(pid)),
                    signal.SIGINT,
                )
            except Exception:
                os.kill(int(pid), signal.SIGINT)

            deadline = time.time() + 12
            while time.time() < deadline and pid_alive(pid):
                time.sleep(0.2)

            if pid_alive(pid):
                try:
                    os.killpg(
                        os.getpgid(int(pid)),
                        signal.SIGKILL,
                    )
                except Exception:
                    os.kill(int(pid), signal.SIGKILL)
        except Exception as exc:
            notes.append(
                "Stop error: "
                + f"{type(exc).__name__}: {exc}"[:250]
            )

    STATE["pid"] = None
    STATE["active"] = False
    STATE["stopped_at"] = now_iso()
    save_state()
    notes.append("Observer runtime stopped.")
    return " ".join(notes)


def status_payload():
    if STATE.get("active") and not process_alive():
        STATE["active"] = False
        STATE["pid"] = None
        save_state()

    runtime_status = current_status()
    current = runtime_status.get("current_state") or {}

    try:
        acct = account()
    except Exception as exc:
        acct = {"error": f"{type(exc).__name__}: {exc}"[:300]}

    current_btc = float(acct.get("btc", 0) or 0)
    baseline_btc = float(
        STATE.get("baseline_btc", current_btc) or 0
    )
    bot_btc = max(0.0, current_btc - baseline_btc)

    pnl = None
    baseline_eq = STATE.get("baseline_equity")
    if (
        baseline_eq is not None
        and acct.get("equity_usdt") is not None
    ):
        pnl = (
            float(acct["equity_usdt"])
            - float(baseline_eq)
        )

    fill_count = max(
        0,
        len(fills())
        - int(STATE.get("baseline_fill_count", 0) or 0),
    )

    return {
        "active": process_alive(),
        "pid": STATE.get("pid"),
        "started_at": STATE.get("started_at"),
        "limits": STATE.get("limits") or {},
        "account": acct,
        "state_id": current.get("state_id"),
        "market_time": current.get("market_time"),
        "price": current.get("price"),
        "action": current.get("action"),
        "regime": current.get("regime"),
        "strategy": current.get("chosen_strategy"),
        "bot_position_btc": bot_btc,
        "bot_position_value_usdt": (
            bot_btc * float(acct.get("price", 0) or 0)
        ),
        "session_pnl_usdt": pnl,
        "fills": fill_count,
        "testnet_only": True,
        "runtime_status": runtime_status,
    }


def report(kind, payload, ack=None):
    body = {"kind": kind, "payload": payload}
    if ack:
        body["ack"] = ack
    return site_request(
        "POST",
        "/observer/edge/agent/report",
        body,
    )


def report_heartbeat():
    report("heartbeat", {
        "time": now_iso(),
        "pid": STATE.get("pid"),
        "active": process_alive(),
        "device": "termux-android",
        "repo": str(REPO),
        "testnet_only": True,
    })


def report_status(ack=None):
    report("status", status_payload(), ack=ack)


def report_new_trades():
    seen = set(STATE.get("reported_trade_keys") or [])
    changed = False

    for row in fills():
        key = trade_key(row)
        if key in seen:
            continue
        report("trade", row)
        seen.add(key)
        changed = True

    if changed:
        STATE["reported_trade_keys"] = list(seen)[-1000:]
        save_state()


def report_checkpoint():
    if not RUNTIME_STATE.exists():
        return

    runtime_status = current_status()
    current = runtime_status.get("current_state") or {}
    state_id = str(current.get("state_id") or "")

    if (
        not state_id
        or state_id == STATE.get("last_checkpoint_state")
    ):
        return

    raw = RUNTIME_STATE.read_bytes()
    payload = {
        "state_id": state_id,
        "saved_at": now_iso(),
        "status": runtime_status,
        "runtime_gzip_b64": base64.b64encode(
            gzip.compress(raw, 6)
        ).decode("ascii"),
    }

    report("checkpoint", payload)
    STATE["last_checkpoint_state"] = state_id
    save_state()


def auto_limits():
    if not process_alive():
        return

    limits = STATE.get("limits") or {}
    max_minutes = int(limits.get("max_minutes", 0) or 0)
    max_fills = int(limits.get("max_fills", 0) or 0)

    elapsed = 0.0
    try:
        started = datetime.fromisoformat(
            str(STATE.get("started_at")).replace(
                "Z",
                "+00:00",
            )
        )
        elapsed = (
            datetime.now(timezone.utc) - started
        ).total_seconds() / 60.0
    except Exception:
        pass

    fill_count = max(
        0,
        len(fills())
        - int(STATE.get("baseline_fill_count", 0) or 0),
    )

    if max_fills and fill_count >= max_fills:
        stop_runtime(True, "AUTO_MAX_FILLS")
    elif max_minutes and elapsed >= max_minutes:
        stop_runtime(True, "AUTO_TIME_LIMIT")


def handle_command(cmd):
    event_id = int(cmd.get("event_id"))
    command = str(cmd.get("command") or "").lower()
    params = (
        cmd.get("params")
        if isinstance(cmd.get("params"), dict)
        else {}
    )

    if command == "start":
        note = start_runtime(params)
    elif command == "close":
        note = flatten("MANUAL_CLOSE")
    elif command == "stop":
        note = stop_runtime(True, "STOP_FLATTEN")
    elif command == "status":
        note = "Status requested."
    else:
        note = "Unknown command ignored."

    return event_id, note


def main():
    print(f"Universe Lab Termux Observer Agent v{VERSION}")
    print("TESTNET ONLY. No real-money key path exists here.")
    print("Control:", CFG["site_url"])
    print("Repo:", REPO)

    processed = set(
        STATE.get("processed_command_ids") or []
    )
    last_heartbeat = 0.0
    last_status = 0.0

    while True:
        now = time.time()
        try:
            auto_limits()
            report_new_trades()
            report_checkpoint()

            if now - last_heartbeat >= 5:
                report_heartbeat()
                last_heartbeat = now

            if now - last_status >= 6:
                report_status()
                last_status = now

            polled = site_request(
                "GET",
                "/observer/edge/agent/poll",
            )
            for cmd in polled.get("commands", []):
                event_id = int(cmd.get("event_id"))

                if event_id in processed:
                    report_status(ack=[event_id])
                    continue

                try:
                    ack_id, note = handle_command(cmd)
                    print(
                        now_iso(),
                        "COMMAND",
                        cmd.get("command"),
                        "=>",
                        note,
                        flush=True,
                    )
                    processed.add(ack_id)
                    STATE["processed_command_ids"] = (
                        list(processed)[-500:]
                    )
                    save_state()
                    report_status(ack=[ack_id])
                except Exception as exc:
                    print(
                        now_iso(),
                        "COMMAND ERROR",
                        cmd.get("command"),
                        type(exc).__name__,
                        str(exc)[:400],
                        flush=True,
                    )
                    report("status", {
                        **status_payload(),
                        "command_error": (
                            f"{type(exc).__name__}: {exc}"
                        )[:600],
                    })

        except KeyboardInterrupt:
            print(
                "Agent stopped. Observer runtime is left unchanged."
            )
            break
        except Exception as exc:
            print(
                now_iso(),
                "AGENT LINK ERROR",
                type(exc).__name__,
                str(exc)[:500],
                flush=True,
            )

        time.sleep(float(CFG.get("poll_seconds", 3)))


if __name__ == "__main__":
    main()

