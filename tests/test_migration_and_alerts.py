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


def _decode(raw_msg: str) -> str:
    """Return subject + HTML body of a sent message as readable text."""
    import email
    from email.header import decode_header, make_header
    m = email.message_from_string(raw_msg)
    subject = str(make_header(decode_header(m["Subject"])))
    body = "".join(p.get_payload(decode=True).decode("utf-8") for p in m.walk() if p.get_content_type() == "text/html")
    return subject + "\n" + body


def _alert_setup(backend, monkeypatch):
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
    svc.record_purchase(hh, "<script>Milk</script>", "חלב", "Dairy & Eggs", 1, TODAY - timedelta(days=40), "A")
    svc.record_purchase(hh, "Rice", "אורז", "Pantry Staples", 1, TODAY - timedelta(days=1), "A")
    return db, svc, hh


def test_alerts_repeat_daily_and_escape_html(backend, monkeypatch):
    db, svc, hh = _alert_setup(backend, monkeypatch)
    s1 = notifications.process_all_daily_alerts(svc)
    assert s1["emails_sent"] == 2 and s1["items_alerted"] == 1  # both members, only the milk
    assert "<script>" not in FakeSMTP.sent[0][1]

    s2 = notifications.process_all_daily_alerts(svc)
    assert s2["emails_sent"] == 2  # still high-risk and untouched → emailed again

    milk = next(i for i in svc.get_pantry(hh) if i["name_he"] == "חלב")
    svc.add_to_shopping_list(hh, [milk], "manual", "A")
    s3 = notifications.process_all_daily_alerts(svc)
    assert s3["emails_sent"] == 0  # on the shopping list → no more emails


def test_alert_preferences_language_and_opt_out(backend, monkeypatch):
    db, svc, hh = _alert_setup(backend, monkeypatch)
    a = db.get_user(email="a@b.com")
    c = db.get_user(email="c@d.com")
    assert a["email_alerts"] == 1 and a["email_lang"] == "en"  # defaults
    db.set_email_prefs(a["id"], True, "he")
    db.set_email_prefs(c["id"], False, "en")
    assert db.get_email_prefs(c["id"]) == {"enabled": False, "lang": "en"}

    s = notifications.process_all_daily_alerts(svc)
    assert s["emails_sent"] == 1  # opted-out member gets nothing
    to, raw = FakeSMTP.sent[0]
    text = _decode(raw)
    assert to == ["a@b.com"]
    assert "התראת מזווה" in text and 'dir="rtl"' in text
    assert "חלב" in text and "Milk" not in text  # Hebrew item name, not the English one


def test_english_email_uses_english_names(backend, monkeypatch):
    db, svc, hh = _alert_setup(backend, monkeypatch)
    db.set_email_prefs(db.get_user(email="c@d.com")["id"], False, "en")
    notifications.process_all_daily_alerts(svc)
    text = _decode(FakeSMTP.sent[0][1])
    assert "Pantry alert" in text and "&lt;script&gt;Milk" in text and "חלב" not in text


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
