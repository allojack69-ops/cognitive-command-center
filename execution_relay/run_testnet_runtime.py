import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "observer_runtime"
sys.path.insert(0, str(RUNTIME))

import app as mor  # noqa: E402

BASELINE_BTC = max(0.0, float(os.getenv("MOR_TESTNET_BASELINE_BTC", "0")))
_original_governor = mor.exchange_risk_governor


def session_isolated_governor(action, market_price, mode=None):
    mode = (mode or mor.EXECUTION_MODE).upper()
    action = str(action or "").upper()

    if mode != "TESTNET" or action != "SELL":
        return _original_governor(action, market_price, mode=mode)

    if not mor.execution_runtime.get("preflight_ok", False):
        return {
            "allowed": False,
            "reason": "EXCHANGE_PREFLIGHT_FAILED",
            "notional_usdt": 0.0,
        }

    now_ms = int(mor.datetime.now(mor.timezone.utc).timestamp() * 1000)
    last_ms = int(mor.execution_runtime.get("last_order_epoch_ms", 0) or 0)
    if last_ms and (
        now_ms - last_ms
    ) < mor.EXCHANGE_ORDER_COOLDOWN_SECONDS * 1000:
        return {
            "allowed": False,
            "reason": "ORDER_COOLDOWN",
            "notional_usdt": 0.0,
        }

    try:
        account = mor.binance_account("TESTNET")
        usdt = mor.binance_free_balance(account, "USDT")
        btc = mor.binance_free_balance(account, "BTC")
        min_notional = mor.binance_symbol_min_notional("TESTNET")
    except Exception as exc:
        return {
            "allowed": False,
            "reason": "ACCOUNT_READ_FAILED: " + str(exc)[:160],
            "notional_usdt": 0.0,
        }

    # Binance Spot Testnet accounts can start with virtual BTC already present.
    # Never sell that initial inventory: only BTC accumulated above the session baseline.
    session_btc = max(0.0, btc - BASELINE_BTC)
    notional = max(0.0, session_btc * float(market_price) * 0.995)

    if notional + 1e-9 < min_notional:
        return {
            "allowed": False,
            "reason": "NO_SESSION_TESTNET_POSITION",
            "notional_usdt": 0.0,
        }

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
        print("Relay Testnet runtime saved.")
