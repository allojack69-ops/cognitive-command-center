from __future__ import annotations

from dataclasses import dataclass, field
from math import exp, log, sqrt
from typing import Callable, Iterable, Sequence

Vector = list[float]
StepFn = Callable[[Vector, Vector], Vector]
ScoreFn = Callable[[Vector], float]
ObserveFn = Callable[[Vector], float]


@dataclass(frozen=True)
class KernelConfig:
    horizon: int = 30
    recovery_horizon: int = 20
    local_eps: float = 1e-4
    amplitudes: tuple[float, ...] = (0.03, 0.07, 0.15, 0.30, 0.50, 0.80, 1.20)
    interaction_amplitude: float = 0.30
    viability_threshold: float = 0.0
    efp_threshold: float = 0.25
    observed_healthy_threshold: float = 0.55
    critical_bisect_steps: int = 18
    max_pairwise_interactions: int = 64


@dataclass
class ModeResult:
    name: str
    index: int
    local_sensitivity: float
    nonlinear_ratio: float
    critical_radius: float | None
    critical_reason: str | None
    hysteresis: float
    min_efp: float
    blind_fraction: float
    amplitudes: list[dict] = field(default_factory=list)


@dataclass
class InteractionResult:
    left: str
    right: str
    score: float


@dataclass
class ROBUSReport:
    baseline_viability: float
    baseline_efp: float
    baseline_observed: float
    effective_rank: float
    explained_modes_95: int
    global_critical_radius: float | None
    modes: list[ModeResult]
    interactions: list[InteractionResult]
    singular_values: list[float]
    blind_region_detected: bool

    def to_dict(self) -> dict:
        return {
            "baseline": {
                "viability": self.baseline_viability,
                "efp": self.baseline_efp,
                "observed": self.baseline_observed,
            },
            "effective_rank": self.effective_rank,
            "explained_modes_95": self.explained_modes_95,
            "global_critical_radius": self.global_critical_radius,
            "blind_region_detected": self.blind_region_detected,
            "singular_values": self.singular_values,
            "modes": [m.__dict__ for m in self.modes],
            "interactions": [i.__dict__ for i in self.interactions],
        }


def _zeros(n: int) -> Vector:
    return [0.0] * n


def _norm(v: Sequence[float]) -> float:
    return sqrt(sum(float(x) * float(x) for x in v))


def _sub(a: Sequence[float], b: Sequence[float]) -> Vector:
    return [float(x) - float(y) for x, y in zip(a, b)]


def _add(a: Sequence[float], b: Sequence[float]) -> Vector:
    return [float(x) + float(y) for x, y in zip(a, b)]


def _scale(v: Sequence[float], s: float) -> Vector:
    return [float(x) * s for x in v]


def _basis(n: int, i: int) -> Vector:
    v = _zeros(n)
    v[i] = 1.0
    return v


def _flatten(traj: Sequence[Sequence[float]]) -> Vector:
    out: Vector = []
    for row in traj:
        out.extend(float(x) for x in row)
    return out


def _simulate(step_fn: StepFn, state0: Sequence[float], perturbation: Sequence[float], horizon: int) -> list[Vector]:
    state = [float(x) for x in state0]
    p = [float(x) for x in perturbation]
    trajectory = [state[:]]
    for _ in range(horizon):
        nxt = [float(x) for x in step_fn(state, p)]
        if len(nxt) != len(state):
            raise ValueError("step_fn must preserve state dimension")
        if any((x != x or x in (float("inf"), float("-inf"))) for x in nxt):
            raise ValueError("step_fn produced non-finite state")
        state = nxt
        trajectory.append(state[:])
    return trajectory


def _trajectory_scores(
    trajectory: Sequence[Sequence[float]],
    viability_fn: ScoreFn,
    efp_fn: ScoreFn,
    observe_fn: ObserveFn,
    cfg: KernelConfig,
) -> tuple[float, float, float, float]:
    viabilities = [float(viability_fn(list(s))) for s in trajectory]
    efps = [max(0.0, min(1.0, float(efp_fn(list(s))))) for s in trajectory]
    observed = [max(0.0, min(1.0, float(observe_fn(list(s))))) for s in trajectory]
    blind = [
        1.0
        for v, e, o in zip(viabilities, efps, observed)
        if o >= cfg.observed_healthy_threshold and (v < cfg.viability_threshold or e < cfg.efp_threshold)
    ]
    return min(viabilities), min(efps), observed[-1], len(blind) / max(1, len(trajectory))


