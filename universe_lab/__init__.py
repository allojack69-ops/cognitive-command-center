from .models import init_lab_models
from .dashboard import bp as lab_bp
from .grp import bp as grp_bp
from .observer import bp as observer_bp
from .mesh import bp as mesh_bp

def register_universe_lab(app):
    init_lab_models()
    app.register_blueprint(lab_bp)
    app.register_blueprint(grp_bp)
    app.register_blueprint(observer_bp)
    app.register_blueprint(mesh_bp)
