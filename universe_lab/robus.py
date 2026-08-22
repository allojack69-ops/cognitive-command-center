from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request

from .robus_kernel import KernelConfig, analyze
from .robus_models import MODELS, get_model
from .robus_observer import build_observer_model

bp = Blueprint("universe_robus", __name__, url_prefix="/lab/robus", template_folder="templates")


@bp.get("/")
def home():
    return render_template("robus.html", models=MODELS)


@bp.post("/api/analyze")
def api_analyze():
    payload = request.get_json(silent=True) or {}
    model = get_model(str(payload.get("model", "generic")))
    horizon = max(5, min(120, int(payload.get("horizon", 30))))
    recovery = max(5, min(120, int(payload.get("recovery_horizon", 20))))
    amplitudes_raw = payload.get("amplitudes") or [0.03, 0.07, 0.15, 0.30, 0.50, 0.80, 1.20]
    amplitudes = tuple(sorted({max(0.001, min(3.0, float(x))) for x in amplitudes_raw}))
    cfg = KernelConfig(horizon=horizon, recovery_horizon=recovery, amplitudes=amplitudes)
    report = analyze(
        state0=model.state0,
        perturbation_names=model.perturbations,
        step_fn=model.step_fn,
        viability_fn=model.viability_fn,
        efp_fn=model.efp_fn,
        observe_fn=model.observe_fn,
        config=cfg,
    )
    result = report.to_dict()
    result["model"] = {
        "key": model.key,
        "title": model.title,
        "description": model.description,
        "synthetic": True,
    }
    return jsonify(result)

@bp.route("/api/observer", methods=["GET", "POST"])
def api_observer():
    # Imported lazily to keep ROBUS decoupled from Observer startup.
    from .observer_control import _load_snapshot, _require_admin

    _require_admin()
    payload = request.get_json(silent=True) or {} if request.method == "POST" else {}

    try:
        snapshot, source = _load_snapshot()
        model = build_observer_model(snapshot, source=source or "unknown")

        horizon = max(5, min(120, int(payload.get("horizon", 30))))
        recovery = max(5, min(120, int(payload.get("recovery_horizon", 20))))
        amplitudes_raw = payload.get("amplitudes") or [0.03, 0.07, 0.15, 0.30, 0.50, 0.80, 1.20]
        amplitudes = tuple(
            sorted({max(0.001, min(3.0, float(x))) for x in amplitudes_raw})
        )

        report = analyze(
            state0=model.state0,
            perturbation_names=model.perturbations,
            step_fn=model.step_fn,
            viability_fn=model.viability_fn,
            efp_fn=model.efp_fn,
            observe_fn=model.observe_fn,
            config=KernelConfig(
                horizon=horizon,
                recovery_horizon=recovery,
                amplitudes=amplitudes,
            ),
        )

        result = report.to_dict()
        result["ok"] = True
        result["model"] = {
            "key": model.key,
            "title": model.title,
            "description": model.description,
            "synthetic": False,
            "calibrated_proxy": True,
            "read_only": True,
        }
        result["observer_source"] = source
        result["calibration"] = model.calibration
        return jsonify(result)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409
    except Exception as exc:
        return jsonify({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }), 500

