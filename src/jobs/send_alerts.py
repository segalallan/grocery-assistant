"""Daily alert job. Run on a schedule (cron, GitHub Actions) — NOT through the Streamlit URL.

    python jobs/send_alerts.py            # send
    python jobs/send_alerts.py --dry-run  # just print who would be alerted

Needs EMAIL_SENDER and EMAIL_PASSWORD as environment variables, and access to the
same database the app uses (PANTRY_DB_PATH).
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
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    service = PantryService(Database(), PantryDepletionEngine())
    summary = process_all_daily_alerts(service, dry_run=args.dry_run)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
