# Universe Lab Public UX v1.2

Purpose: make the existing Universe Lab understandable and usable by a person arriving from a public link.

No database schema changes.
No research-engine changes.
No experiment endpoint changes.

## v1.2 changes

- `/` becomes the canonical public entry.
- One navigation everywhere: Test AI / Experiments / Lab / Research.
- Admin is removed from public navigation/footer.
- Public-facing copy is consistently English.
- Primary CTA is `TEST MY AI`.
- `/test-ai` puts `START BOT TEST` above the fold.
- Existing `/experiments`, `/lab/`, `/research`, data, results and research engines are preserved.
- Mobile CTA becomes full-width for lower friction.

## Install

From `~/cognitive-command-center`:

    unzip -o ~/storage/downloads/universe_lab_public_v12.zip
    python install_public_ux_v12.py
    python -m py_compile app.py universe_lab/*.py
    git diff --check
    git status --short

Do not use `git add .`.
