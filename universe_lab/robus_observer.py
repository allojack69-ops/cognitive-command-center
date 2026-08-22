from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from statistics import mean
from typing import Callable, Sequence

Vector = list[float]
StepFn = Callable[[Vector, Vector], Vector]
ScoreFn = Callable[[Vector], float]


@dataclass(frozen=True)
class ObserverROBUSModel:
    key: str
    title: str
    description: str
    state0: tuple[float, ...]
    perturbations: tuple[str, ...]
    step_fn: StepFn
    viability_fn: ScoreFn
    efp_fn: ScoreFn
    observe_fn: ScoreFn
    calibration: dict


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _num(value, default: float = 0.0) -> float:
    try:
        x = float(value)
        return x if isfinite(x) else float(default)
    except Exception:
        return float(default)


def _unit(value, default: float = 0.5) -> float:
    if value is None:
        return float(default)
    x = _num(value, default)
    if 1.0 < x <= 100.0:
        x /= 100.0
    elif x > 100.0:
        x = x / (100.0 + x)
    return _clip(x, 0.0, 1.0)


def _unit_error(value, default: float = 0.0) -> float:
    if value is None:
        return float(default)
    x = abs(_num(value, default))
    if 1.0 < x <= 100.0:
        x /= 100.0
    elif x > 100.0:
        x = x / (100.0 + x)
    return _clip(x, 0.0, 1.0)


def _gate(value, default: float = 0.5) -> float:
    if not isinstance(value, dict):
        return float(default)
    for key in ("allowed", "ready", "strict_ready", "testnet_ready"):
        if key in value:
            return 1.0 if bool(value.get(key)) else 0.0
    return float(default)


def _action_signal(action) -> float:
    action = str(action or "").strip().upper()
    if action == "BUY":
        return 1.0
    if action == "SELL":
        return -1.0
    return 0.0


def _lag1(values: Sequence[float], default: float) -> float:
    if len(values) < 4:
        return float(default)
    x0 = list(values[:-1])
    x1 = list(values[1:])
    m0, m1 = mean(x0), mean(x1)
    den = sum((x - m0) ** 2 for x in x0)
    if den <= 1e-12:
        return float(default)
    cov = sum((a - m0) * (b - m1) for a, b in zip(x0, x1))
    return _clip(cov / den, -0.95, 0.95)


def _series(snapshot: dict) -> tuple[list[float], list[float]]:
    recent = snapshot.get("recent") or {}
    rows = recent.get("states") or []
    trends: list[float] = []
    vols: list[float] = []
    for row in rows[-60:]:
        if not isinstance(row, dict):
            continue
        if row.get("trend_pct") is not None:
            trends.append(_num(row.get("trend_pct")))
        if row.get("volatility_pct") is not None:
            vols.append(abs(_num(row.get("volatility_pct"))))
    return trends, vols


