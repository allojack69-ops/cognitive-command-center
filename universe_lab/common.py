import json, secrets
from flask import session
from db import Participant, Run, utcnow

SYSTEM_PARTICIPANT_ID = "P-LAB-SYSTEM"

def ensure_participant(db):
    pid=session.get("participant_id")
    p=db.get(Participant,pid) if pid else None
    if p:
        return p
    p=Participant(id="P-"+secrets.token_hex(6))
    db.add(p); db.commit()
    session["participant_id"]=p.id
    return p

def ensure_system_participant(db):
    p=db.get(Participant,SYSTEM_PARTICIPANT_ID)
    if not p:
        p=Participant(id=SYSTEM_PARTICIPANT_ID,label="system/dataset")
        db.add(p); db.commit()
    return p

def create_run(db,participant,study_key,protocol_version,metadata=None,provider=None,model_label=None,status="created"):
    run=Run(id="R-"+secrets.token_hex(8),participant_id=participant.id,study_key=study_key,
            protocol_version=protocol_version,provider=provider,model_label=model_label,status=status,
            metadata_json=json.dumps(metadata or {},ensure_ascii=False))
    db.add(run); db.flush()
    return run

def complete_run(run,metadata_update=None):
    meta=run.meta()
    if metadata_update:
        meta.update(metadata_update)
    run.set_meta(meta)
    run.status="completed"
    run.completed_at=utcnow()
