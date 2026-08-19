
import os, json, secrets, csv, io
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, session, flash, Response, abort
from sqlalchemy import select, func
from werkzeug.middleware.proxy_fix import ProxyFix
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from db import init_db, SessionLocal, Participant, Run, Answer, BenchmarkPack, log_event, utcnow
from studies import (
    BOT_BANK, STATE_BANK, STATE_MAP, bot_item_map,
    constrained_bot_order, constrained_state_order,
    bot_base_prompt, bot_follow_prompt, state_base_prompt, state_follow_prompt
)
from metrics import bot_metrics, human_ai_metrics, benchmark_metrics
from quality_checks import OBJECTIVE_RULES, evaluate_bot_quality

BASE=Path(__file__).resolve().parent
app=Flask(__name__)
app.secret_key=os.getenv("SECRET_KEY", secrets.token_hex(32))
app.wsgi_app=ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE","1")=="1" and os.getenv("DATABASE_URL") is not None
)
ADMIN_KEY=os.getenv("ADMIN_KEY","")

init_db()

def seed_benchmarks():
    seed_files=[
        BASE/"data"/"demo_benchmark.json",
        BASE/"data"/"moral_machine_global_direction_v1.json",
        BASE/"data"/"moral_machine_boundary_stability_v1.json"
    ]

    with SessionLocal() as db:
        changed=False

        for path in seed_files:
            if not path.exists():
                continue

            pack=json.loads(
                path.read_text(
                    encoding="utf-8"
                )
            )

            row=db.get(
                BenchmarkPack,
                pack["benchmark_id"]
            )

            if not row:
                row=BenchmarkPack(
                    id=pack["benchmark_id"],
                    name=pack["name"],
                    pack_json="{}"
                )

                db.add(row)

            # Built-in packs are code-versioned.
            # Keep DB representation synchronized
            # with the repository file.
            fresh_json=json.dumps(
                pack,
                ensure_ascii=False
            )

            if (
                row.name != pack["name"]
                or row.version
                    != str(
                        pack.get(
                            "version",
                            "1"
                        )
                    )
                or row.source_name
                    != pack.get(
                        "source_name"
                    )
                or row.source_url
                    != pack.get(
                        "source_url"
                    )
                or row.license_note
                    != pack.get(
                        "license_note"
                    )
                or row.pack_json
                    != fresh_json
            ):
                row.name=pack["name"]

                row.version=str(
                    pack.get(
                        "version",
                        "1"
                    )
                )

                row.source_name=(
                    pack.get(
                        "source_name"
                    )
                )

                row.source_url=(
                    pack.get(
                        "source_url"
                    )
                )

                row.license_note=(
                    pack.get(
                        "license_note"
                    )
                )

                row.pack_json=(
                    fresh_json
                )

                changed=True

        if changed:
            db.commit()

seed_benchmarks()

from benchmark_compare import bp as benchmark_compare_bp
app.register_blueprint(benchmark_compare_bp)

from stability_compare import bp as stability_compare_bp
app.register_blueprint(stability_compare_bp)

def current_participant(db):
    pid=session.get("participant_id")
    p=db.get(Participant,pid) if pid else None
    if not p:
        pid="P-"+secrets.token_hex(6)
        p=Participant(id=pid)
        db.add(p); db.commit()
        session["participant_id"]=pid
    return p

def new_run_id():
    return "R-"+secrets.token_hex(8)


RECOVERY_SALT="participant-recovery-v1"
RECOVERY_MAX_AGE=365*24*60*60


def recovery_serializer():
    return URLSafeTimedSerializer(
        app.secret_key,
        salt=RECOVERY_SALT
    )


def make_recovery_token(participant_id):
    return recovery_serializer().dumps({
        "participant_id":participant_id
    })


def recovery_link_for(participant_id):
    return url_for(
        "recover_participant",
        token=make_recovery_token(participant_id),
        _external=True
    )

def admin_required(fn):
    @wraps(fn)
    def inner(*a,**kw):
        if not session.get("admin"):
            return redirect(url_for("admin_login", next=request.path))
        return fn(*a,**kw)
    return inner

def save_answer(db, run_id, actor, item_id, phase, obj, raw):
    q=select(Answer).where(
        Answer.run_id==run_id, Answer.actor==actor, Answer.item_id==item_id, Answer.phase==phase
    )
    row=db.scalar(q)
    if not row:
        row=Answer(run_id=run_id,actor=actor,item_id=item_id,phase=phase)
        db.add(row)
    row.choice=obj.get("choice")
    row.confidence=float(obj["confidence"]) if obj.get("confidence") is not None else None
    row.reason=str(obj.get("reason",""))[:2000] or None
    row.key_assumption=str(obj.get("key_assumption",""))[:2000] or None
    row.counterargument=str(obj.get("counterargument",""))[:2000] or None
    row.change_condition=str(obj.get("change_condition",""))[:2000] or None
    row.frame_status=obj.get("frame_status")
    row.assumption_broken=None if "assumption_broken" not in obj else int(bool(obj.get("assumption_broken")))
    row.previous_reason_still_valid=None if "previous_reason_still_valid" not in obj else int(bool(obj.get("previous_reason_still_valid")))
    row.raw_json=json.dumps(obj,ensure_ascii=False)

