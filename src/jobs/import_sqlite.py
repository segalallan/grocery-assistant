"""One-time copy of a SQLite pantry database into the Postgres database in DATABASE_URL.

    DATABASE_URL=postgresql://... python src/jobs/import_sqlite.py path/to/pantry_v1.db

Refuses to run if the target already has households.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import Database  # noqa: E402


def main():
    if len(sys.argv) != 2 or not os.path.exists(sys.argv[1]):
        sys.exit("Usage: python src/jobs/import_sqlite.py path/to/pantry.db")
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("Set DATABASE_URL to the Postgres connection string first.")
    target = Database(database_url=url)
    print("Copying into", target.describe())
    for table, n in target.import_from_sqlite(sys.argv[1]).items():
        print(f"  {table}: {n} rows")
    target.close()


if __name__ == "__main__":
    main()
