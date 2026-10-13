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

## Database

* Local: a SQLite file (`pantry_v1.db` in the project folder), no setup.
* Production: Postgres. Set the `DATABASE_URL` secret and the app uses it automatically.
  On the first start against an empty Postgres, it copies in any existing `pantry_v1.db`.
  Manual copy: `DATABASE_URL=... python src/jobs/import_sqlite.py path/to/pantry_v1.db`
* Tests run on SQLite by default. Set `TEST_DATABASE_URL` to a throwaway Postgres to run
  them there too (they drop all tables in it).

## Daily alerts

`.github/workflows/daily_alerts.yml` runs `src/jobs/send_alerts.py` every morning. It needs
three GitHub repository secrets: `DATABASE_URL`, `EMAIL_SENDER`, `EMAIL_PASSWORD`.
Run it manually from the Actions tab; the manual run defaults to a dry run.
