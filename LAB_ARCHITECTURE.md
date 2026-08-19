# Universe Laboratory v1 — architecture map

Built from the uploaded four-project archive.

## Canonical shell: Cognitive Command Center

Keep Flask + Gunicorn + SQLAlchemy/Postgres as the main web/control plane.

Existing canonical backbone:
- participants
- runs
- answers
- events
- benchmark_packs

Existing studies stay intact:
- BOT_STRESS_V01
- HUMAN_AI_STATE_V1
- BENCHMARK:* packs
- HISTORY_INTERVENTION_V01

Existing analysis stays intact:
model comparison, Human↔AI alignment, objective checks, boundary stability,
presentation/form geometry, history metrics.

## Cognitive Dynamics / GRP

Original service is FastAPI with isolated participant_sessions/trials.
Frozen protocol is preserved operationally as CD-GRP-0.5.2-RESET-WASHOUT:
10 baseline → 4 intervention/sham → reset → 6 early washout → reset → 12 late washout.

In the unified lab:
- grp_sessions
- grp_trials
- every GRP session also creates a canonical Run with study_key=CD_GRP_V052
- new GRP sessions use the same anonymous Participant as the other studies

Legacy GRP data can be imported from its two CSV exports. Because the old app
did not preserve a cross-session participant identity, old sessions are not
falsely merged.

## Observer WAPI v0.7

Keep numpy/scipy/scikit-learn outside the public web runtime.
The offline engine produces metrics.json; the lab stores the compact result as
DATASET:OBSERVER_WAPI_V07. This avoids bloating Render and preserves the engine.

## MOR Observer

The market/state observer contains state, phase-front, corridor, geometry,
transition outcome/edge matrices, predictions and trackers. It stays a separate
runtime. Compact/latest JSON snapshots enter the unified catalog as
DATASET:MOR_OBSERVER. Full raw history stays in observer storage.

## MOR Mesh v0.7

Android remains the network runtime. The lab records real two-node field tests
as canonical MOR_MESH_FIELD_V07 runs:
HELLO A, HELLO B, encrypted delivery, signed ACK, queue garbage collection.

## Unified ontology

Participant
  → Run
      → study/protocol
          → Answers (Bot/Human–AI/History)
          → GRPSession → GRPTrial
          → MOR Mesh field metadata
          → Dataset result

Then:
Metrics → comparison → geometry/history → anomaly → next experiment

The result is one scientific history without forcing incompatible runtimes into
one monolithic process.
