"""Existing v1 databases must open cleanly, and the alert job must send once per item."""
import shutil
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

import notifications
import pantry_service
from db import Database
from engine import PantryDepletionEngine
from pantry_service import PantryService

TODAY = date(2026, 10, 13)
ORIGINAL = Path("/mnt/user-data/uploads/database.py")


@pytest.mark.skipif(not ORIGINAL.exists(), reason="original v1 database.py not available")
def test_v1_database_upgrades(tmp_path, monkeypatch):
    monkeypatch.setattr(pantry_service, "app_today", lambda: TODAY)
    # Build a DB with the ORIGINAL schema, using the user's original code.
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    shutil.copy(ORIGINAL, legacy_dir / "legacy_database.py")
    sys.path.insert(0, str(legacy_dir))
    try:
        from legacy_database import PantryDatabase
        path = str(tmp_path / "v1.db")
        old = PantryDatabase(path)
        assert old.create_user("old@b.com", "pw", "Old", "User", household_name="Old Home")
    finally:
        sys.path.remove(str(legacy_dir))

    conn = sqlite3.connect(path)
    hh = conn.execute("SELECT id FROM households").fetchone()[0]
    conn.execute("INSERT INTO inventory (household_id, category, item_name_en, item_name_he, units, purchase_date, user_lambda)"
                 " VALUES (?, 'Dairy & Eggs', 'Milk', 'חלב', 1, ?, 12.0)", (hh, str(TODAY - timedelta(days=5))))
    conn.execute("INSERT INTO purchase_history (household_id, category, interval_days, recorded_at, recorded_by)"
                 " VALUES (?, 'Dairy & Eggs', 6.0, '2026-09-01', 'x')", (hh,))
    conn.commit(); conn.close()

    db = Database(path)  # runs migrations
    svc = PantryService(db, PantryDepletionEngine())
    assert db.authenticate("old@b.com", "pw") is not None  # old password hashes still work
    items = svc.get_pantry(hh)
    assert items[0]["item_key"] == "milk"
    # Legacy category-level history still informs the category estimate
    assert svc.item_learning_stats(hh, "milk", "Dairy & Eggs")["category_cycles"] == 1
    svc.record_purchase(hh, "Milk", "חלב", "Dairy & Eggs", 1, TODAY, "Old")
    assert svc.item_learning_stats(hh, "milk", "Dairy & Eggs")["item_cycles"] == 1


class FakeSMTP:
    sent = []

    def __init__(self, *a, **kw): pass
    def login(self, *a): pass
    def sendmail(self, sender, to, msg):
        FakeSMTP.sent.append((to, msg)); return {}
    def quit(self): pass


def test_alerts_send_once_and_escape_html(backend, monkeypatch):
    monkeypatch.setattr(pantry_service, "app_today", lambda: TODAY)
    monkeypatch.setattr(notifications.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setenv("EMAIL_SENDER", "bot@x.com")
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    FakeSMTP.sent = []

    db = backend()
    db.create_user("a@b.com", "pw", "A", "B", household_name="Home")
    with db.connect() as c:
        code = c.execute("SELECT household_code FROM households").fetchone()[0]
        hh = c.execute("SELECT id FROM households").fetchone()[0]
    db.create_user("c@d.com", "pw", "C", "D", join_code=code)
    svc = PantryService(db, PantryDepletionEngine())
    svc.record_purchase(hh, "<script>Milk</script>", "", "Dairy & Eggs", 1, TODAY - timedelta(days=40), "A")
    svc.record_purchase(hh, "Rice", "", "Pantry Staples", 1, TODAY - timedelta(days=1), "A")

    s1 = notifications.process_all_daily_alerts(svc)
    assert s1["emails_sent"] == 2 and s1["items_alerted"] == 1  # both members, only the milk
    assert "<script>" not in FakeSMTP.sent[0][1]

    s2 = notifications.process_all_daily_alerts(svc)
    assert s2["emails_sent"] == 0  # no repeat nag the next day

    item = svc.get_pantry(hh)[0] if svc.get_pantry(hh)[0]["name_en"].startswith("<") else svc.get_pantry(hh)[1]
    svc.mark_still_going(hh, [item["id"]], TODAY)
    assert svc.get_pantry(hh)  # still there; stamp cleared, will alert again once risk climbs


@pytest.mark.skipif(not ORIGINAL.exists(), reason="original v1 database.py not available")
def test_import_v1_sqlite_into_current_backend(backend, tmp_path, monkeypatch):
    """The first-start copy: an old v1 SQLite file → the new database, ids and logins preserved."""
    monkeypatch.setattr(pantry_service, "app_today", lambda: TODAY)
    legacy_dir = tmp_path / "legacy2"
    legacy_dir.mkdir()
    shutil.copy(ORIGINAL, legacy_dir / "legacy_database2.py")
    sys.path.insert(0, str(legacy_dir))
    try:
        from legacy_database2 import PantryDatabase
        old_path = str(tmp_path / "old_v1.db")
        PantryDatabase(old_path).create_user("old@b.com", "pw", "Old", "User", household_name="Old Home")
    finally:
        sys.path.remove(str(legacy_dir))
    conn = sqlite3.connect(old_path)
    hh = conn.execute("SELECT id FROM households").fetchone()[0]
    conn.execute("INSERT INTO inventory (household_id, category, item_name_en, item_name_he, units, purchase_date, user_lambda)"
                 " VALUES (?, 'Dairy & Eggs', 'Milk', 'חלב', 1, ?, 12.0)", (hh, str(TODAY - timedelta(days=5))))
    conn.commit(); conn.close()

    target = backend()
    if backend.name == "sqlite":  # target must be a different, empty file
        target = Database(db_path=str(tmp_path / "fresh_target.db"))
    counts = target.import_from_sqlite(old_path)
    assert counts["users"] == 1 and counts["inventory"] == 1 and counts["household_categories"] == 8
    assert target.authenticate("old@b.com", "pw")["household_name"] == "Old Home"

    svc = PantryService(target, PantryDepletionEngine())
    assert svc.get_pantry(hh)[0]["item_key"] == "milk"
    ok, _ = target.create_user("new@b.com", "pw", "N", "U", household_name="New")  # ids don't collide
    assert ok
    with pytest.raises(RuntimeError):
        target.import_from_sqlite(old_path)  # never imports over existing data
