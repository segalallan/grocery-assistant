# Smart Pantry Assistant

Learns how fast your household uses each item and tells you when it's probably running out.

## Run it

```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # add your OpenAI key
streamlit run src/app.py
```

Tests: `pip install pytest && pytest -q`

## Layout

Code lives in `src/`; priors in `data/`. Run commands from the project folder.

| File | Role |
|---|---|
| `app.py` | Streamlit UI only. No SQL. |
| `pantry_service.py` | All business logic: purchases, "ran out", "still have it", shopping list. |
| `engine.py` | Weibull depletion math. Pure functions, no I/O. |
| `db.py` | Connections, schema + automatic migrations, accounts, categories. |
| `text_parser.py` | Quick Add: pasted list → items (OpenAI). |
| `notifications.py`, `src/jobs/send_alerts.py` | Daily email alerts, run headless on a schedule. |
| `config.py` | Paths, threshold, timezone, secrets lookup. |
| `i18n.py` | English / Hebrew strings. |
| `data/category_priors.json` | Typical per-unit lifetime (median days) and shape per category. |
| `notebooks/` | Offline survival-analysis scripts (not deployed). |
| `src/future/receipt_ocr.py` | Parked receipt OCR for v2 (not imported). |

## How learning works

Each stock row is one purchase of an item. A consumption cycle closes when you
**rebuy** it or mark it **ran out**; the per-unit duration is saved to `purchase_history`.
The item's expected lifetime is shrunk *item history → your household's category history →
population prior*, so a new item starts sensible and quickly becomes yours.

**Still have it** never moves the purchase date. It stores `confirmed_until`, and the risk
becomes conditional: P(gone by today | was there on the confirmed date).

## Daily alerts

Streamlit can't be triggered by a plain HTTP request (the script only runs for a browser
session), so alerts run as a separate job:

```bash
EMAIL_SENDER=... EMAIL_PASSWORD=... PANTRY_DB_PATH=/path/to/pantry_v1.db python src/jobs/send_alerts.py
```

The job must reach the same database as the app. With SQLite that means running it on the same
machine. On Streamlit Community Cloud, where the disk is wiped on restart, move to a hosted
Postgres first; then a GitHub Actions cron can run the job.