def _jacobi_eigenvalues_sym(matrix: Sequence[Sequence[float]], tol: float = 1e-12, max_iter: int = 10000) -> list[float]:
    n = len(matrix)
    if n == 0:
        return []
    a = [list(map(float, row)) for row in matrix]
    for _ in range(max_iter):
        p = q = 0
        max_off = 0.0
        for i in range(n):
            for j in range(i + 1, n):
                v = abs(a[i][j])
                if v > max_off:
                    max_off = v
                    p, q = i, j
        if max_off < tol:
            break
        app, aqq, apq = a[p][p], a[q][q], a[p][q]
        if abs(apq) < tol:
            continue
        tau = (aqq - app) / (2.0 * apq)
        t = (1.0 if tau >= 0 else -1.0) / (abs(tau) + sqrt(1.0 + tau * tau))
        c = 1.0 / sqrt(1.0 + t * t)
        s = t * c
        for k in range(n):
            if k in (p, q):
                continue
            aik, akq = a[k][p], a[k][q]
            a[k][p] = a[p][k] = c * aik - s * akq
            a[k][q] = a[q][k] = s * aik + c * akq
        a[p][p] = c * c * app - 2.0 * s * c * apq + s * s * aqq
        a[q][q] = s * s * app + 2.0 * s * c * apq + c * c * aqq
        a[p][q] = a[q][p] = 0.0
    return sorted((max(0.0, a[i][i]) for i in range(n)), reverse=True)


def _singular_values(response_vectors: Sequence[Sequence[float]]) -> list[float]:
    if not response_vectors:
        return []
    k = len(response_vectors)
    gram = [[0.0 for _ in range(k)] for _ in range(k)]
    for i in range(k):
        for j in range(i, k):
            dot = sum(float(a) * float(b) for a, b in zip(response_vectors[i], response_vectors[j]))
            gram[i][j] = gram[j][i] = dot
    return [sqrt(v) for v in _jacobi_eigenvalues_sym(gram) if v > 1e-18]


def _rank_metrics(singular_values: Sequence[float]) -> tuple[float, int]:
    energy = [s * s for s in singular_values if s > 0.0]
    total = sum(energy)
    if total <= 0.0:
        return 0.0, 0
    probs = [x / total for x in energy]
    entropy = -sum(p * log(p) for p in probs if p > 0.0)
    effective_rank = exp(entropy)
    cumulative = 0.0
    n95 = 0
    for x in energy:
        cumulative += x
        n95 += 1
        if cumulative / total >= 0.95:
            break
    return effective_rank, n95


def _is_failed(min_viability: float, min_efp: float, cfg: KernelConfig) -> tuple[bool, str | None]:
    if min_viability < cfg.viability_threshold:
        return True, "viability"
    if min_efp < cfg.efp_threshold:
        return True, "efp"
    return False, None