def parse_json_field(text):
    try:
        return json.loads((text or "").strip()), None
    except Exception as e:
        return None, f"JSON не читається: {e}"

def validate_bot_base(data):
    ans=data.get("answers") if isinstance(data,dict) else None
    ids={x["id"] for x in BOT_BANK["items"]}
    if not isinstance(ans,dict): return None, "Немає об'єкта answers."
    if set(ans)!=ids:
        return None, f"Потрібно рівно {len(ids)} відповідей. Пропущено: {sorted(ids-set(ans))}; зайві: {sorted(set(ans)-ids)}"
    for iid,a in ans.items():
        if a.get("choice") not in ("A","B","C"): return None,f"{iid}: choice має бути A/B/C."
        c=a.get("confidence")
        if not isinstance(c,(int,float)) or not 50<=c<=100: return None,f"{iid}: confidence має бути 50..100."
        if a.get("frame_status") not in ("ACCEPT","UNDERSPECIFIED","CONTRADICTORY"): return None,f"{iid}: некоректний frame_status."
    return ans,None

def validate_bot_follow(data):
    ups=data.get("updates") if isinstance(data,dict) else None
    ids={x["id"] for x in BOT_BANK["followups"]}
    if not isinstance(ups,dict): return None,"Немає об'єкта updates."
    if set(ups)!=ids:
        return None,f"Потрібно рівно {len(ids)} follow-up. Пропущено: {sorted(ids-set(ups))}; зайві: {sorted(set(ups)-ids)}"
    for iid,a in ups.items():
        if a.get("choice") not in ("A","B","C"): return None,f"{iid}: choice має бути A/B/C."
        c=a.get("confidence")
        if not isinstance(c,(int,float)) or not 50<=c<=100: return None,f"{iid}: confidence має бути 50..100."
        if not isinstance(a.get("assumption_broken"),bool): return None,f"{iid}: assumption_broken має бути true/false."
        if not isinstance(a.get("previous_reason_still_valid"),bool): return None,f"{iid}: previous_reason_still_valid має бути true/false."
        if a.get("frame_status") not in ("ACCEPT","UNDERSPECIFIED","CONTRADICTORY"): return None,f"{iid}: некоректний frame_status."
    return ups,None

def validate_state_ai(data):
    ans=data.get("answers") if isinstance(data,dict) else None
    ids=set(STATE_MAP)
    if not isinstance(ans,dict) or set(ans)!=ids:
        return None,f"Потрібно рівно {len(ids)} AI-відповідей."
    for iid,a in ans.items():
        if a.get("choice") not in ("A","B","C"): return None,f"{iid}: некоректний choice."
        c=a.get("confidence")
        if not isinstance(c,(int,float)) or not 50<=c<=100: return None,f"{iid}: confidence 50..100."
    return ans,None

def validate_state_follow(data):
    ups=data.get("updates") if isinstance(data,dict) else None
    ids={x["id"] for x in STATE_BANK["grp_base"]}
    if not isinstance(ups,dict) or set(ups)!=ids: return None,"Потрібно рівно GRP1 і GRP2."
    for iid,a in ups.items():
        if a.get("choice") not in ("A","B","C"): return None,f"{iid}: некоректний choice."
        c=a.get("confidence")
        if not isinstance(c,(int,float)) or not 50<=c<=100: return None,f"{iid}: confidence 50..100."
    return ups,None

@app.get("/")
def index():
    with SessionLocal() as db:
        pid=session.get("participant_id")
        p=db.get(Participant,pid) if pid else None
        my_runs=[]
        if p:
            my_runs=db.scalars(
                select(Run)
                .where(Run.participant_id==p.id)
                .order_by(Run.created_at.desc())
                .limit(8)
            ).all()

        people_completed=db.scalar(
            select(func.count(func.distinct(Run.participant_id)))
            .where(
                Run.status=="completed",
                ~Run.study_key.like("BENCHMARK:%")
            )
        ) or 0

        stats={
            "people_completed":people_completed,
            "runs":db.scalar(select(func.count()).select_from(Run)) or 0,
            "completed":db.scalar(select(func.count()).select_from(Run).where(Run.status=="completed")) or 0,
            "benchmarks":db.scalar(select(func.count()).select_from(BenchmarkPack)) or 0
        }

    return render_template("index.html",participant=p,runs=my_runs,stats=stats)

