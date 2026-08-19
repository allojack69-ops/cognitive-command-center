# Universe Lab v1.5 — Acrylic State-Field Background

This patch turns the matrix/state-field visual into the **global visual layer**
of the site instead of showing it as a standalone image card.

## Changes

- installs one full-page background asset:
  - `static/universe/universe_bg_acrylic.png`
- global fixed state-field background on all pages
- dark acrylic veil for readability
- glass/acrylic treatment for cards and panels
- mobile-specific lower background intensity
- removes the standalone State Field image block from `/`
- removes the standalone State Field image block from `/research`
- no database changes
- no route changes
- no experiment logic changes

## Install

```bash
cd ~/cognitive-command-center

unzip -o ~/storage/downloads/universe_lab_acrylic_bg_v15.zip

python install_acrylic_bg_v15.py

python -m py_compile app.py universe_lab/*.py

git diff --check

git status --short
```
