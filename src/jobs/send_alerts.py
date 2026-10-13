"""Daily alert job. Run on a schedule (cron, GitHub Actions) — NOT through the Streamlit URL.

    python jobs/send_alerts.py            # send
    python jobs/send_alerts.py --dry-run  # just print who would be alerted

Needs EMAIL_SENDER, EMAIL_PASSWORD and DATABASE_URL (the same database the app uses)
as environment variables. In production this runs from .github/workflows/daily_alerts.yml.
"""
import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import Database  # noqa: E402
from engine import PantryDepletionEngine  # noqa: E402
from notifications import process_all_daily_alerts  # noqa: E402
from pantry_service import PantryService  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--require-postgres", action="store_true",
                        help="fail instead of silently using an empty local SQLite file")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    db = Database()
    print("Database:", db.describe())
    if args.require_postgres and not db.is_postgres:
        print("ERROR: DATABASE_URL is not set, refusing to run against local SQLite.")
        return 2
    service = PantryService(db, PantryDepletionEngine())
    try:
        summary = process_all_daily_alerts(service, dry_run=args.dry_run)
    finally:
        db.close()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
