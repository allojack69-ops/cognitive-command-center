from flask import Blueprint, render_template, session
from sqlalchemy import func, select

from db import SessionLocal, Run
from .dashboard import get_summary
from .registry import PUBLIC_PROJECTS, LAB_PROJECTS, RESEARCH_PROJECTS

bp = Blueprint("universe_public", __name__, template_folder="templates")


def public_stats():
    summary = get_summary()
    with SessionLocal() as db:
        model_count = db.scalar(
            select(func.count(func.distinct(Run.model_label))).where(
                Run.status == "completed",
                Run.model_label.is_not(None),
                Run.model_label != "",
                Run.model_label != "unknown",
                ~Run.study_key.like("DATASET:%"),
            )
        ) or 0
        ai_runs = db.scalar(
            select(func.count()).select_from(Run).where(
                Run.status == "completed",
                Run.study_key.in_(["BOT_STRESS_V01", "HUMAN_AI_STATE_V1", "HISTORY_INTERVENTION_V01"]),
            )
        ) or 0
    return {
        "participants": summary["human_people"],
        "completed_runs": summary["completed_runs"],
        "models": int(model_count),
        "experiments": len(PUBLIC_PROJECTS) + len(LAB_PROJECTS),
        "ai_runs": int(ai_runs),
    }


@bp.get("/test-ai")
def test_ai():
    return render_template(
        "public_test_ai.html",
        stats=public_stats(),
        public_projects=PUBLIC_PROJECTS,
    )


@bp.get("/experiments")
def experiments():
    return render_template(
        "experiments.html",
        stats=public_stats(),
        public_projects=PUBLIC_PROJECTS,
        lab_projects=LAB_PROJECTS,
        research_projects=RESEARCH_PROJECTS,
    )


@bp.get("/research")
def research():
    return render_template(
        "research.html",
        stats=public_stats(),
        research_projects=RESEARCH_PROJECTS,
    )


@bp.app_context_processor
def inject_public_layer():
    return {
        "universe_public_stats": public_stats,
        "universe_public_projects": PUBLIC_PROJECTS,
        "universe_lab_projects": LAB_PROJECTS,
        "universe_research_projects": RESEARCH_PROJECTS,
    }