def analyze(
    *,
    state0: Sequence[float],
    perturbation_names: Sequence[str],
    step_fn: StepFn,
    viability_fn: ScoreFn,
    efp_fn: ScoreFn,
    observe_fn: ObserveFn,
    config: KernelConfig | None = None,
) -> ROBUSReport:
    cfg = config or KernelConfig()
    if cfg.horizon < 1:
        raise ValueError("horizon must be >= 1")
    m = len(perturbation_names)
    if m == 0:
        raise ValueError("at least one perturbation is required")

    p0 = _zeros(m)
    base_traj = _simulate(step_fn, state0, p0, cfg.horizon)
    base_flat = _flatten(base_traj)
    base_v = float(viability_fn(base_traj[-1]))
    base_e = max(0.0, min(1.0, float(efp_fn(base_traj[-1]))))
    base_o = max(0.0, min(1.0, float(observe_fn(base_traj[-1]))))

    response_library: list[Vector] = []
    modes: list[ModeResult] = []
    basis_vectors = [_basis(m, i) for i in range(m)]

    for i, (name, direction) in enumerate(zip(perturbation_names, basis_vectors)):
        eps = cfg.local_eps
        plus_eps = _simulate(step_fn, state0, _scale(direction, eps), cfg.horizon)
        minus_eps = _simulate(step_fn, state0, _scale(direction, -eps), cfg.horizon)
        local_vec = _scale(_sub(_flatten(plus_eps), _flatten(minus_eps)), 1.0 / (2.0 * eps))
        local_sensitivity = _norm(local_vec) / sqrt(max(1, len(local_vec)))

        amp_rows: list[dict] = []
        nonlin_values: list[float] = []
        critical_radius: float | None = None
        critical_reason: str | None = None
        prior_safe = 0.0

        for amp in cfg.amplitudes:
            plus = _simulate(step_fn, state0, _scale(direction, amp), cfg.horizon)
            minus = _simulate(step_fn, state0, _scale(direction, -amp), cfg.horizon)
            dplus = _sub(_flatten(plus), base_flat)
            dminus = _sub(_flatten(minus), base_flat)
            odd = _scale(_sub(dplus, dminus), 0.5)
            even = _scale(_add(dplus, dminus), 0.5)
            nl = _norm(even) / (_norm(odd) + 1e-12)
            nonlin_values.append(nl)
            if _norm(dplus) > 0:
                response_library.append(_scale(dplus, 1.0 / max(amp, 1e-12)))
            if _norm(dminus) > 0:
                response_library.append(_scale(dminus, -1.0 / max(amp, 1e-12)))

            min_v, min_e, obs, blind = _trajectory_scores(plus, viability_fn, efp_fn, observe_fn, cfg)
            failed, reason = _is_failed(min_v, min_e, cfg)
            amp_rows.append({
                "amplitude": amp,
                "min_viability": min_v,
                "min_efp": min_e,
                "observed_final": obs,
                "blind_fraction": blind,
                "failed": failed,
            })
            if failed and critical_radius is None:
                lo, hi = prior_safe, amp
                why = reason
                for _ in range(cfg.critical_bisect_steps):
                    mid = 0.5 * (lo + hi)
                    tr = _simulate(step_fn, state0, _scale(direction, mid), cfg.horizon)
                    mv, me, _, _ = _trajectory_scores(tr, viability_fn, efp_fn, observe_fn, cfg)
                    mid_failed, mid_reason = _is_failed(mv, me, cfg)
                    if mid_failed:
                        hi = mid
                        why = mid_reason or why
                    else:
                        lo = mid
                critical_radius = hi
                critical_reason = why
            if not failed:
                prior_safe = amp

        stress_amp = critical_radius if critical_radius is not None else cfg.amplitudes[-1]
        stress_traj = _simulate(step_fn, state0, _scale(direction, stress_amp), cfg.horizon)
        stressed = stress_traj[-1]
        recovered_traj = _simulate(step_fn, stressed, p0, cfg.recovery_horizon)
        reference_traj = _simulate(step_fn, state0, p0, cfg.horizon + cfg.recovery_horizon)
        hysteresis = _norm(_sub(recovered_traj[-1], reference_traj[-1]))
        min_efp = min(row["min_efp"] for row in amp_rows) if amp_rows else base_e
        blind_fraction = max((row["blind_fraction"] for row in amp_rows), default=0.0)
        modes.append(ModeResult(
            name=name,
            index=i,
            local_sensitivity=local_sensitivity,
            nonlinear_ratio=max(nonlin_values, default=0.0),
            critical_radius=critical_radius,
            critical_reason=critical_reason,
            hysteresis=hysteresis,
            min_efp=min_efp,
            blind_fraction=blind_fraction,
            amplitudes=amp_rows,
        ))

    interactions: list[InteractionResult] = []
    pair_count = 0
    a = cfg.interaction_amplitude
    for i in range(m):
        for j in range(i + 1, m):
            if pair_count >= cfg.max_pairwise_interactions:
                break
            pair_count += 1
            pi = _scale(basis_vectors[i], a)
            pj = _scale(basis_vectors[j], a)
            pij = _add(pi, pj)
            ti = _flatten(_simulate(step_fn, state0, pi, cfg.horizon))
            tj = _flatten(_simulate(step_fn, state0, pj, cfg.horizon))
            tij = _flatten(_simulate(step_fn, state0, pij, cfg.horizon))
            di = _sub(ti, base_flat)
            dj = _sub(tj, base_flat)
            dij = _sub(tij, base_flat)
            residual = _sub(dij, _add(di, dj))
            denom = _norm(di) + _norm(dj) + 1e-12
            score = _norm(residual) / denom
            if _norm(residual) > 0:
                response_library.append(_scale(residual, 1.0 / max(a * a, 1e-12)))
            interactions.append(InteractionResult(perturbation_names[i], perturbation_names[j], score))
        if pair_count >= cfg.max_pairwise_interactions:
            break

    singular = _singular_values(response_library)
    effective_rank, n95 = _rank_metrics(singular)
    criticals = [m.critical_radius for m in modes if m.critical_radius is not None]
    global_radius = min(criticals) if criticals else None
    blind_detected = any(m.blind_fraction > 0.0 for m in modes)

    return ROBUSReport(
        baseline_viability=base_v,
        baseline_efp=base_e,
        baseline_observed=base_o,
        effective_rank=effective_rank,
        explained_modes_95=n95,
        global_critical_radius=global_radius,
        modes=modes,
        interactions=sorted(interactions, key=lambda x: x.score, reverse=True),
        singular_values=singular,
        blind_region_detected=blind_detected,
    )
