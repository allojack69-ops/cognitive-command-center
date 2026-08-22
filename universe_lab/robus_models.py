from __future__ import annotations

from dataclasses import dataclass
from math import exp
from typing import Callable

from .robus_kernel import Vector


@dataclass(frozen=True)
class SyntheticModel:
    key: str
    title: str
    description: str
    state0: tuple[float, float, float, float]
    perturbations: tuple[str, ...]
    step_fn: Callable[[Vector, Vector], Vector]
    viability_fn: Callable[[Vector], float]
    efp_fn: Callable[[Vector], float]
    observe_fn: Callable[[Vector], float]


def _clamp(x: float, lo: float = -4.0, hi: float = 4.0) -> float:
    return max(lo, min(hi, x))


def _family(coeff: dict):
    def step(state: Vector, p: Vector) -> Vector:
        s, r, m, o = state
        scale, freq, duration, intensity, role, recur = p
        ns = (
            coeff["ss"] * s + coeff["sr"] * r + coeff["sm"] * m
            + coeff["scale2"] * scale * scale + coeff["intensity"] * intensity
            + coeff["freq"] * freq
        )
        nr = (
            coeff["rr"] * r + coeff["rs"] * s + coeff["rm"] * m
            + coeff["role2"] * role * role + coeff["srx"] * scale * recur
            + coeff["freqdur"] * freq * duration
        )
        threshold = coeff["memory_threshold"]
        overload = max(0.0, abs(s) + abs(r) - threshold)
        nm = (
            coeff["mm"] * m + coeff["overload"] * overload
            + coeff["duration"] * abs(duration) + coeff["recur"] * abs(scale * recur)
            + coeff["role_memory"] * abs(role) * max(0.0, r)
        )
        no = (
            coeff["oo"] * o + coeff["or"] * r + coeff["om"] * m
            - coeff["observer_blind"] * intensity * m
            - coeff["observer_role"] * role * r
        )
        return [_clamp(ns), _clamp(nr), _clamp(nm, 0.0, 6.0), _clamp(no)]

    def viability(state: Vector) -> float:
        s, r, m, _ = state
        damage = coeff["vs"] * abs(s) + coeff["vr"] * abs(r) + coeff["vm"] * m
        return 1.0 - damage

    def efp(state: Vector) -> float:
        s, r, m, _ = state
        load = coeff["es"] * abs(s) + coeff["er"] * abs(r) + coeff["em"] * m
        return exp(-max(0.0, load))

    def observe(state: Vector) -> float:
        s, r, m, o = state
        # The observer sees structure/relation and an observation channel, but only a
        # configurable fraction of accumulated memory. This intentionally allows
        # epistemic blind zones without making them inevitable.
        apparent_damage = (
            coeff["os"] * abs(s) + coeff["orr"] * abs(r)
            + coeff["omem"] * m + coeff["oo_penalty"] * max(0.0, -o)
        )
        return max(0.0, min(1.0, 1.0 - apparent_damage))

    return step, viability, efp, observe


BASE = {
    "ss": 0.77, "sr": 0.11, "sm": 0.04,
    "rr": 0.73, "rs": 0.13, "rm": 0.04,
    "mm": 0.91, "oo": 0.78, "or": 0.09, "om": 0.02,
    "scale2": 0.22, "intensity": 0.10, "freq": 0.06,
    "role2": 0.16, "srx": 0.31, "freqdur": 0.08,
    "memory_threshold": 0.48, "overload": 0.18, "duration": 0.045,
    "recur": 0.20, "role_memory": 0.08,
    "observer_blind": 0.14, "observer_role": 0.06,
    "vs": 0.56, "vr": 0.55, "vm": 0.42,
    "es": 0.48, "er": 0.46, "em": 0.72,
    "os": 0.31, "orr": 0.28, "omem": 0.08, "oo_penalty": 0.12,
}


def _build(key: str, title: str, description: str, overrides: dict) -> SyntheticModel:
    c = dict(BASE)
    c.update(overrides)
    step, viability, efp, observe = _family(c)
    return SyntheticModel(
        key=key,
        title=title,
        description=description,
        state0=(0.03, 0.02, 0.01, 0.10),
        perturbations=("scale", "frequency", "duration", "intensity", "role", "recurrence"),
        step_fn=step,
        viability_fn=viability,
        efp_fn=efp,
        observe_fn=observe,
    )


MODELS = {
    "generic": _build(
        "generic", "Generic nonlinear system",
        "Neutral synthetic ROBUS sandbox. Parameters are illustrative, not empirical data.",
        {},
    ),
    "social": _build(
        "social", "Social filtering / truth suppression",
        "Synthetic model of local conflict reduction with accumulated information loss.",
        {"role2": 0.22, "role_memory": 0.14, "observer_blind": 0.18, "em": 0.82},
    ),
    "economic": _build(
        "economic", "Aggressive discounting / dumping",
        "Synthetic model of short-run growth pressure versus accumulated future-option loss.",
        {"scale2": 0.27, "duration": 0.07, "recur": 0.24, "vm": 0.35, "em": 0.86},
    ),
    "organization": _build(
        "organization", "Centralized approval",
        "Synthetic model of coordination benefit turning into relational bottlenecks.",
        {"rr": 0.79, "role2": 0.28, "srx": 0.36, "vr": 0.66, "er": 0.58},
    ),
    "technical": _build(
        "technical", "Automatic retry",
        "Synthetic retry-loop model with scale×recurrence interaction and delayed memory load.",
        {"srx": 0.52, "recur": 0.34, "overload": 0.24, "memory_threshold": 0.40},
    ),
    "moral": _build(
        "moral", "Zero-tolerance rule",
        "Synthetic model of rigid universalization with role and history sensitivity.",
        {"role2": 0.31, "role_memory": 0.18, "vs": 0.60, "vr": 0.63, "em": 0.78},
    ),
}


def get_model(key: str) -> SyntheticModel:
    return MODELS.get(key, MODELS["generic"])
