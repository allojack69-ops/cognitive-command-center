import json, random, secrets
from flask import Blueprint, abort, jsonify, render_template, request, session, url_for
from sqlalchemy import func, select
from db import SessionLocal, Run, utcnow
from .common import ensure_participant, create_run, complete_run
from .models import GRPSession, GRPTrial

bp=Blueprint("universe_grp",__name__,url_prefix="/grp",template_folder="templates")
EXPERIMENT_VERSION="CD-GRP-0.5.2-RESET-WASHOUT"
CONSENT_VERSION="2026-08-09-v1"
SUPPORTED_LANGS={"uk","en","cs","vi"}
SEEDS={
"uk":["вода","дорога","світло","дім","час","вікно","дерево","вибір"],
"en":["water","road","light","home","time","window","tree","choice"],
"cs":["voda","cesta","světlo","domov","čas","okno","strom","volba"],
"vi":["nước","con đường","ánh sáng","nhà","thời gian","cửa sổ","cây","lựa chọn"],
}
ATTENTION_CONDITIONS=["sensory","future","social","self"]

def make_plan(language):
    rng=random.SystemRandom()
    group=rng.choice(["active","sham"])
    assigned_cue=rng.choice(ATTENTION_CONDITIONS)
    initial_seed,early_reset_seed,late_reset_seed=rng.sample(SEEDS[language],3)
    plan=[]
    for step in range(32):
        if step<10: phase="baseline"
        elif step<14: phase="intervention" if group=="active" else "sham_window"
        elif step<20: phase="early_washout"
        else: phase="late_washout"
        is_probe=bool(group=="active" and 10<=step<14)
        condition=assigned_cue if is_probe else "free"
        if step==0: seed,reset_stage=initial_seed,"initial"
        elif step==14: seed,reset_stage=early_reset_seed,"early_reset"
        elif step==20: seed,reset_stage=late_reset_seed,"late_reset"
        else: seed,reset_stage=None,"none"
        plan.append({"global_index":step,"block_index":0,"step_index":step,"condition":condition,
                     "is_probe":is_probe,"seed":seed,"phase":phase,"block_role":group,
                     "assigned_cue":assigned_cue,"intervention_onset":10,"intervention_length":4,
                     "washout_length":18,"reset_stage":reset_stage})
    return plan

def _owned(db,sid):
    obj=db.get(GRPSession,sid)
    if not obj: abort(404)
    if not session.get("admin") and obj.participant_id!=session.get("participant_id"): abort(403)
    return obj

def _state(db,obj):
    plan=obj.plan()
    count=db.scalar(select(func.count(GRPTrial.id)).where(GRPTrial.session_id==obj.id)) or 0
    if count>=len(plan):
        return {"session_id":obj.id,"completed":True,"total_steps":len(plan)}
    item=plan[count]
    if item.get("seed") is not None:
        stimulus=item["seed"]
    else:
        prev=db.scalar(select(GRPTrial).where(GRPTrial.session_id==obj.id)
                       .order_by(GRPTrial.global_index.desc()).limit(1))
        if not prev: abort(409,"Broken session state")
        stimulus=prev.response
    return {"session_id":obj.id,"global_index":item["global_index"],"block_index":item["block_index"],
            "step_index":item["step_index"],"total_steps":len(plan),"stimulus":stimulus,
            "attention_condition":item["condition"],"is_probe":item["is_probe"],"phase":item["phase"],
            "block_role":item["block_role"],"assigned_cue":item["assigned_cue"],
            "reset_stage":item["reset_stage"],"completed":False}

@bp.get("/")
def home():
    return render_template("grp.html",experiment_version=EXPERIMENT_VERSION)