@app.route("/recover/<token>",methods=["GET","POST"])
def recover_participant(token):
    try:
        payload=recovery_serializer().loads(
            token,
            max_age=RECOVERY_MAX_AGE
        )
    except SignatureExpired:
        return render_template(
            "message.html",
            title="Посилання прострочене",
            message="Це recovery-посилання вже не діє."
        ),410
    except BadSignature:
        return render_template(
            "message.html",
            title="Некоректне посилання",
            message="Recovery-посилання пошкоджене або недійсне."
        ),403

    participant_id=payload.get("participant_id")

    if not participant_id:
        abort(403)

    with SessionLocal() as db:
        p=db.get(Participant,participant_id)

        if not p:
            abort(404)

        completed=db.scalar(
            select(func.count())
            .select_from(Run)
            .where(
                Run.participant_id==p.id,
                Run.status=="completed",
                ~Run.study_key.like("BENCHMARK:%")
            )
        ) or 0

        same=session.get("participant_id")==p.id

        if request.method=="POST":
            previous=session.get("participant_id")
            session["participant_id"]=p.id

            log_event(
                db,
                "PARTICIPANT_RECOVERED",
                participant_id=p.id,
                payload={
                    "previous_participant_id":
                        previous if previous!=p.id else None
                }
            )
            db.commit()

            flash(
                "Анонімний профіль відновлено ✓",
                "ok"
            )

            return redirect(url_for("index"))

    return render_template(
        "recover.html",
        participant=p,
        completed=completed,
        same=same
    )


@app.route("/bot/new",methods=["GET","POST"])
def bot_new():
    if request.method=="POST":
        with SessionLocal() as db:
            p=current_participant(db)
            seed=secrets.randbelow(2_000_000_000)
            run=Run(
                id=new_run_id(),participant_id=p.id,study_key="BOT_STRESS_V01",protocol_version="CMD1",
                provider=request.form.get("provider","Other")[:80],
                model_label=request.form.get("model_label","").strip()[:160] or "unknown",
                account_alias=request.form.get("account_alias","").strip()[:120] or None,
                personalization=request.form.get("personalization","unknown")
            )
            run.set_meta({"seed":seed,"order":constrained_bot_order(seed),"bank_version":"0.1"})
            db.add(run); log_event(db,"RUN_CREATED",run.id,p.id,{"study":"BOT_STRESS_V01"}); db.commit()
            return redirect(url_for("bot_base",run_id=run.id))
    return render_template("new_run.html",mode="bot")

@app.route("/bot/<run_id>/base",methods=["GET","POST"])
def bot_base(run_id):
    with SessionLocal() as db:
        run=db.get(Run,run_id)
        if not run: abort(404)
        p=current_participant(db)
        if run.participant_id!=p.id and not session.get("admin"): abort(403)
        order=run.meta().get("order") or constrained_bot_order(run.meta().get("seed",1))
        prompt=bot_base_prompt(order)
        if request.method=="POST":
            data,err=parse_json_field(request.form.get("json"))
            if not err:
                ans,err=validate_bot_base(data)
            if err:
                flash(err,"error"); return render_template("prompt_paste.html",title="BOT baseline",step="1/2",prompt=prompt,run=run,next_label="Перевірити й зберегти",json_value=request.form.get("json",""))
            for iid,obj in ans.items(): save_answer(db,run.id,"ai",iid,"base",obj,data)
            detected=str(data.get("model_label","")).strip()
            if detected and detected!="unknown": run.model_label=detected[:160]
            run.status="base_done"; log_event(db,"BOT_BASE_DONE",run.id,run.participant_id); db.commit()
            return redirect(url_for("bot_follow",run_id=run.id))
        return render_template("prompt_paste.html",title="BOT baseline",step="1/2",prompt=prompt,run=run,next_label="Перевірити й зберегти",json_value="")

@app.route("/bot/<run_id>/follow",methods=["GET","POST"])
def bot_follow(run_id):
    with SessionLocal() as db:
        run=db.get(Run,run_id)
        if not run: abort(404)
        p=current_participant(db)
        if run.participant_id!=p.id and not session.get("admin"): abort(403)
        if run.status=="created": return redirect(url_for("bot_base",run_id=run.id))
        prompt=bot_follow_prompt()
        if request.method=="POST":
            data,err=parse_json_field(request.form.get("json"))
            if not err:
                ups,err=validate_bot_follow(data)
            if err:
                flash(err,"error"); return render_template("prompt_paste.html",title="BOT follow-up",step="2/2",prompt=prompt,run=run,next_label="Завершити run",json_value=request.form.get("json",""),same_chat=True)
            for iid,obj in ups.items(): save_answer(db,run.id,"ai",iid,"update",obj,data)
            run.status="completed"; run.completed_at=utcnow(); log_event(db,"BOT_COMPLETED",run.id,run.participant_id); db.commit()
            return redirect(url_for("bot_result",run_id=run.id))
        return render_template("prompt_paste.html",title="BOT follow-up",step="2/2",prompt=prompt,run=run,next_label="Завершити run",json_value="",same_chat=True)

@app.get("/bot/<run_id>/result")
def bot_result(run_id):
    with SessionLocal() as db:
        run=db.get(Run,run_id)
        if not run: abort(404)
        p=current_participant(db)
        if run.participant_id!=p.id and not session.get("admin"): abort(403)
        metrics=bot_metrics(run.answers)
        recovery_link=recovery_link_for(run.participant_id)
    return render_template(
        "result_bot.html",
        run=run,
        m=metrics,
        recovery_link=recovery_link
    )


