import json
from pathlib import Path
from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for
from sqlalchemy import select
from db import SessionLocal, Run
from .common import ensure_system_participant, create_run, complete_run
from .models import LabExternalResult

bp=Blueprint("universe_observer",__name__,url_prefix="/observer",template_folder="templates")
BASE=Path(__file__).resolve().parent
def _load(name): return json.loads((BASE/"data"/name).read_text(encoding="utf-8"))
def _require_admin():
    if not session.get("admin"): abort(403)

def _save_external(result_type,source_version,payload,study_key,protocol_version):
    with SessionLocal() as db:
        p=ensure_system_participant(db)
        run=create_run(db,p,study_key,protocol_version,{"source":"UNIVERSE_LAB_IMPORT","result_type":result_type})
        row=LabExternalResult(run_id=run.id,result_type=result_type,source_version=source_version,
                              payload_json=json.dumps(payload,ensure_ascii=False))
        db.add(row); db.flush()
        complete_run(run,{"external_result_id":row.id})
        db.commit()
        return run.id

@bp.route("/wapi",methods=["GET","POST"])
def wapi_home():
    if request.method=="POST":
        _require_admin()
        try: payload=json.loads(request.form.get("payload",""))
        except Exception:
            flash("Некоректний JSON.","error"); return redirect(url_for("universe_observer.wapi_home"))
        version=str(payload.get("version") or "")
        if not version:
            flash("У metrics.json немає version.","error"); return redirect(url_for("universe_observer.wapi_home"))
        rid=_save_external("OBSERVER_WAPI",version,payload,"DATASET:OBSERVER_WAPI_V07",f"WAPI-{version}")
        flash(f"WAPI result imported ✓ {rid}","ok"); return redirect(url_for("universe_observer.wapi_home"))
    snapshot=_load("observer_selftest_summary.json")
    with SessionLocal() as db:
        recent=db.scalars(select(Run).where(Run.study_key=="DATASET:OBSERVER_WAPI_V07")
                          .order_by(Run.created_at.desc()).limit(10)).all()
    return render_template("observer_wapi.html",snapshot=snapshot,recent=recent)

@bp.route("/mor",methods=["GET","POST"])
def mor_home():
    if request.method=="POST":
        _require_admin()
        try: payload=json.loads(request.form.get("payload",""))
        except Exception:
            flash("Некоректний JSON.","error"); return redirect(url_for("universe_observer.mor_home"))
        version=str(payload.get("export_version") or "")
        if not version:
            flash("У MOR export немає export_version.","error"); return redirect(url_for("universe_observer.mor_home"))
        encoded=json.dumps(payload,ensure_ascii=False).encode()
        if len(encoded)>2_000_000:
            flash("Export >2 MB. Використай compact/latest export, не full raw history.","error")
            return redirect(url_for("universe_observer.mor_home"))
        rid=_save_external("MOR_OBSERVER",version,payload,"DATASET:MOR_OBSERVER",version)
        flash(f"MOR snapshot imported ✓ {rid}","ok"); return redirect(url_for("universe_observer.mor_home"))
    snapshot=_load("mor_observer_summary.json")
    with SessionLocal() as db:
        recent=db.scalars(select(Run).where(Run.study_key=="DATASET:MOR_OBSERVER")
                          .order_by(Run.created_at.desc()).limit(10)).all()
    return render_template("mor_observer.html",snapshot=snapshot,recent=recent)
