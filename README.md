# Cognitive Command Center v0.1

A deployable research HQ for:
1. **Bot Cognitive Stress Test v0.1** — participant copies a generated prompt, pastes model JSON, validator requires 20/20 + 6/6.
2. **Human–AI State Test v1** — human base + human GRP update + AI base + AI GRP update.
3. **External benchmark packs** — compare model choices with authorized human response distributions.
4. **Admin HQ** — model aggregates, legacy JSON import, benchmark import, CSV exports.

## Why this architecture
- Participant, model run and model identity are separate entities.
- One participant can run multiple models.
- The same model can be compared across multiple accounts.
- Legacy manual runs are marked `LEGACY_MANUAL`.
- Bot Stress questions stay bank v0.1; Command Center uses recorded constrained random ordering.
- EFP4 in the Human–AI test is stored as diagnostic/ambiguous and excluded from EFP core scoring.

## Local smoke run
```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export COOKIE_SECURE=0
export ADMIN_KEY=test
python app.py
```
Open http://127.0.0.1:5000

## Render + Neon
Use `render.yaml`.
Set `DATABASE_URL` to the Neon pooled Postgres connection string.
Render generates `SECRET_KEY` and `ADMIN_KEY`; copy the generated ADMIN_KEY somewhere private.

## External benchmark pack format
```json
{
  "benchmark_id": "MY-PACK-001",
  "name": "My authorized human benchmark",
  "version": "1",
  "source_name": "Source",
  "source_url": "https://...",
  "license_note": "What you are allowed to store/use",
  "items": [
    {
      "id": "Q1",
      "text": "Question",
      "options": {"A":"...", "B":"...", "C":"..."},
      "human_distribution": {"A":0.35, "B":0.50, "C":0.15}
    }
  ]
}
```

**Do not upload raw external datasets unless their terms permit it.**
Prefer derived/authorized `human_distribution` packs where appropriate.

## Admin
Go to `/admin`, enter `ADMIN_KEY`.
- import historical complete Bot Stress JSON runs;
- import benchmark packs;
- export `runs.csv` and `answers.csv`;
- view basic model-level aggregates.

## Next engineering steps
- CSRF protection / rate limiting before public high-volume launch.
- Participant consent page if collecting anything beyond anonymous model-run metadata.
- Adapters for Moral Machine / ETHICS / Scruples and approved WVS/ESS derived packs.
- Statistical notebook: mixed-effects / hierarchical model for Model + Account + State + interactions.
