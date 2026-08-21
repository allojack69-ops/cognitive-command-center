
import os, json
from datetime import datetime, timezone
from sqlalchemy import create_engine, String, Integer, Float, Text, DateTime, ForeignKey, UniqueConstraint, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

def utcnow():
    return datetime.now(timezone.utc)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///command_center.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://") and "+psycopg" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

engine_kwargs = {"pool_pre_ping": True}

if DATABASE_URL.startswith("postgresql+psycopg://"):
    engine_kwargs.update({
        "connect_args": {"connect_timeout": 5},
        "pool_timeout": 5,
        "pool_recycle": 300,
    })

engine = create_engine(DATABASE_URL, **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

class Participant(Base):
    __tablename__ = "participants"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    label: Mapped[str|None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    runs = relationship("Run", back_populates="participant")

class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    participant_id: Mapped[str] = mapped_column(ForeignKey("participants.id"))
    study_key: Mapped[str] = mapped_column(String(80), index=True)
    protocol_version: Mapped[str] = mapped_column(String(40), default="CMD1")
    provider: Mapped[str|None] = mapped_column(String(80), nullable=True)
    model_label: Mapped[str|None] = mapped_column(String(160), nullable=True)
    account_alias: Mapped[str|None] = mapped_column(String(120), nullable=True)
    personalization: Mapped[str|None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="created", index=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime|None] = mapped_column(DateTime(timezone=True), nullable=True)
    participant = relationship("Participant", back_populates="runs")
    answers = relationship("Answer", back_populates="run", cascade="all, delete-orphan")

    def meta(self):
        try:
            return json.loads(self.metadata_json or "{}")
        except Exception:
            return {}

    def set_meta(self, obj):
        self.metadata_json = json.dumps(obj, ensure_ascii=False)

class Answer(Base):
    __tablename__ = "answers"
    __table_args__ = (UniqueConstraint("run_id","actor","item_id","phase", name="uq_answer"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    actor: Mapped[str] = mapped_column(String(20), default="ai")
    item_id: Mapped[str] = mapped_column(String(80), index=True)
    phase: Mapped[str] = mapped_column(String(20), default="base")
    choice: Mapped[str|None] = mapped_column(String(8), nullable=True)
    confidence: Mapped[float|None] = mapped_column(Float, nullable=True)
    reason: Mapped[str|None] = mapped_column(Text, nullable=True)
    key_assumption: Mapped[str|None] = mapped_column(Text, nullable=True)
    counterargument: Mapped[str|None] = mapped_column(Text, nullable=True)
    change_condition: Mapped[str|None] = mapped_column(Text, nullable=True)
    frame_status: Mapped[str|None] = mapped_column(String(32), nullable=True)
    assumption_broken: Mapped[int|None] = mapped_column(Integer, nullable=True)
    previous_reason_still_valid: Mapped[int|None] = mapped_column(Integer, nullable=True)
    raw_json: Mapped[str|None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    run = relationship("Run", back_populates="answers")

class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str|None] = mapped_column(String(40), nullable=True, index=True)
    participant_id: Mapped[str|None] = mapped_column(String(40), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class BenchmarkPack(Base):
    __tablename__ = "benchmark_packs"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    version: Mapped[str] = mapped_column(String(40), default="1")
    source_name: Mapped[str|None] = mapped_column(String(200), nullable=True)
    source_url: Mapped[str|None] = mapped_column(Text, nullable=True)
    license_note: Mapped[str|None] = mapped_column(Text, nullable=True)
    pack_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def pack(self):
        return json.loads(self.pack_json)

def init_db():
    Base.metadata.create_all(engine)

def log_event(db, event_type, run_id=None, participant_id=None, payload=None):
    db.add(Event(
        run_id=run_id,
        participant_id=participant_id,
        event_type=event_type,
        payload_json=json.dumps(payload or {}, ensure_ascii=False)
    ))
