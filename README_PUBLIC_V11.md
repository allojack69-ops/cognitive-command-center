# Universe Lab Public Layer v1.1

This patch goes on top of the already deployed Universe Lab v1 in `~/cognitive-command-center`.

It does **not** replace the database, old runs, MOR pages, benchmarks, history experiments, GRP, Observer or Mesh.

It creates three product layers:

- `/` — public entry
- `/test-ai` — simple BYO-AI testing flow
- `/experiments` — experiment library
- `/lab/` — operations / Command Center
- `/research` — MOR / Observer / Mesh / geometry research layer

## Install

```bash
cd ~/cognitive-command-center
unzip -o ~/storage/downloads/universe_lab_public_v11.zip
python install_public_layer_v11.py
python -m py_compile app.py universe_lab/*.py
git diff --check
git status --short
```

Do not use `git add .`; stage only production files after review.
