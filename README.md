# Universe Laboratory v1

Target:

    ~/cognitive-command-center

Built against the exact source archive uploaded on 2026-08-20.

## Install

    cd ~/cognitive-command-center
    unzip -o ~/storage/downloads/universe_lab_upgrade_v1.zip
    python install_universe_lab.py

Checks:

    python -m py_compile app.py universe_lab/*.py
    git diff --check
    git status --short

Then:

    git add app.py templates static universe_lab LAB_ARCHITECTURE.md tools/import_legacy_grp_csv.py
    git commit -m "feat: unify research projects in Universe Laboratory"
    git push origin main

## New URLs

    /
    /lab/
    /lab/data
    /grp/
    /grp/recruitment
    /observer/wapi
    /observer/mor
    /mesh-lab/

All existing Command Center routes remain.

## Import old Cognitive Dynamics data later

Keep the old service alive until export is done.
From its admin page export:
- Sessions CSV
- Trials CSV

Then:

    cd ~/cognitive-command-center
    python tools/import_legacy_grp_csv.py \
      --sessions ~/storage/downloads/cognitive_sessions_v052.csv \
      --trials ~/storage/downloads/cognitive_trials_v052.csv

The importer is idempotent by old session ID.
