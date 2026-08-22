from universe_lab.robus_kernel import KernelConfig, analyze
from universe_lab.robus_observer import build_observer_model


def _snapshot():
    return {
        "symbol": "BTCUSDT",
        "current_state": {
            "state_id": "XTEST",
            "market_time": "2026-08-22T20:00:00+00:00",
            "p_success": 75.0,
            "trend_pct": 0.15,
            "volatility_pct": 0.35,
            "action": "SELL",
            "tradeability_gate": {"allowed": True},
            "edge_gate": {"allowed": True},
            "execution_readiness": {
                "score": 80.0,
                "strict_ready": True,
                "eh1": {"direction_conflict": False},
                "gsr1": {"geometry_preferred_action": "SELL"},
            },
        },
        "metrics": {
            "model_residual_tracker": {
                "reliability": 90.0,
                "ema_total_error": 0.12,
                "ema_direction_error": 0.08,
            }
        },
        "recent": {
            "states": [
                {"trend_pct": 0.10, "volatility_pct": 0.30},
                {"trend_pct": 0.13, "volatility_pct": 0.31},
                {"trend_pct": 0.12, "volatility_pct": 0.34},
                {"trend_pct": 0.15, "volatility_pct": 0.35},
                {"trend_pct": 0.16, "volatility_pct": 0.36},
                {"trend_pct": 0.15, "volatility_pct": 0.35},
            ]
        },
    }


def test_observer_adapter_builds_read_only_proxy():
    model = build_observer_model(_snapshot(), source="runtime:observer_status.json")
    assert len(model.state0) == 6
    assert model.perturbations == (
        "scale", "relation", "memory", "noise", "recurrence", "observer_loss"
    )
    assert model.calibration["state_id"] == "XTEST"
    assert "Read-only" in model.calibration["proxy_note"]


def test_observer_proxy_runs_through_robus_kernel():
    model = build_observer_model(_snapshot(), source="runtime:observer_status.json")
    report = analyze(
        state0=model.state0,
        perturbation_names=model.perturbations,
        step_fn=model.step_fn,
        viability_fn=model.viability_fn,
        efp_fn=model.efp_fn,
        observe_fn=model.observe_fn,
        config=KernelConfig(
            horizon=20,
            recovery_horizon=10,
            amplitudes=(0.05, 0.15, 0.30, 0.60, 1.0),
            interaction_amplitude=0.30,
        ),
    )
    assert report.effective_rank > 0
    assert len(report.modes) == 6
    pair = next(
        x for x in report.interactions
        if {x.left, x.right} == {"scale", "recurrence"}
    )
    assert pair.score > 0.02