@bp.post("/api/session/start")
def start_session():
    data=request.get_json(silent=True) or {}
    if data.get("consent") is not True or data.get("age_18") is not True:
        return jsonify({"detail":"18+ confirmation and voluntary consent are required."}),422
    language=str(data.get("language") or "uk").lower()
    if language not in SUPPORTED_LANGS:
        return jsonify({"detail":"Unsupported language"}),422
    with SessionLocal() as db:
        p=ensure_participant(db)
        plan=make_plan(language)
        sid=secrets.token_hex(16)
        run=create_run(db,p,"CD_GRP_V052",EXPERIMENT_VERSION,{
            "grp_session_id":sid,"group":plan[0]["block_role"],"assigned_cue":plan[0]["assigned_cue"],
            "source":"UNIVERSE_LAB"})
        obj=GRPSession(id=sid,run_id=run.id,participant_id=p.id,experiment_version=EXPERIMENT_VERSION,
            consent_version=CONSENT_VERSION,consent=True,age_18=True,language=language,
            country=str(data.get("country") or "").strip()[:80] or None,
            education=str(data.get("education") or "").strip()[:80] or None,
            plan_json=json.dumps(plan,ensure_ascii=False))
        db.add(obj); db.commit()
        return jsonify(_state(db,obj))

@bp.get("/api/session/<sid>/state")
def get_state(sid):
    with SessionLocal() as db:
        return jsonify(_state(db,_owned(db,sid)))

@bp.post("/api/session/<sid>/answer")
def answer(sid):
    data=request.get_json(silent=True) or {}
    response_text=" ".join(str(data.get("response") or "").strip().split())
    if not response_text or len(response_text)>120:
        return jsonify({"detail":"Response must be 1..120 characters"}),422
    try: reaction_ms=float(data.get("reaction_ms"))
    except Exception: return jsonify({"detail":"reaction_ms must be numeric"}),422
    if not 100<=reaction_ms<=300000:
        return jsonify({"detail":"reaction_ms out of range"}),422
    focus_lost=bool(data.get("focus_lost",False))
    with SessionLocal() as db:
        obj=_owned(db,sid)
        if obj.completed:
            return jsonify({"detail":"Session already completed"}),409
        st=_state(db,obj)
        db.add(GRPTrial(session_id=obj.id,block_index=st["block_index"],step_index=st["step_index"],
            global_index=st["global_index"],stimulus=st["stimulus"],response=response_text,
            attention_condition=st["attention_condition"],is_probe=st["is_probe"],
            reaction_ms=reaction_ms,focus_lost=focus_lost))
        db.flush()
        nxt=_state(db,obj)
        if nxt.get("completed"):
            obj.completed=True; obj.completed_at=utcnow()
            run=db.get(Run,obj.run_id)
            complete_run(run,{"grp_completed":True,"grp_session_id":obj.id})
            db.commit()
            return jsonify({"session_id":obj.id,"completed":True,"total_steps":32,
                            "result_url":url_for("universe_grp.result",sid=obj.id)})
        db.commit()
        return jsonify(nxt)

@bp.get("/session/<sid>/result")
def result(sid):
    with SessionLocal() as db:
        obj=_owned(db,sid)
        n=db.scalar(select(func.count(GRPTrial.id)).where(GRPTrial.session_id==obj.id)) or 0
        focus=db.scalar(select(func.count(GRPTrial.id)).where(GRPTrial.session_id==obj.id,
                     GRPTrial.focus_lost.is_(True))) or 0
        run=db.get(Run,obj.run_id)
    return render_template("grp_result.html",s=obj,run=run,trial_count=n,focus_lost_n=focus)

@bp.get("/recruitment")
def recruitment():
    with SessionLocal() as db:
        total=db.scalar(select(func.count()).select_from(GRPSession)) or 0
        rows=db.scalars(select(GRPSession).where(GRPSession.completed.is_(True))).all()
        completed=len(rows); active=sham=0
        for row in rows:
            group=row.plan()[0].get("block_role")
            active+=int(group=="active"); sham+=int(group=="sham")
    return render_template("grp_recruitment.html",total=total,completed=completed,
                           active=active,sham=sham,target=180,cap=300,
                           experiment_version=EXPERIMENT_VERSION)
