"""SL1 v0.2 — Stochastic Leverage / Noise-to-Leverage research engine.

Changes vs v0.1:
- Removes the HOLD -> BUY fallback.
- Uses market drift consistently; SELL is applied only in the payoff layer.
- Ignores p_success as directional evidence when the source action is HOLD/NONE.
- Evaluates BUY and SELL symmetrically for live Observer states.
- Separates "a harvestable region exists" from "current baseline is actionable".
- Remains research-only. It never places orders.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional, Tuple
import argparse
import json
import math
import random

SL_VERSION = "SL1.2"


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _quantile(xs, q):
    if not xs:
        return 0.0
    ys = sorted(float(x) for x in xs)
    q = _clamp(float(q), 0.0, 1.0)
    pos = q * (len(ys) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return ys[lo]
    w = pos - lo
    return ys[lo] * (1 - w) + ys[hi] * w


def _cvar_loss(payoffs, alpha):
    losses = [max(0.0, -float(x)) for x in payoffs]
    if not losses:
        return 0.0
    cutoff = _quantile(losses, alpha)
    tail = [x for x in losses if x >= cutoff - 1e-15]
    return _mean(tail)


def _max_drawdown(path):
    peak = 0.0
    worst = 0.0
    for x in path:
        x = float(x)
        peak = max(peak, x)
        worst = max(worst, peak - x)
    return worst


@dataclass(frozen=True)
class Policy:
    name: str
    action: str
    exposure: float
    stop_loss_pct: Optional[float] = None
    stop_slippage_pct: float = 0.0
    description: str = ""

    @property
    def direction(self):
        a = self.action.upper()
        return 1 if a == "BUY" else -1 if a == "SELL" else 0


@dataclass(frozen=True)
class Config:
    samples: int = 500
    path_steps: int = 15
    seed: int = 1701
    alpha: float = 0.95
    epsilon_ruin: float = 0.01
    catastrophic_loss_pct: float = 1.00
    cvar_limit_pct: float = 0.70
    lambda_cvar: float = 0.80
    lambda_ruin: float = 8.00
    lambda_drawdown: float = 0.10
    transaction_cost_pct: float = 0.24
    jump_probability: float = 0.035
    jump_scale: float = 2.75
    sigma_multipliers: Tuple[float, ...] = (0.25, 0.50, 1.00, 2.00, 4.00)
    exposure_grid: Tuple[float, ...] = (0.25, 0.50, 1.00)
    stop_sigma_grid: Tuple[float, ...] = (0.50, 0.75, 1.00)


def _simulate_paths(sigma_pct, market_drift_pct, cfg, seed_offset=0):
    rng = random.Random(cfg.seed + int(seed_offset))
    steps = max(2, int(cfg.path_steps))
    samples = max(100, int(cfg.samples))
    step_sigma = max(1e-9, float(sigma_pct)) / math.sqrt(steps)
    step_drift = float(market_drift_pct) / steps
    out = []

    for _ in range(samples):
        cumulative = 0.0
        path = []
        for _j in range(steps):
            shock = rng.gauss(0.0, step_sigma)
            if rng.random() < cfg.jump_probability:
                shock += (
                    rng.choice((-1.0, 1.0))
                    * abs(rng.gauss(0.0, step_sigma * cfg.jump_scale))
                )
            cumulative += step_drift + shock
            path.append(cumulative)
        out.append(path)

    return out


def _payoff(path, policy, cfg):
    direction = policy.direction
    exposure = _clamp(float(policy.exposure), 0.0, 1.0)

    if direction == 0 or exposure <= 0:
        return 0.0, 0.0, False

    directed = [direction * float(x) for x in path]
    gross = directed[-1]
    stopped = False

    if policy.stop_loss_pct is not None:
        stop = max(0.0, float(policy.stop_loss_pct))
        for value in directed:
            if value <= -stop:
                gross = -stop - max(0.0, float(policy.stop_slippage_pct))
                stopped = True
                break

    dd = _max_drawdown([exposure * x for x in directed])
    payoff = (
        exposure * gross
        - exposure * max(0.0, cfg.transaction_cost_pct)
    )
    return payoff, dd, stopped


def evaluate_policy(policy, sigma_pct, market_drift_pct, cfg, seed_offset=0):
    paths = _simulate_paths(
        sigma_pct,
        market_drift_pct,
        cfg,
        seed_offset,
    )

    payoffs = []
    dds = []
    stops = 0

    for path in paths:
        payoff, dd, stopped = _payoff(path, policy, cfg)
        payoffs.append(payoff)
        dds.append(dd)
        stops += int(stopped)

    mean_payoff = _mean(payoffs)
    upside = _mean([max(0.0, x) for x in payoffs])
    downside = _mean([max(0.0, -x) for x in payoffs])
    cvar = _cvar_loss(payoffs, cfg.alpha)
    ruin = (
        sum(x <= -cfg.catastrophic_loss_pct for x in payoffs)
        / len(payoffs)
    )
    mean_dd = _mean(dds)

    efp = (
        mean_payoff
        - cfg.lambda_cvar * cvar
        - cfg.lambda_ruin * ruin
        - cfg.lambda_drawdown * mean_dd
    )

    feasible = (
        ruin <= cfg.epsilon_ruin
        and cvar <= cfg.cvar_limit_pct
    )

    sli = (
        (upside + max(0.0, mean_payoff))
        / (cvar + downside + 1e-9)
        if policy.direction != 0
        else 0.0
    )

    return {
        "policy": asdict(policy),
        "sigma_pct": round(float(sigma_pct), 8),
        "market_drift_pct": round(float(market_drift_pct), 8),
        "mean_payoff_pct": round(mean_payoff, 8),
        "mean_upside_pct": round(upside, 8),
        "mean_downside_pct": round(downside, 8),
        "cvar_loss_pct": round(cvar, 8),
        "ruin_probability": round(ruin, 8),
        "mean_path_drawdown_pct": round(mean_dd, 8),
        "p05_payoff_pct": round(_quantile(payoffs, 0.05), 8),
        "p50_payoff_pct": round(_quantile(payoffs, 0.50), 8),
        "p95_payoff_pct": round(_quantile(payoffs, 0.95), 8),
        "stop_hit_rate": round(stops / len(payoffs), 8),
        "efp_score": round(efp, 8),
        "sli": round(sli, 8),
        "feasible": bool(feasible),
    }


def candidate_policies(direction, baseline_sigma_pct, cfg):
    direction = direction.upper()
    if direction not in ("BUY", "SELL"):
        raise ValueError("direction must be BUY or SELL")

    rows = [
        Policy(
            "A_VARIANCE_SUPPRESSION",
            "HOLD",
            0.0,
            description="Control A: suppress exposure to uncertainty.",
        )
    ]

    for exposure in cfg.exposure_grid:
        rows.append(
            Policy(
                f"B_LINEAR_{direction}_E{exposure:.2f}",
                direction,
                exposure,
                description="Control B: linear directional exposure.",
            )
        )

    base_sigma = max(1e-6, float(baseline_sigma_pct))

    for exposure in cfg.exposure_grid:
        for stop_mult in cfg.stop_sigma_grid:
            stop = max(
                cfg.transaction_cost_pct * 1.25,
                base_sigma * stop_mult,
            )
            rows.append(
                Policy(
                    (
                        f"C_STOCHASTIC_LEVERAGE_{direction}"
                        f"_E{exposure:.2f}_S{stop_mult:.2f}"
                    ),
                    direction,
                    exposure,
                    stop_loss_pct=round(stop, 8),
                    stop_slippage_pct=max(
                        0.02,
                        cfg.transaction_cost_pct * 0.15,
                    ),
                    description=(
                        "Control C: bounded-downside counterfactual. "
                        "Requires actual stop enforcement."
                    ),
                )
            )

    return rows


def _best(rows):
    feasible = [x for x in rows if x.get("feasible")]
    pool = feasible if feasible else list(rows)
    if not pool:
        return {}
    return max(
        pool,
        key=lambda x: (
            float(x.get("efp_score", -1e99)),
            float(x.get("mean_payoff_pct", -1e99)),
            -float(x.get("cvar_loss_pct", 1e99)),
        ),
    )


def _baseline_point(sweep):
    if not sweep:
        return {}
    return min(
        sweep,
        key=lambda x: abs(float(x["sigma_multiplier"]) - 1.0),
    )


def run_test(
    baseline_sigma_pct,
    market_drift_pct=0.0,
    direction="BUY",
    cfg=None,
):
    cfg = cfg or Config()
    baseline_sigma_pct = max(1e-6, float(baseline_sigma_pct))
    direction = direction.upper()

    if direction not in ("BUY", "SELL"):
        raise ValueError("direction must be BUY or SELL")

    policies = candidate_policies(
        direction,
        baseline_sigma_pct,
        cfg,
    )

    sweep = []

    for i, mult in enumerate(cfg.sigma_multipliers):
        sigma = baseline_sigma_pct * mult

        rows = [
            evaluate_policy(
                policy,
                sigma,
                market_drift_pct,
                cfg,
                seed_offset=i * 10000,
            )
            for policy in policies
        ]

        control_a = next(
            (
                x
                for x in rows
                if x["policy"]["name"] == "A_VARIANCE_SUPPRESSION"
            ),
            {},
        )

        control_b = _best(
            [
                x for x in rows
                if x["policy"]["name"].startswith("B_LINEAR_")
            ]
        )

        control_c = _best(
            [
                x for x in rows
                if x["policy"]["name"].startswith(
                    "C_STOCHASTIC_LEVERAGE_"
                )
            ]
        )

        sweep.append({
            "sigma_multiplier": mult,
            "sigma_pct": round(sigma, 8),
            "control_a": control_a,
            "control_b": control_b,
            "control_c": control_c,
            "best": _best(rows),
        })

    intervals = []

    for left, right in zip(sweep, sweep[1:]):
        l = left["control_c"]
        r = right["control_c"]

        if not l or not r:
            continue

        dsigma = right["sigma_pct"] - left["sigma_pct"]
        if dsigma <= 0:
            continue

        slope = (
            float(r["efp_score"])
            - float(l["efp_score"])
        ) / dsigma

        controlled = (
            l["feasible"]
            and r["feasible"]
            and l["ruin_probability"] <= cfg.epsilon_ruin
            and r["ruin_probability"] <= cfg.epsilon_ruin
        )

        if slope > 0 and controlled:
            intervals.append({
                "sigma_from_pct": left["sigma_pct"],
                "sigma_to_pct": right["sigma_pct"],
                "d_efp_d_sigma": round(slope, 8),
                "left_policy": l["policy"]["name"],
                "right_policy": r["policy"]["name"],
                "left_ruin_probability": l["ruin_probability"],
                "right_ruin_probability": r["ruin_probability"],
            })

    c_efp = [
        float(x["control_c"].get("efp_score", 0.0))
        for x in sweep
    ]

    second_diff = [
        c - 2 * b + a
        for a, b, c in zip(c_efp, c_efp[1:], c_efp[2:])
    ]

    strongest = (
        max(intervals, key=lambda x: x["d_efp_d_sigma"])
        if intervals
        else None
    )

    baseline = _baseline_point(sweep)
    baseline_c = baseline.get("control_c") or {}

    baseline_actionable = bool(
        baseline_c.get("feasible")
        and float(baseline_c.get("efp_score", 0.0)) > 0.0
        and baseline_c.get("policy", {}).get("action")
        in ("BUY", "SELL")
    )

    return {
        "version": SL_VERSION,
        "kind": "NOISE_TO_LEVERAGE_TEST",
        "direction": direction,
        "baseline_sigma_pct": round(baseline_sigma_pct, 8),
        "market_drift_pct": round(float(market_drift_pct), 8),
        "stochastic_leverage_detected": bool(intervals),
        "baseline_actionable": baseline_actionable,
        "baseline_control_c": baseline_c,
        "leverage_intervals": intervals,
        "strongest_interval": strongest,
        "convexity_proxy": round(_mean(second_diff), 8),
        "interpretation": (
            "HARVESTABLE_NOISE_REGION_FOUND"
            if intervals
            else "NO_CONTROLLED_POSITIVE_NOISE_SLOPE_FOUND"
        ),
        "execution_status": "RESEARCH_ONLY_COUNTERFACTUAL",
        "warning": (
            "Protective-stop geometry is not realized until "
            "the execution layer enforces the stop."
        ),
        "sweep": sweep,
    }


def state_inputs(state):
    state = dict(state or {})
    sf = state.get("state_features") or {}

    source_action = str(
        state.get("action") or "HOLD"
    ).upper()

    candidates = [
        state.get("volatility_pct"),
        sf.get("volatility_pct"),
        sf.get("atr_pct"),
        sf.get("realized_volatility_pct"),
    ]

    sigma = next(
        (
            abs(float(x))
            for x in candidates
            if isinstance(x, (int, float))
            and abs(float(x)) > 1e-9
        ),
        0.25,
    )

    trend = state.get("trend_pct")
    trend = (
        float(trend)
        if isinstance(trend, (int, float))
        else 0.0
    )

    p_success = state.get("p_success")
    p_success_valid = (
        isinstance(p_success, (int, float))
        and 0.0 < float(p_success) <= 1.0
        and source_action in ("BUY", "SELL")
    )

    # Market drift stays in market coordinates.
    market_drift = 0.20 * trend

    if p_success_valid:
        p_edge = _clamp(
            (float(p_success) - 0.5) * 2.0,
            -1.0,
            1.0,
        )

        if source_action == "BUY":
            market_drift += 0.10 * p_edge * sigma
        else:
            market_drift -= 0.10 * p_edge * sigma

    return {
        "baseline_sigma_pct": max(0.01, sigma),
        "market_drift_pct": market_drift,
        "source_action": source_action,
        "source_p_success": (
            float(p_success)
            if isinstance(p_success, (int, float))
            else None
        ),
        "p_success_used": bool(p_success_valid),
        "source_trend_pct": trend,
    }


def _baseline_score(result):
    c = result.get("baseline_control_c") or {}
    if not c.get("feasible"):
        return -1e99
    return float(c.get("efp_score", -1e99))


def evaluate_state(state, cfg=None):
    inputs = state_inputs(state)

    buy = run_test(
        inputs["baseline_sigma_pct"],
        inputs["market_drift_pct"],
        "BUY",
        cfg,
    )

    sell = run_test(
        inputs["baseline_sigma_pct"],
        inputs["market_drift_pct"],
        "SELL",
        cfg,
    )

    candidates = [buy, sell]
    selected = max(candidates, key=_baseline_score)
    selected_score = _baseline_score(selected)

    if selected_score <= 0.0:
        selected_direction = "HOLD"
        current_actionable = False
    else:
        selected_direction = selected["direction"]
        current_actionable = bool(
            selected.get("baseline_actionable")
        )

    all_intervals = []
    for result in candidates:
        for interval in result["leverage_intervals"]:
            item = dict(interval)
            item["direction"] = result["direction"]
            all_intervals.append(item)

    strongest = (
        max(
            all_intervals,
            key=lambda x: float(x["d_efp_d_sigma"]),
        )
        if all_intervals
        else None
    )

    detected = bool(all_intervals)

    # Convexity is reported for the currently selected directional research
    # surface, even if baseline recommendation remains HOLD.
    selected_convexity = selected.get("convexity_proxy", 0.0)

    return {
        "version": SL_VERSION,
        "kind": "OBSERVER_STOCHASTIC_LEVERAGE",
        "state_id": state.get("state_id"),
        "source": inputs,
        "selected_direction": selected_direction,
        "current_actionable": current_actionable,
        "baseline_selected_efp": (
            None if selected_score <= -1e90 else round(selected_score, 8)
        ),
        "stochastic_leverage_detected": detected,
        "leverage_intervals": all_intervals,
        "strongest_interval": strongest,
        "convexity_proxy": selected_convexity,
        "interpretation": (
            "CURRENT_BASELINE_STOCHASTIC_LEVERAGE"
            if current_actionable
            else (
                "HARVESTABLE_NOISE_REGION_FOUND"
                if detected
                else "NO_CONTROLLED_POSITIVE_NOISE_SLOPE_FOUND"
            )
        ),
        "directional_tests": {
            "BUY": buy,
            "SELL": sell,
        },
        "execution_status": "RESEARCH_ONLY_COUNTERFACTUAL",
        "warning": (
            "No execution authority. Protective-stop geometry is "
            "counterfactual until enforced by the exchange layer."
        ),
    }


def load_state(path):
    with open(path, "r", encoding="utf-8") as f:
        obj = json.load(f)

    if (
        isinstance(obj, dict)
        and isinstance(obj.get("current_state"), dict)
    ):
        return obj["current_state"]

    if not isinstance(obj, dict):
        raise ValueError("JSON root must be an object")

    return obj


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="SL1 v0.2 Noise-to-Leverage research test"
    )

    parser.add_argument("--state-json")
    parser.add_argument("--sigma", type=float, default=0.25)
    parser.add_argument("--drift", type=float, default=0.0)
    parser.add_argument(
        "--direction",
        choices=("BUY", "SELL"),
        default="BUY",
    )
    parser.add_argument("--samples", type=int, default=500)
    parser.add_argument("--steps", type=int, default=15)
    parser.add_argument("--compact", action="store_true")

    args = parser.parse_args(argv)

    cfg = Config(
        samples=max(100, args.samples),
        path_steps=max(2, args.steps),
    )

    if args.state_json:
        result = evaluate_state(
            load_state(args.state_json),
            cfg,
        )
    else:
        result = run_test(
            args.sigma,
            args.drift,
            args.direction,
            cfg,
        )

    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=None if args.compact else 2,
        )
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
