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
| `text_parser.py` | Quick Add: pasted list → items; naming receipt lines (OpenAI). |
| `receipt_reader.py` | Receipt photo/PDF → purchase lines: vision transcription + checks against the printed total. |
| `notifications.py`, `src/jobs/send_alerts.py` | Daily email alerts, run headless on a schedule. |
| `src/normalize.py`, `src/matcher.py` | Item names → products: size parsing, alias/fuzzy/AI matching. |
| `config.py` | Paths, threshold, timezone, secrets lookup. |
| `i18n.py` | English / Hebrew strings. |
| `data/category_priors.json` | Typical per-unit lifetime (median days) and shape per category. |
| `notebooks/` | Offline analysis: model backtest (`evaluate_model`), receipt accuracy (`eval_receipts.py`). Not deployed. |

## How learning works

Each stock row is one purchase of an item. A consumption cycle closes when you
**rebuy** it or mark it **ran out**; the per-unit duration is saved to `purchase_history`.
The item's expected lifetime is shrunk *item history → your household's category history →
population prior*, so a new item starts sensible and quickly becomes yours.

**Still have it** never moves the purchase date. It stores `confirmed_until`, and the risk
becomes conditional: P(gone by today | was there on the confirmed date).

## Receipts

📷 **Scan receipt** accepts a phone photo (JPG/PNG/HEIC) or a digital receipt PDF.

1. **Transcribe** (`OPENAI_VISION_MODEL`, default `gpt-4.1-mini`, falls back to `gpt-4o-mini`): every printed
   row is copied as-is (code, text, qty or kg, line price, kind). Long receipts are sliced through blank
   gaps between rows so text stays large; PDF pages are rendered as images (their text layer is unreliable).
2. **Assemble** (plain Python): deal notes dropped, deposits kept out of the pantry, credits netted into
   their item, repeated lines merged, weights become package sizes.
3. **Check**: line prices must add up to the printed total, and units (weighed item = 1, deposits
   included) must equal the printed item count. If not, the model gets a second look told exactly
   how far off it was; anything still off is shown as a warning above the review table.
4. Lines are named and categorized one-to-one, matched to products (item **codes** are remembered
   as aliases, so a barcode matches instantly next time), and confirmed by the user as usual.

Accuracy on real receipts: put receipts and hand transcriptions (`<name>.truth.json`) in
`data/receipts/` (gitignored) and run `python notebooks/eval_receipts.py` with `OPENAI_API_KEY` set.

## Admin test mode

Admins (`ADMIN_EMAILS`) get a **🧪 Test mode** switch under their name. While it's on, everything
(purchases, receipt scans, edits, deletions) goes to a private sandbox household (`households.is_test = 1`,
linked via `users.sandbox_household_id`) instead of the real pantry. The sandbox:

* is never emailed (alerts follow `users.household_id`, the real household), can't be joined by code,
  and is left out of admin statistics and scanner reliability;
* starts empty and can be wiped with **🧹 Reset sandbox**; it's deleted along with the account.

Rule for future code: anything computed across households (pooled priors, population stats) must use
`db.real_household_ids()` or filter `COALESCE(is_test, 0) = 0`.

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
