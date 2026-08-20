"""Live SL1 sidecar for MOR Observer.

Reads the latest Observer state and writes:
- storage/stochastic_leverage_sl1.json
- storage/stochastic_leverage_sl1.jsonl

It never places orders and never changes ERL1.
"""

from pathlib import Path
from datetime import datetime, timezone
import argparse
import json
import os
import time

from stochastic_leverage import Config, evaluate_state

BASE = Path(__file__).resolve().parent
STORAGE = BASE / "storage"
LATEST = STORAGE / "stochastic_leverage_sl1.json"
HISTORY = STORAGE / "stochastic_leverage_sl1.jsonl"

CANDIDATES = (
    STORAGE / "observer_status.json",
    STORAGE / "mor_analysis_export_latest.json",
    STORAGE / "MOR_latest_export.json",
)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def current_state():
    for path in CANDIDATES:
        if not path.exists():
            continue
        obj = read_json(path)
        if not isinstance(obj, dict):
            continue
        state = obj.get("current_state")
        if isinstance(state, dict):
            return state, path.name
        if obj.get("state_id"):
            return obj, path.name
    return None, None


def write_atomic(path, obj):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


def append_jsonl(path, obj):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--samples", type=int, default=350)
    args = parser.parse_args()

    STORAGE.mkdir(parents=True, exist_ok=True)

    cfg = Config(samples=max(100, args.samples))
    last_state_id = None

    print("SL1 SIDECAR ONLINE")
    print("mode=RESEARCH_ONLY")
    print("latest=", LATEST)

    while True:
        state, source = current_state()

        if state:
            state_id = str(state.get("state_id") or "UNKNOWN")

            if state_id != last_state_id or args.once:
                started = time.perf_counter()

                try:
                    result = evaluate_state(state, cfg)
                    result["source_file"] = source
                    result["generated_at"] = (
                        datetime.now(timezone.utc).isoformat()
                    )
                    result["compute_ms"] = round(
                        (time.perf_counter() - started) * 1000.0,
                        3,
                    )

                    write_atomic(LATEST, result)
                    append_jsonl(
                        HISTORY,
                        {
                            "generated_at": result["generated_at"],
                            "state_id": state_id,
                            "interpretation": result["interpretation"],
                            "detected": result[
                                "stochastic_leverage_detected"
                            ],
                            "convexity_proxy": result["convexity_proxy"],
                            "strongest_interval": result[
                                "strongest_interval"
                            ],
                            "source": result["source"],
                        },
                    )

                    print(
                        state_id,
                        result["interpretation"],
                        "convexity=",
                        result["convexity_proxy"],
                        "ms=",
                        result["compute_ms"],
                    )

                    last_state_id = state_id

                except Exception as exc:
                    print(
                        "SL1 ERROR:",
                        type(exc).__name__,
                        str(exc),
                    )

        if args.once:
            return 0

        time.sleep(max(0.5, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