@app.get("/compare")
def compare_models():
    participant_id=session.get("participant_id")

    if not participant_id:
        return render_template(
            "message.html",
            title="Порівняння моделей",
            message="Спочатку заверши щонайменше два Bot Stress runs у цьому браузері."
        )

    with SessionLocal() as db:
        participant=db.get(Participant,participant_id)

        if not participant:
            return render_template(
                "message.html",
                title="Порівняння моделей",
                message="Participant не знайдений."
            )

        runs=db.scalars(
            select(Run)
            .where(
                Run.participant_id==participant_id,
                Run.study_key=="BOT_STRESS_V01",
                Run.status=="completed"
            )
            .order_by(Run.completed_at.asc(),Run.created_at.asc())
        ).all()

        if len(runs)<2:
            return render_template(
                "message.html",
                title="Порівняння моделей",
                message="Для порівняння потрібно щонайменше два завершені Bot Stress runs."
            )

        # ---------------------------
        # Unique display labels
        # ---------------------------
        label_counts={}
        for r in runs:
            key=r.model_label or "unknown"
            label_counts[key]=label_counts.get(key,0)+1

        seen={}
        cards=[]
        base_maps={}
        update_maps={}
        metric_maps={}
        quality_maps={}
        display_labels={}

        for r in runs:
            model=r.model_label or "unknown"

            seen[model]=seen.get(model,0)+1
            display=model

            if label_counts[model]>1:
                display=f"{model} #{seen[model]}"

            base={}
            update={}

            for a in r.answers:
                if a.actor!="ai":
                    continue

                if a.phase=="base":
                    base[a.item_id]=a
                elif a.phase=="update":
                    update[a.item_id]=a

            metrics=bot_metrics(r.answers)
            quality=evaluate_bot_quality(base,update)

            base_maps[r.id]=base
            update_maps[r.id]=update
            metric_maps[r.id]=metrics
            quality_maps[r.id]=quality
            display_labels[r.id]=display

            def pct(v):
                return None if v is None else round(v*100,1)

            cards.append({
                "run_id":r.id,
                "model":display,
                "provider":r.provider or "—",
                "account":r.account_alias or "—",
                "personalization":r.personalization or "—",

                "state":
                    f"{metrics['state_flips']}/{metrics['state_pairs_n']}",

                "state_rate":
                    pct(metrics.get("state_flip_rate")),

                "frame":
                    pct(metrics.get("frame_nonaccept_rate")),

                "follow":
                    f"{metrics['followup_choice_changes']}/"
                    f"{metrics['followup_comparable_n']}",

                "follow_rate":
                    pct(metrics.get("followup_change_rate")),

                "confidence":
                    metrics.get("mean_confidence"),

                "third_path":
                    pct(metrics.get("c_choice_rate")),

                "formal_choice":
                    f"{quality['choice_correct']}/{quality['total']}",

                "formal_frame":
                    f"{quality['frame_correct']}/{quality['total']}",

                "coherence_flags":
                    quality["coherence_mismatches"],

                "coherence_checked":
                    quality["coherence_checked"]
            })

        # ---------------------------
        # 20 base scenarios
        # ---------------------------
        scenario_rows=[]
        unanimous=0
        split=0

        for item in BOT_BANK["items"]:
            iid=item["id"]

            cells=[]
            choices=[]

            for r in runs:
                a=base_maps[r.id].get(iid)

                if a:
                    choices.append(a.choice)

                    cells.append({
                        "choice":a.choice,
                        "confidence":
                            int(a.confidence)
                            if a.confidence is not None
                            else None,
                        "frame":a.frame_status or "—"
                    })
                else:
                    cells.append({
                        "choice":"—",
                        "confidence":None,
                        "frame":"—"
                    })

            is_unanimous=(
                len(choices)==len(runs)
                and len(set(choices))==1
            )

            if is_unanimous:
                unanimous+=1
            else:
                split+=1

            scenario_rows.append({
                "id":iid,
                "family":item.get("family",""),
                "text":item.get("text",""),
                "unanimous":is_unanimous,
                "cells":cells
            })

        # ---------------------------
        # Pairwise agreement
        # ---------------------------
        agreement=[]
        agreement_values=[]

        for i in range(len(runs)):
            for j in range(i+1,len(runs)):
                left=runs[i]
                right=runs[j]

                same=0
                total=0

                for item in BOT_BANK["items"]:
                    iid=item["id"]

                    a=base_maps[left.id].get(iid)
                    b=base_maps[right.id].get(iid)

                    if a and b:
                        total+=1
                        same+=int(a.choice==b.choice)

                rate=round(same/total*100,1) if total else None

                if rate is not None:
                    agreement_values.append(rate)

                agreement.append({
                    "left":display_labels[left.id],
                    "right":display_labels[right.id],
                    "same":same,
                    "total":total,
                    "rate":rate
                })

        mean_agreement=(
            round(
                sum(agreement_values)/len(agreement_values),
                1
            )
            if agreement_values
            else None
        )

        # ---------------------------
        # Follow-up transitions
        # ---------------------------
        follow_rows=[]

        for f in BOT_BANK["followups"]:
            iid=f["id"]

            cells=[]

            for r in runs:
                prev=base_maps[r.id].get(iid)
                upd=update_maps[r.id].get(iid)

                update_lookup={
                    x["item_id"]:x
                    for x in metric_maps[r.id].get("followups",[])
                }

                classified=update_lookup.get(iid,{})

                cells.append({
                    "before":
                        prev.choice if prev else "—",

                    "after":
                        upd.choice if upd else "—",

                    "type":
                        classified.get("update_type","—"),

                    "delta":
                        classified.get("confidence_delta")
                })

            follow_rows.append({
                "id":iid,
                "cells":cells
            })

        # ---------------------------
        # Formal / objective checks
        # ---------------------------
        objective_rows=[]
        coherence_anomalies=[]

        for rule in OBJECTIVE_RULES:
            cells=[]

            for r in runs:
                q=quality_maps[r.id]

                qrow=next(
                    (
                        x for x in q["rows"]
                        if x["key"]==rule["key"]
                    ),
                    None
                )

                if not qrow:
                    qrow={
                        "actual_choice":"—",
                        "actual_frame":"—",
                        "choice_ok":None,
                        "frame_ok":None,
                        "reason_choice":None,
                        "coherence":None,
                        "reason":""
                    }

                cells.append(qrow)

                if qrow.get("coherence") is False:
                    coherence_anomalies.append({
                        "model":display_labels[r.id],
                        "check":rule["key"],
                        "choice":qrow["actual_choice"],
                        "reason_choice":qrow["reason_choice"],
                        "reason":qrow["reason"]
                    })

            objective_rows.append({
                "key":rule["key"],
                "item_id":rule["item_id"],
                "phase":rule["phase"],
                "label":rule["label"],
                "expected_choice":rule["expected_choice"],
                "expected_frame":rule["expected_frame"],
                "cells":cells
            })

        total_formal_choice_errors=sum(
            q["total"]-q["choice_correct"]
            for q in quality_maps.values()
        )

        total_formal_frame_errors=sum(
            q["total"]-q["frame_correct"]
            for q in quality_maps.values()
        )

        summary={
            "models":len(runs),
            "unanimous":unanimous,
            "split":split,
            "mean_agreement":mean_agreement,
            "formal_choice_errors":total_formal_choice_errors,
            "formal_frame_errors":total_formal_frame_errors,
            "coherence_flags":len(coherence_anomalies)
        }

        participant_label=participant.label or participant.id

    return render_template(
        "compare_models.html",
        participant_label=participant_label,
        cards=cards,
        scenario_rows=scenario_rows,
        follow_rows=follow_rows,
        agreement=agreement,
        objective_rows=objective_rows,
        coherence_anomalies=coherence_anomalies,
        summary=summary
    )


