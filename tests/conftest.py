import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# Set TEST_DATABASE_URL to a throwaway Postgres to also run every DB test against Postgres.
# WARNING: tests DROP all app tables in that database.
PG_URL = os.environ.get("TEST_DATABASE_URL")


def reset_postgres(url):
    import psycopg
    from db import TABLES
    with psycopg.connect(url, autocommit=True) as c:
        for t in reversed(TABLES):
            c.execute(f"DROP TABLE IF EXISTS {t} CASCADE")


@pytest.fixture(params=["sqlite", "postgres"])
def backend(request, tmp_path, monkeypatch):
    """Yields a factory that returns a fresh Database on the requested backend."""
    from db import Database
    made = []

    if request.param == "postgres":
        if not PG_URL:
            pytest.skip("TEST_DATABASE_URL not set")
        reset_postgres(PG_URL)
        monkeypatch.setenv("DATABASE_URL", PG_URL)

        def factory():
            d = Database(database_url=PG_URL)
            made.append(d)
            return d
    else:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        path = str(tmp_path / "test.db")
        monkeypatch.setenv("PANTRY_DB_PATH", path)

        def factory():
            return Database(db_path=path)

    factory.name = request.param
    yield factory
    for d in made:
        d.close()
