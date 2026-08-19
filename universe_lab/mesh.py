from pathlib import Path
from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from sqlalchemy import select
from db import SessionLocal, Run
from .common import ensure_participant, create_run, complete_run

bp=Blueprint("universe_mesh",__name__,url_prefix="/mesh-lab",template_folder="templates")
BASE=Path(__file__).resolve().parent
VERSION="0.7"

@bp.route("/",methods=["GET","POST"])
def home():
    if request.method=="POST":
        with SessionLocal() as db:
            p=ensure_participant(db)
            meta={
                "version":VERSION,
                "device_a":request.form.get("device_a","").strip()[:120],
                "device_b":request.form.get("device_b","").strip()[:120],
                "hello_a":request.form.get("hello_a")=="1",
                "hello_b":request.form.get("hello_b")=="1",
                "delivered":request.form.get("delivered")=="1",
                "ack_verified":request.form.get("ack")=="1",
                "queue_gc":request.form.get("queue_gc")=="1",
                "notes":request.form.get("notes","").strip()[:2000],
            }
            meta["result"]="PASS" if all(meta[k] for k in ["hello_a","hello_b","delivered","ack_verified","queue_gc"]) else "FAIL"
            run=create_run(db,p,"MOR_MESH_FIELD_V07","MOR-MESH-0.7",meta)
            complete_run(run); db.commit()
            flash(f"Field run saved: {run.id} · {meta['result']}","ok")
            return redirect(url_for("universe_mesh.home"))
    test_results=(BASE/"data"/"mesh_test_results.txt").read_text(encoding="utf-8")
    with SessionLocal() as db:
        recent=db.scalars(select(Run).where(Run.study_key=="MOR_MESH_FIELD_V07")
                          .order_by(Run.created_at.desc()).limit(20)).all()
    return render_template("mesh.html",version=VERSION,test_results=test_results,recent=recent)

@bp.get("/run/<run_id>")
def result(run_id):
    with SessionLocal() as db:
        run=db.get(Run,run_id)
        if not run or run.study_key!="MOR_MESH_FIELD_V07": return ("Not found",404)
        if not session.get("admin") and run.participant_id!=session.get("participant_id"): return ("Forbidden",403)
        meta=run.meta()
    return render_template("mesh_result.html",run=run,meta=meta)
