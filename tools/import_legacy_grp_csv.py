"""Import legacy Cognitive Dynamics v0.5.2 CSV exports into the unified lab."""
import argparse,csv,hashlib,json
from datetime import datetime
from pathlib import Path
from db import SessionLocal,Participant
from universe_lab.models import GRPSession,GRPTrial,init_lab_models
from universe_lab.common import create_run,complete_run

def truth(v): return str(v).strip().lower() in {"1","true","yes","y"}
def dt(v):
    v=(v or "").strip()
    return datetime.fromisoformat(v.replace("Z","+00:00")) if v else None
def pid_for(sid): return "P-GRP-"+hashlib.sha256(sid.encode()).hexdigest()[:12]

def reconstruct(s,trials):
    by={int(r["global_index"]):r for r in trials}; out=[]
    for i in range(32):
        r=by.get(i,{})
        out.append({"global_index":i,"block_index":int(r.get("block_index") or 0),
        "step_index":int(r.get("step_index") or i),"condition":r.get("attention_condition") or "free",
        "is_probe":truth(r.get("is_probe")),"seed":r.get("stimulus") if i in {0,14,20} else None,
        "phase":r.get("phase") or "legacy","block_role":r.get("participant_group") or s.get("participant_group") or "legacy",
        "assigned_cue":r.get("assigned_cue") or s.get("assigned_cue") or "legacy",
        "intervention_onset":int(r.get("intervention_onset") or 10),"intervention_length":int(r.get("intervention_length") or 4),
        "washout_length":int(r.get("washout_length") or 18),
        "reset_stage":r.get("reset_stage") or ("initial" if i==0 else "early_reset" if i==14 else "late_reset" if i==20 else "none")})
    return out

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--sessions",required=True); ap.add_argument("--trials",required=True); a=ap.parse_args()
    init_lab_models()
    ss=list(csv.DictReader(Path(a.sessions).open(encoding="utf-8-sig")))
    tt=list(csv.DictReader(Path(a.trials).open(encoding="utf-8-sig")))
    by={}
    for r in tt: by.setdefault(r["session_id"],[]).append(r)
    imported=skipped=0
    with SessionLocal() as db:
        for s in ss:
            sid=s["session_id"]
            if db.get(GRPSession,sid): skipped+=1; continue
            pid=pid_for(sid); p=db.get(Participant,pid)
            if not p: p=Participant(id=pid,label="legacy GRP session"); db.add(p); db.flush()
            tr=sorted(by.get(sid,[]),key=lambda x:int(x["global_index"]))
            run=create_run(db,p,"CD_GRP_V052",s.get("experiment_version") or "CD-GRP-0.5.2-RESET-WASHOUT",
                           {"grp_session_id":sid,"legacy_import":True,"group":s.get("participant_group"),"assigned_cue":s.get("assigned_cue")})
            obj=GRPSession(id=sid,run_id=run.id,participant_id=p.id,created_at=dt(s.get("started_at")) or run.created_at,
                completed_at=dt(s.get("completed_at")),completed=truth(s.get("completed")),
                experiment_version=s.get("experiment_version") or "CD-GRP-0.5.2-RESET-WASHOUT",
                consent_version="legacy-export",consent=True,age_18=True,language=s.get("language") or "uk",
                country=s.get("country") or None,education=s.get("education") or None,
                plan_json=json.dumps(reconstruct(s,tr),ensure_ascii=False),legacy_source="cognitive-dynamics-grp")
            db.add(obj); db.flush()
            for r in tr:
                db.add(GRPTrial(session_id=sid,block_index=int(r.get("block_index") or 0),
                    step_index=int(r.get("step_index") or r["global_index"]),global_index=int(r["global_index"]),
                    stimulus=r.get("stimulus") or "",response=r.get("response") or "",
                    attention_condition=r.get("attention_condition") or "free",is_probe=truth(r.get("is_probe")),
                    reaction_ms=float(r.get("reaction_ms") or 0),focus_lost=truth(r.get("focus_lost")),
                    created_at=dt(r.get("created_at")) or run.created_at))
            if obj.completed:
                complete_run(run,{"legacy_import":True,"grp_completed":True})
                if obj.completed_at: run.completed_at=obj.completed_at
            db.commit(); imported+=1
    print(f"OK imported={imported} skipped_existing={skipped}")
if __name__=="__main__": main()