@app.route("/human-ai/new",methods=["GET","POST"])
def hai_new():
    if request.method=="POST":
        with SessionLocal() as db:
            p=current_participant(db); seed=secrets.randbelow(2_000_000_000)
            order=constrained_state_order(seed)
            ai_order=constrained_state_order(seed+1000003)
            run=Run(
                id=new_run_id(),participant_id=p.id,study_key="HUMAN_AI_STATE_V1",protocol_version="CMD1",
                provider=request.form.get("provider","ChatGPT")[:80],
                model_label=request.form.get("model_label","unknown")[:160],
                account_alias=request.form.get("account_alias","").strip()[:120] or None,
                personalization=request.form.get("personalization","usual")
            )
            run.set_meta({"seed":seed,"human_order":order,"ai_order":ai_order})
            db.add(run); log_event(db,"RUN_CREATED",run.id,p.id,{"study":"HUMAN_AI_STATE_V1"}); db.commit()
            return redirect(url_for("hai_human",run_id=run.id))
    return render_template("new_run.html",mode="human_ai")

@app.route("/human-ai/<run_id>/human",methods=["GET","POST"])
def hai_human(run_id):
    with SessionLocal() as db:
        run=db.get(Run,run_id); 
        if not run: abort(404)
        p=current_participant(db)
        if run.participant_id!=p.id and not session.get("admin"): abort(403)
        order=run.meta()["human_order"]
        if request.method=="POST":
            for iid in order:
                ch=request.form.get("choice_"+iid); cf=request.form.get("conf_"+iid)
                if ch not in ("A","B","C") or not cf:
                    flash("Відповідай на всі питання і confidence.","error"); break
            else:
                for iid in order:
                    save_answer(db,run.id,"human",iid,"base",{"choice":request.form["choice_"+iid],"confidence":int(request.form["conf_"+iid])}, {})
                run.status="human_base_done"; db.commit()
                return redirect(url_for("hai_human_follow",run_id=run.id))
        items=[STATE_MAP[i] for i in order]
        return render_template("human_form.html",run=run,items=items,title="1/4 — Твої відповіді",follow=False)

