from universe_lab.robus_kernel import KernelConfig, analyze
from universe_lab.robus_models import get_model


def test_robus_report_detects_structure():
    model = get_model("technical")
    report = analyze(
        state0=model.state0,
        perturbation_names=model.perturbations,
        step_fn=model.step_fn,
        viability_fn=model.viability_fn,
        efp_fn=model.efp_fn,
        observe_fn=model.observe_fn,
        config=KernelConfig(horizon=20, recovery_horizon=10, amplitudes=(0.05, 0.15, 0.30, 0.60, 1.0)),
    )
    assert len(report.modes) == len(model.perturbations)
    assert report.effective_rank > 0
    assert report.explained_modes_95 >= 1
    assert len(report.interactions) > 0
    assert report.interactions[0].score >= report.interactions[-1].score


def test_quadratic_mode_not_lost_when_local_derivative_is_small():
    model = get_model("generic")
    report = analyze(
        state0=model.state0,
        perturbation_names=model.perturbations,
        step_fn=model.step_fn,
        viability_fn=model.viability_fn,
        efp_fn=model.efp_fn,
        observe_fn=model.observe_fn,
        config=KernelConfig(horizon=15, recovery_horizon=8, amplitudes=(0.10, 0.30, 0.60)),
    )
    role = next(m for m in report.modes if m.name == "role")
    assert role.nonlinear_ratio > 0.1


def test_scale_recurrence_interaction_is_visible():
    model = get_model("technical")
    report = analyze(
        state0=model.state0,
        perturbation_names=model.perturbations,
        step_fn=model.step_fn,
        viability_fn=model.viability_fn,
        efp_fn=model.efp_fn,
        observe_fn=model.observe_fn,
        config=KernelConfig(horizon=20, recovery_horizon=8, amplitudes=(0.10, 0.30, 0.60), interaction_amplitude=0.3),
    )
    pair = next(i for i in report.interactions if {i.left, i.right} == {"scale", "recurrence"})
    assert pair.score > 0.01
