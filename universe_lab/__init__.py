import os
from .models import init_lab_models
from .dashboard import bp as lab_bp
from .grp import bp as grp_bp
from .observer import bp as observer_bp
from .observer_control import bp as observer_control_bp
from .observer_testnet import bp as observer_testnet_bp
from .observer_edge import bp as observer_edge_bp
from .mesh import bp as mesh_bp
from .public import bp as public_bp
from .analytics import bp as analytics_bp


def register_universe_lab(app):
    # Never perform database schema I/O during Gunicorn import.
    # Existing production tables are already persisted.
    # Explicit schema bootstrap can be enabled separately when needed.
    if os.getenv("BOOTSTRAP_DB_ON_START", "0") == "1":
        try:
            init_lab_models()
        except Exception as exc:
            print(
                f"[BOOT] Universe Lab schema bootstrap skipped: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
    app.register_blueprint(public_bp)
    app.register_blueprint(analytics_bp)
    app.register_blueprint(lab_bp)
    app.register_blueprint(grp_bp)
    app.register_blueprint(observer_bp)
    app.register_blueprint(observer_control_bp)
    app.register_blueprint(observer_testnet_bp)
    app.register_blueprint(observer_edge_bp)
    app.register_blueprint(mesh_bp)