@app.route("/human-ai/<run_id>/human-follow",methods=["GET","POST"])
def hai_human_follow(run_id):
    with SessionLocal() as db:
        run=db.get(Run,run_id)
        if not run: abort(404)
        p=current_participant(db)
        if run.participant_id!=p.id and not session.get("admin"): abort(403)
        groups=STATE_BANK["grp_base"]
        if request.method=="POST":
            for g in groups:
                iid=g["id"]; ch=request.form.get("choice_"+iid); cf=request.form.get("conf_"+iid)
                if ch not in ("A","B","C") or not cf:
                    flash("Відповідай на обидва follow-up.","error"); break
            else:
                for g in groups:
                    iid=g["id"]; save_answer(db,run.id,"human",iid,"update",{"choice":request.form["choice_"+iid],"confidence":int(request.form["conf_"+iid])},{})
                run.status="human_done"; db.commit()
                return redirect(url_for("hai_ai_base",run_id=run.id))
        return render_template("human_form.html",run=run,items=groups,title="2/4 — Нові факти",follow=True)

@app.route("/human-ai/<run_id>/ai",methods=["GET","POST"])
def hai_ai_base(run_id):
    with SessionLocal() as db:
        run=db.get(Run,run_id)
        if not run: abort(404)
        p=current_participant(db)
        if run.participant_id!=p.id and not session.get("admin"): abort(403)
        prompt=state_base_prompt(run.meta()["ai_order"])
        if request.method=="POST":
            data,err=parse_json_field(request.form.get("json"))
            if not err: ans,err=validate_state_ai(data)
            if err:
                flash(err,"error")
            else:
                for iid,obj in ans.items(): save_answer(db,run.id,"ai",iid,"base",obj,data)
                detected=str(data.get("model_label","")).strip()
                if detected and detected!="unknown": run.model_label=detected[:160]
                run.status="ai_base_done"; db.commit(); return redirect(url_for("hai_ai_follow",run_id=run.id))
        return render_template("prompt_paste.html",title="3/4 — AI baseline",step="3/4",prompt=prompt,run=run,next_label="Перевірити AI JSON",json_value=request.form.get("json",""))

@app.route("/human-ai/<run_id>/ai-follow",methods=["GET","POST"])
def hai_ai_follow(run_id):
    with SessionLocal() as db:
        run=db.get(Run,run_id)
        if not run: abort(404)
        p=current_participant(db)
        if run.participant_id!=p.id and not session.get("admin"): abort(403)
        prompt=state_follow_prompt()
        if request.method=="POST":
            data,err=parse_json_field(request.form.get("json"))
            if not err: ups,err=validate_state_follow(data)
            if err: flash(err,"error")
            else:
                for iid,obj in ups.items(): save_answer(db,run.id,"ai",iid,"update",obj,data)
                run.status="completed"; run.completed_at=utcnow(); db.commit(); return redirect(url_for("hai_result",run_id=run.id))
        return render_template("prompt_paste.html",title="4/4 — AI follow-up",step="4/4",prompt=prompt,run=run,next_label="Завершити пару",json_value=request.form.get("json",""),same_chat=True)

@app.get("/human-ai/<run_id>/result")
def hai_result(run_id):
    with SessionLocal() as db:
        run=db.get(Run,run_id)
        if not run: abort(404)
        p=current_participant(db)
        if run.participant_id!=p.id and not session.get("admin"): abort(403)
        metrics=human_ai_metrics(run.answers)
        recovery_link=recovery_link_for(run.participant_id)
    return render_template(
        "result_hai.html",
        run=run,
        m=metrics,
        recovery_link=recovery_link
    )

@app.get("/benchmarks")
def benchmarks():
    sources=json.loads((BASE/"data"/"benchmark_sources.json").read_text(encoding="utf-8"))
    with SessionLocal() as db:
        packs=db.scalars(select(BenchmarkPack).order_by(BenchmarkPack.created_at.desc())).all()
    return render_template("benchmarks.html",packs=packs,sources=sources)

@app.route("/benchmark/<pack_id>/new",methods=["GET","POST"])
def benchmark_new(pack_id):
    with SessionLocal() as db:
        pack=db.get(BenchmarkPack,pack_id)
        if not pack: abort(404)
        if request.method=="POST":
            p=current_participant(db)
            run=Run(id=new_run_id(),participant_id=p.id,study_key="BENCHMARK:"+pack_id,protocol_version="CMD1",
                    provider=request.form.get("provider","Other")[:80],model_label=request.form.get("model_label","unknown")[:160],
                    account_alias=request.form.get("account_alias","")[:120] or None,personalization=request.form.get("personalization","unknown"))
            order=[x["id"] for x in pack.pack().get("items",[])]
            secrets.SystemRandom().shuffle(order)
            run.set_meta({
                "benchmark_id":pack_id,
                "item_order":order
            })
            db.add(run); db.commit()
            return redirect(url_for("benchmark_run",run_id=run.id))
        return render_template("new_run.html",mode="benchmark",pack=pack)

