import json
from datetime import datetime, timezone
from sqlalchemy import String, Integer, Float, Text, DateTime, Boolean, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from db import Base, engine

def utcnow():
    return datetime.now(timezone.utc)

class GRPSession(Base):
    __tablename__ = "grp_sessions"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), unique=True, index=True)
    participant_id: Mapped[str] = mapped_column(ForeignKey("participants.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    experiment_version: Mapped[str] = mapped_column(String(80), nullable=False)
    consent_version: Mapped[str] = mapped_column(String(80), nullable=False)
    consent: Mapped[bool] = mapped_column(Boolean, nullable=False)
    age_18: Mapped[bool] = mapped_column(Boolean, nullable=False)
    language: Mapped[str] = mapped_column(String(12), nullable=False)
    country: Mapped[str | None] = mapped_column(String(80), nullable=True)
    education: Mapped[str | None] = mapped_column(String(80), nullable=True)
    plan_json: Mapped[str] = mapped_column(Text, nullable=False)
    legacy_source: Mapped[str | None] = mapped_column(String(80), nullable=True)
    trials = relationship("GRPTrial", back_populates="session", cascade="all, delete-orphan",
                          order_by="GRPTrial.global_index")
    def plan(self):
        return json.loads(self.plan_json)

class GRPTrial(Base):
    __tablename__ = "grp_trials"
    __table_args__ = (UniqueConstraint("session_id","global_index",name="uq_grp_trial_step"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("grp_sessions.id", ondelete="CASCADE"), index=True)
    block_index: Mapped[int] = mapped_column(Integer, nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    global_index: Mapped[int] = mapped_column(Integer, nullable=False)
    stimulus: Mapped[str] = mapped_column(Text, nullable=False)
    response: Mapped[str] = mapped_column(Text, nullable=False)
    attention_condition: Mapped[str] = mapped_column(String(24), nullable=False)
    is_probe: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reaction_ms: Mapped[float] = mapped_column(Float, nullable=False)
    focus_lost: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    session = relationship("GRPSession", back_populates="trials")

class LabExternalResult(Base):
    __tablename__ = "lab_external_results"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), unique=True, index=True)
    result_type: Mapped[str] = mapped_column(String(80), index=True)
    source_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    def payload(self):
        return json.loads(self.payload_json)

class ObserverStateRecord(Base):
    """Durable lightweight record for every observed closed-candle state."""
    __tablename__ = "observer_state_records"
    __table_args__ = (
        UniqueConstraint("session_id", "state_id", name="uq_observer_state_session"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(96), index=True, nullable=False, default="unknown")
    state_id: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    market_time: Mapped[str | None] = mapped_column(String(64), nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    action: Mapped[str | None] = mapped_column(String(20), nullable=True)
    regime: Mapped[str | None] = mapped_column(String(80), nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    def payload(self):
        return json.loads(self.payload_json)


class ObserverRuntimeCheckpoint(Base):
    """Compressed full runtime checkpoint used to restore Observer after restart/redeploy."""
    __tablename__ = "observer_runtime_checkpoints"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(96), index=True, nullable=False, default="unknown")
    state_id: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    runtime_gzip_b64: Mapped[str] = mapped_column(Text, nullable=False)
    status_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

def init_lab_models():
    Base.metadata.create_all(engine)