def build_observer_model(snapshot: dict, *, source: str = "unknown") -> ObserverROBUSModel:
    if not isinstance(snapshot, dict):
        raise ValueError("Observer snapshot must be a JSON object")

    current = snapshot.get("current_state") or {}
    if not isinstance(current, dict) or not current:
        raise ValueError("Observer snapshot has no current_state")

    derived = snapshot.get("derived") or {}
    metrics = snapshot.get("metrics") or {}
    execution = current.get("execution_readiness") or {}
    residual = metrics.get("model_residual_tracker") or derived.get("RES1") or {}
    eh1 = execution.get("eh1") or derived.get("EH1") or {}
    gsr1 = execution.get("gsr1") or derived.get("GSR1") or {}

    trends, vols = _series(snapshot)
    current_trend = _num(current.get("trend_pct"))
    current_vol = abs(_num(current.get("volatility_pct")))

    trend_scale = max(
        [0.05, abs(current_trend)] + [abs(x) for x in trends]
    )
    vol_scale = max([0.05, current_vol] + vols)

    norm_trends = [_clip(x / trend_scale, -2.0, 2.0) for x in trends]
    norm_vols = [_clip(x / vol_scale, 0.0, 3.0) for x in vols]

    trend0 = _clip(current_trend / trend_scale, -2.0, 2.0)
    vol0 = _clip(current_vol / vol_scale, 0.0, 3.0)
    trend_target = mean(norm_trends[-20:]) if norm_trends else trend0
    vol_target = mean(norm_vols[-20:]) if norm_vols else vol0
    trend_persistence = _lag1(norm_trends, 0.72)
    vol_persistence = _lag1(norm_vols, 0.78)

    p_success = _unit(current.get("p_success"), 0.55)
    execution_score = _unit(execution.get("score"), 0.50)
    trade_gate = _gate(current.get("tradeability_gate"), 0.50)
    edge_gate = _gate(current.get("edge_gate"), 0.50)

    structure0 = _clip(
        0.35 * p_success
        + 0.25 * execution_score
        + 0.20 * trade_gate
        + 0.20 * edge_gate,
        0.0,
        1.0,
    )

    action = _action_signal(current.get("action"))
    relation0 = 0.35 if bool(eh1.get("direction_conflict", False)) else 0.82
    geometry_action = _action_signal(gsr1.get("geometry_preferred_action"))
    if geometry_action and action:
        relation0 += 0.10 if geometry_action == action else -0.20
    relation0 = _clip(relation0, 0.0, 1.0)

    reliability = _unit(residual.get("reliability"), p_success)
    memory0 = _clip(
        0.50 * _unit_error(residual.get("ema_total_error"))
        + 0.30 * _unit_error(residual.get("ema_direction_error"))
        + 0.20 * (1.0 - reliability),
        0.0,
        1.0,
    )

    source_quality = 0.90 if str(source).startswith("runtime:") else 0.65
    observer0 = _clip(0.70 * reliability + 0.30 * source_quality, 0.0, 1.0)

    state0 = (
        structure0,
        relation0,
        memory0,
        trend0,
        vol0,
        observer0,
    )

    base_gap = abs(trend0 - action) if action else 0.25 * abs(trend0)

    def step_fn(state: Vector, perturbation: Vector) -> Vector:
        if len(state) != 6:
            raise ValueError("Observer ROBUS state must have 6 dimensions")
        if len(perturbation) != 6:
            raise ValueError("Observer ROBUS perturbation must have 6 dimensions")

        structure, relation_state, memory_load, trend, volatility, observability = map(float, state)
        scale, relation, memory, noise, recurrence, observer_loss = map(float, perturbation)

        next_trend = _clip(
            trend_persistence * trend
            + (1.0 - trend_persistence) * trend_target
            + 0.05 * relation * action
            - 0.10 * noise * volatility
            + 0.07 * scale * recurrence,
            -2.0,
            2.0,
        )

        next_vol = _clip(
            vol_persistence * volatility
            + (1.0 - vol_persistence) * vol_target
            + 0.10 * abs(noise)
            + 0.06 * scale * scale
            + 0.04 * abs(recurrence) * max(volatility, 0.10),
            0.0,
            3.0,
        )

        gap = abs(next_trend - action) if action else 0.25 * abs(next_trend)
        gap_excess = max(0.0, gap - base_gap)

        next_relation = _clip(
            0.92 * relation_state
            + 0.08 * relation0
            - 0.12 * abs(relation)
            - 0.08 * abs(relation * scale)
            - 0.05 * gap_excess,
            0.0,
            1.0,
        )

        pressure = (
            0.09 * abs(scale)
            + 0.07 * max(0.0, next_vol - vol0)
            + 0.08 * max(0.0, memory_load - memory0)
            + 0.08 * (scale * recurrence) ** 2
            + 0.06 * noise * noise
            + 0.04 * gap_excess
        )

        next_structure = _clip(
            0.93 * structure
            + 0.07 * structure0
            + 0.03 * (next_relation - relation0)
            - pressure,
            0.0,
            1.0,
        )

        next_memory = _clip(
            0.96 * memory_load
            + 0.04 * memory0
            + 0.07 * abs(memory)
            + 0.07 * memory * memory
            + 0.07 * abs(scale * recurrence)
            + 0.05 * max(0.0, structure0 - next_structure)
            + 0.04 * abs(relation) * max(0.0, relation0 - next_relation),
            0.0,
            2.0,
        )

        next_observer = _clip(
            0.94 * observability
            + 0.06 * observer0
            - 0.12 * abs(observer_loss)
            - 0.08 * observer_loss * observer_loss
            - 0.04 * max(0.0, next_memory - memory0)
            + 0.02 * (next_relation - relation0),
            0.0,
            1.0,
        )

        return [
            next_structure,
            next_relation,
            next_memory,
            next_trend,
            next_vol,
            next_observer,
        ]

    def viability_fn(state: Vector) -> float:
        structure, relation_state, memory_load, _trend, volatility, observability = map(float, state)
        health = (
            0.38 * structure
            + 0.22 * relation_state
            + 0.16 * observability
            + 0.14 * (1.0 - _clip(memory_load, 0.0, 1.0))
            + 0.10 * (1.0 - min(volatility / 2.0, 1.0))
        )
        return health - 0.35

    def efp_fn(state: Vector) -> float:
        structure, relation_state, memory_load, _trend, volatility, observability = map(float, state)
        volatility_penalty = min(
            max(0.0, volatility - vol0) / max(1.0 + vol0, 1e-9),
            1.0,
        )
        return _clip(
            0.34 * structure
            + 0.18 * relation_state
            + 0.14 * observability
            + 0.22 * (1.0 - min(memory_load, 1.0))
            + 0.12 * (1.0 - volatility_penalty),
            0.0,
            1.0,
        )

    def observe_fn(state: Vector) -> float:
        structure, relation_state, _memory_load, _trend, _volatility, observability = map(float, state)
        return _clip(
            0.50 * structure
            + 0.25 * relation_state
            + 0.25 * observability,
            0.0,
            1.0,
        )

    calibration = {
        "source": source,
        "state_id": current.get("state_id"),
        "market_time": current.get("market_time") or current.get("time"),
        "symbol": snapshot.get("symbol") or current.get("symbol"),
        "action": current.get("action"),
        "state_labels": [
            "structure_health",
            "relation_coherence",
            "memory_load",
            "trend_normalized",
            "volatility_normalized",
            "observability",
        ],
        "state0": list(state0),
        "trend_scale_pct": trend_scale,
        "volatility_scale_pct": vol_scale,
        "trend_persistence": trend_persistence,
        "volatility_persistence": vol_persistence,
        "recent_state_count": len((snapshot.get("recent") or {}).get("states") or []),
        "residual_reliability": reliability,
        "proxy_note": (
            "Read-only counterfactual model calibrated from the live Observer snapshot. "
            "It is not an empirical causal proof and does not alter execution."
        ),
    }

    return ObserverROBUSModel(
        key="observer_live_proxy",
        title="Observer Live ROBUS",
        description="Read-only ROBUS counterfactual model calibrated from the current MOR Observer snapshot.",
        state0=state0,
        perturbations=(
            "scale",
            "relation",
            "memory",
            "noise",
            "recurrence",
            "observer_loss",
        ),
        step_fn=step_fn,
        viability_fn=viability_fn,
        efp_fn=efp_fn,
        observe_fn=observe_fn,
        calibration=calibration,
    )