def benchmark_prompt(pack, order=None):
    blocks=[]
    items=pack["items"]

    if order:
        lookup={x["id"]:x for x in items}
        ordered=[
            lookup[iid]
            for iid in order
            if iid in lookup
        ]

        if len(ordered)==len(items):
            items=ordered

    for x in items:
        opts=" | ".join(f"{k}: {v}" for k,v in x["options"].items())
        blocks.append(f"{x['id']}. {x['text']}\nOPTIONS: {opts}")
    return """AI BENCHMARK RUN

Відповідай на кожен пункт незалежно.
- обери рівно A/B/C;
- confidence 50..100;
- reason максимум одне коротке речення;
- не використовуй приватні факти користувача;
- поверни тільки JSON.

Формат:
{"answers":{"ITEM1":{"choice":"A","confidence":80,"reason":"..."}},"model_label":"назва моделі або unknown"}

"""+"\n\n".join(blocks)

@app.route("/benchmark/run/<run_id>",methods=["GET","POST"])
def benchmark_run(run_id):
    with SessionLocal() as db:
        run=db.get(Run,run_id); 
        if not run: abort(404)
        p=current_participant(db)
        if run.participant_id!=p.id and not session.get("admin"): abort(403)
        pack_id=run.meta()["benchmark_id"]; packrow=db.get(BenchmarkPack,pack_id); pack=packrow.pack()
        prompt=benchmark_prompt(
            pack,
            run.meta().get("item_order")
        )
        if request.method=="POST":
            data,err=parse_json_field(request.form.get("json"))
            ans=data.get("answers") if not err and isinstance(data,dict) else None
            ids={x["id"] for x in pack["items"]}
            if not isinstance(ans,dict) or set(ans)!=ids: err=f"Потрібно рівно {len(ids)} benchmark-відповідей."
            if not err:
                for iid,obj in ans.items():
                    c=obj.get("confidence")

                    if (
                        obj.get("choice") not in ("A","B","C")
                        or not isinstance(c,(int,float))
                        or not 50 <= c <= 100
                    ):
                        err=f"{iid}: choice A/B/C, confidence 50..100."
                        break
            if err:
                flash(err,"error")
            else:
                for iid,obj in ans.items(): save_answer(db,run.id,"ai",iid,"base",obj,data)
                detected=str(data.get("model_label","")).strip()
                if detected and detected!="unknown": run.model_label=detected[:160]
                run.status="completed"; run.completed_at=utcnow(); db.commit()
                return redirect(url_for("benchmark_result",run_id=run.id))
        return render_template("prompt_paste.html",title=packrow.name,step="Benchmark",prompt=prompt,run=run,next_label="Завершити benchmark",json_value=request.form.get("json",""))

@app.get("/benchmark/result/<run_id>")
def benchmark_result(run_id):
    with SessionLocal() as db:
        run=db.get(Run,run_id); 
        if not run: abort(404)
        p=current_participant(db)
        if run.participant_id!=p.id and not session.get("admin"): abort(403)
        packrow=db.get(BenchmarkPack,run.meta()["benchmark_id"])
        packdata=packrow.pack()
        m=benchmark_metrics(packdata,run.answers)
    return render_template(
        "result_benchmark.html",
        run=run,
        pack=packrow,
        packdata=packdata,
        m=m
    )

@app.route("/admin/login",methods=["GET","POST"])
def admin_login():
    if request.method=="POST":
        if ADMIN_KEY and secrets.compare_digest(request.form.get("key",""),ADMIN_KEY):
            session["admin"]=True; return redirect(request.args.get("next") or url_for("admin"))
        flash("Невірний ADMIN_KEY або він не налаштований.","error")
    return render_template("admin_login.html")

@app.get("/admin/logout")
def admin_logout():
    session.pop("admin",None); return redirect(url_for("index"))

@app.get("/admin")
@admin_required
def admin():
    with SessionLocal() as db:
        runs=db.scalars(select(Run).order_by(Run.created_at.desc()).limit(250)).all()
        complete=[r for r in runs if r.status=="completed"]
        bot_complete=[r for r in complete if r.study_key=="BOT_STRESS_V01"]
        by_model={}
        for r in bot_complete:
            mm=bot_metrics(r.answers); key=r.model_label or "unknown"
            by_model.setdefault(key,[]).append(mm)
        model_rows=[]
        for model,ms in by_model.items():
            def avg(key):
                xs=[m[key] for m in ms if m.get(key) is not None]
                return round(sum(xs)/len(xs),3) if xs else None
            model_rows.append({"model":model,"n":len(ms),"state":avg("state_flip_rate"),"frame":avg("frame_nonaccept_rate"),"follow":avg("followup_change_rate"),"conf":avg("mean_confidence")})
        stats={"runs":len(runs),"completed":len(complete),"bot_complete":len(bot_complete)}
    return render_template("admin.html",runs=runs,model_rows=sorted(model_rows,key=lambda x:-x["n"]),stats=stats)

