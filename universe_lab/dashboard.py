from flask import Blueprint, jsonify, render_template
from sqlalchemy import func, select
from db import SessionLocal, Run, BenchmarkPack
from .models import GRPSession, LabExternalResult
from .registry import PROJECTS

bp=Blueprint("universe_lab",__name__,url_prefix="/lab",template_folder="templates")

def get_summary():
    with SessionLocal() as db:
        total_runs=db.scalar(select(func.count()).select_from(Run)) or 0
        completed_runs=db.scalar(select(func.count()).select_from(Run).where(Run.status=="completed")) or 0
        human_people=db.scalar(select(func.count(func.distinct(Run.participant_id))).where(
            Run.status=="completed",~Run.study_key.like("BENCHMARK:%"),~Run.study_key.like("DATASET:%"),
            Run.participant_id!="P-LAB-SYSTEM")) or 0
        grp_started=db.scalar(select(func.count()).select_from(GRPSession)) or 0
        grp_completed=db.scalar(select(func.count()).select_from(GRPSession).where(GRPSession.completed.is_(True))) or 0
        external=db.scalar(select(func.count()).select_from(LabExternalResult)) or 0
        benchmarks=db.scalar(select(func.count()).select_from(BenchmarkPack)) or 0
        by_study={k:int(n) for k,n in db.execute(select(Run.study_key,func.count()).group_by(Run.study_key)).all()}
    return {"projects":len(PROJECTS),"human_people":int(human_people),"total_runs":int(total_runs),
            "completed_runs":int(completed_runs),"grp_started":int(grp_started),"grp_completed":int(grp_completed),
            "external_results":int(external),"benchmark_packs":int(benchmarks),"by_study":by_study}

@bp.get("/")
def home():
    return render_template("home.html",projects=PROJECTS,summary=get_summary())

@bp.get("/data")
def data_home():
    with SessionLocal() as db:
        runs=db.scalars(select(Run).order_by(Run.created_at.desc()).limit(100)).all()
    return render_template("data.html",runs=runs,summary=get_summary())

@bp.get("/api/summary")
def api_summary(): return jsonify(get_summary())

@bp.app_context_processor
def inject():
    return {"universe_projects":PROJECTS,"universe_lab_summary":get_summary}
