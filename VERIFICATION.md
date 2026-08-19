# Verification

The upgrade was checked against a clean copy of the uploaded `cognitive-command-center`.

Passed:
- installer safety anchor found and applied
- automatic backup created
- Python syntax compile for `app.py` and all `universe_lab/*.py`
- Jinja syntax parse for existing and new templates
- Universe Lab blueprint registration marker
- dataset/system participant exclusion patch
- blueprint template loader configuration
- no existing source route/table is deleted by the installer

Not executed in the sandbox:
- full Flask runtime smoke test, because Flask is not installed in the artifact sandbox
- live Postgres migration
- Render deploy

Those are the first post-install checks on the target repo.
