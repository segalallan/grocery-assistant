"""Remember-me sessions and Google accounts — SQLite and (with TEST_DATABASE_URL) Postgres."""
from datetime import datetime, timedelta, timezone

import pytest

from db import SESSION_DAYS

T0 = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(backend):
    d = backend()
    d.create_user("a@b.com", "password1", "A", "B", household_name="Home")
    return d


def test_session_roundtrip_and_expiry(db):
    uid = db.get_user(email="a@b.com")["id"]
    token = db.create_session(uid, now=T0)
    assert db.user_for_session(token, now=T0 + timedelta(days=1))["id"] == uid
    assert db.user_for_session(token, now=T0 + timedelta(days=SESSION_DAYS, seconds=1)) is None
    assert db.user_for_session("not-a-real-token") is None
    assert db.user_for_session(None) is None


def test_token_not_stored_in_plain_text(db):
    token = db.create_session(db.get_user(email="a@b.com")["id"])
    with db.connect() as c:
        stored = c.execute("SELECT token_hash FROM login_sessions").fetchone()[0]
    assert token != stored and token not in stored


def test_logout_and_logout_everywhere(db):
    uid = db.get_user(email="a@b.com")["id"]
    phone, laptop, tablet = (db.create_session(uid) for _ in range(3))
    db.end_session(phone)
    assert db.user_for_session(phone) is None and db.user_for_session(laptop)
    db.end_all_sessions(uid, keep_token=laptop)
    assert db.user_for_session(laptop) and db.user_for_session(tablet) is None


def test_password_reset_logs_out_all_devices(db):
    uid = db.get_user(email="a@b.com")["id"]
    token = db.create_session(uid)
    code = db.create_reset_code("a@b.com")["code"]
    assert db.reset_password_with_code("a@b.com", code, "newpassword1")[0]
    assert db.user_for_session(token) is None


def test_google_account_creation(db):
    with db.connect() as c:
        code = c.execute("SELECT household_code FROM households").fetchone()[0]
    ok, _ = db.create_oauth_user("g@gmail.com", "Gal Cohen", join_code=code)
    assert ok
    u = db.get_user(email="g@gmail.com")
    assert u["display_name"] == "Gal Cohen" and u["household_id"] == db.get_user(email="a@b.com")["household_id"]
    # A Google user can still set a password later through "Forgot password"
    reset = db.create_reset_code("g@gmail.com")
    assert db.reset_password_with_code("g@gmail.com", reset["code"], "mypassword1")[0]
    assert db.authenticate("g@gmail.com", "mypassword1")
