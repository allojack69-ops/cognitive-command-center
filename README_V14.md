# Universe Lab v1.4 — Funnel Analytics + State Field Visual Identity

Incremental patch for the existing Universe Lab v1.3.

## Adds

### Private conversion funnel

Admin analytics now measures:

1. public entry
2. `/test-ai`
3. Bot Stress run created
4. baseline JSON accepted
5. completed Bot Stress run

It also attributes the funnel by first-touch UTM/referrer when the browser can be linked to the anonymous laboratory participant.

Historical runs remain in aggregate totals, but traffic that predates first-party analytics cannot be retroactively attributed.

### Interface visual identity

The two supplied images are included unchanged as:

- `static/universe/state_field_01.jpg`
- `static/universe/state_field_02.jpg`

They are used as the visual state-field/lattice identity on:

- public `/`
- `/research`

No image-generation or alteration is performed; presentation uses CSS crop/overlay only.

## Install

    cd ~/cognitive-command-center
    unzip -o ~/storage/downloads/universe_lab_funnel_visual_v14.zip
    python install_funnel_visual_v14.py
    python -m py_compile app.py universe_lab/*.py
    git diff --check
    git status --short