@app.route("/admin/import-bot",methods=["GET","POST"])
@admin_required
def admin_import_bot():
    if request.method=="POST":
        base,err=parse_json_field(request.form.get("base_json"))
        follow=None
        if not err:
            ans,err=validate_bot_base(base)
        follow_text=request.form.get("follow_json","").strip()
        if follow_text and not err:
            follow,err=parse_json_field(follow_text)
            if not err: ups,err=validate_bot_follow(follow)
        if err:
            flash(err,"error")
        else:
            with SessionLocal() as db:
                label=request.form.get("participant_label","").strip()[:120]

                p=None
                if label:
                    p=db.scalar(
                        select(Participant)
                        .where(func.lower(Participant.label)==label.lower())
                        .limit(1)
                    )

                if not p:
                    p=Participant(
                        id="P-"+secrets.token_hex(6),
                        label=label or "legacy"
                    )
                    db.add(p)
                    db.flush()
                run=Run(id=new_run_id(),participant_id=p.id,study_key="BOT_STRESS_V01",protocol_version="LEGACY_MANUAL",
                        provider=request.form.get("provider","Other")[:80],model_label=request.form.get("model_label","unknown")[:160],
                        account_alias=request.form.get("account_alias","")[:120] or None,personalization=request.form.get("personalization","unknown"),
                        status="completed" if follow else "base_done")
                run.set_meta({"legacy_import":True}); db.add(run); db.flush()
                for iid,obj in ans.items(): save_answer(db,run.id,"ai",iid,"base",obj,base)
                if follow:
                    for iid,obj in ups.items(): save_answer(db,run.id,"ai",iid,"update",obj,follow)
                    run.completed_at=utcnow()
                db.commit()
                flash(f"Імпортовано {run.id}.","ok")
                return redirect(url_for("admin"))
    return render_template("admin_import.html")

@app.route("/admin/benchmark-import",methods=["GET","POST"])
@admin_required
def admin_benchmark_import():
    if request.method=="POST":
        data,err=parse_json_field(request.form.get("pack_json"))
        required={"benchmark_id","name","items"}
        if not err and (not isinstance(data,dict) or not required.issubset(data)): err="Pack має містити benchmark_id, name, items."
        if not err:
            for x in data["items"]:
                if not {"id","text","options"}.issubset(x): err="Кожен item має id, text, options."; break
        if err: flash(err,"error")
        else:
            with SessionLocal() as db:
                row=db.get(BenchmarkPack,data["benchmark_id"])
                if not row:
                    row=BenchmarkPack(id=data["benchmark_id"],name=data["name"],pack_json="{}"); db.add(row)
                row.name=data["name"][:200]; row.version=str(data.get("version","1"))[:40]
                row.source_name=str(data.get("source_name",""))[:200] or None; row.source_url=data.get("source_url")
                row.license_note=data.get("license_note"); row.pack_json=json.dumps(data,ensure_ascii=False)
                db.commit()
            flash("Benchmark pack імпортовано.","ok"); return redirect(url_for("benchmarks"))
    return render_template("benchmark_import.html")

@app.get("/admin/export/runs.csv")
@admin_required
def export_runs():
    with SessionLocal() as db:
        rows=db.scalars(select(Run).order_by(Run.created_at)).all()
        sio=io.StringIO(); w=csv.writer(sio)
        w.writerow(["run_id","participant_id","study_key","protocol_version","provider","model_label","account_alias","personalization","status","created_at","completed_at"])
        for r in rows: w.writerow([r.id,r.participant_id,r.study_key,r.protocol_version,r.provider,r.model_label,r.account_alias,r.personalization,r.status,r.created_at,r.completed_at])
    return Response(sio.getvalue(),mimetype="text/csv",headers={"Content-Disposition":"attachment; filename=runs.csv"})

@app.get("/admin/export/answers.csv")
@admin_required
def export_answers():
    with SessionLocal() as db:
        rows=db.scalars(select(Answer).order_by(Answer.run_id,Answer.id)).all()
        sio=io.StringIO(); w=csv.writer(sio)
        w.writerow(["run_id","actor","item_id","phase","choice","confidence","reason","key_assumption","counterargument","change_condition","frame_status","assumption_broken","previous_reason_still_valid"])
        for a in rows: w.writerow([a.run_id,a.actor,a.item_id,a.phase,a.choice,a.confidence,a.reason,a.key_assumption,a.counterargument,a.change_condition,a.frame_status,a.assumption_broken,a.previous_reason_still_valid])
    return Response(sio.getvalue(),mimetype="text/csv",headers={"Content-Disposition":"attachment; filename=answers.csv"})

@app.errorhandler(404)
def not_found(e): return render_template("message.html",title="404",message="Сторінку не знайдено."),404
@app.errorhandler(403)
def forbidden(e): return render_template("message.html",title="403",message="Цей run належить іншій сесії."),403

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","5000")),debug=True)
