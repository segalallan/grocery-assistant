"""Leave household, delete account, admin stats, feedback & error log — both databases."""
from datetime import date, datetime, timedelta, timezone

import pytest

import pantry_service
from engine import PantryDepletionEngine
from pantry_service import PantryService

TODAY = date(2026, 10, 14)


@pytest.fixture
def setup(backend, monkeypatch):
    monkeypatch.setattr(pantry_service, "app_today", lambda: TODAY)
    db = backend()
    db.create_user("a@b.com", "pw", "A", "B", household_name="Home")
    with db.connect() as c:
        code = c.execute("SELECT household_code FROM households").fetchone()[0]
    db.create_user("c@d.com", "pw", "C", "D", join_code=code)
    svc = PantryService(db, PantryDepletionEngine())
    hh = db.get_user(email="a@b.com")["household_id"]
    svc.record_purchase(hh, "Milk", "", "Dairy & Eggs", 1, TODAY - timedelta(days=3), "A")
    return db, svc, hh


def count(db, table):
    with db.connect() as c:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_leave_household_keeps_data_for_others(setup):
    db, svc, hh = setup
    c = db.get_user(email="c@d.com")
    new_hh = db.leave_household(c["id"], "My flat")
    assert new_hh != hh and db.get_user(email="c@d.com")["household_name"] == "My flat"
    assert len(svc.get_pantry(hh)) == 1                       # A still has the milk
    assert svc.get_pantry(new_hh) == []
    assert len(db.get_household_categories(new_hh)) == 8      # new household gets default categories


def test_delete_member_keeps_household(setup):
    db, svc, hh = setup
    c = db.get_user(email="c@d.com")
    db.create_session(c["id"])
    db.delete_user(c["id"])
    assert db.get_user(email="c@d.com") is None and db.authenticate("c@d.com", "pw") is None
    assert len(svc.get_pantry(hh)) == 1
    assert count(db, "login_sessions") == 0


def test_delete_last_member_wipes_household(setup):
    db, svc, hh = setup
    db.delete_user(db.get_user(email="c@d.com")["id"])
    db.delete_user(db.get_user(email="a@b.com")["id"])
    for t in ("households", "inventory", "purchases", "products", "product_aliases", "household_categories"):
        assert count(db, t) == 0, t


def test_admin_stats(setup):
    db, svc, hh = setup
    row = [r for r in db.admin_stats(TODAY.isoformat()) if r["id"] == hh][0]
    assert row["members"] == 2 and row["buys_7d"] == 1 and row["products"] == 1 and row["in_pantry"] == 1


def test_error_log_dedupes(setup):
    db, _, _ = setup
    t0 = datetime(2026, 10, 14, 9, tzinfo=timezone.utc)
    assert db.log_error("ValueError@x.py:1", "boom", "tb", "a@b.com", now=t0) is True
    assert db.log_error("ValueError@x.py:1", "boom", "tb", "a@b.com", now=t0 + timedelta(minutes=5)) is False
    assert db.log_error("ValueError@x.py:1", "boom", "tb", "a@b.com", now=t0 + timedelta(minutes=70)) is True
    assert db.list_errors()[0]["count"] == 3


def test_feedback_survives_account_deletion_anonymously(setup):
    db, _, hh = setup
    a = db.get_user(email="a@b.com")
    db.add_feedback(a["id"], hh, "a@b.com", "great app")
    db.delete_user(a["id"])
    fb = db.list_feedback()[0]
    assert fb["message"] == "great app" and fb["email"] is None
