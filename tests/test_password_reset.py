"""Password reset by emailed code — runs on SQLite and (with TEST_DATABASE_URL) Postgres."""
from datetime import datetime, timedelta, timezone

import pytest

import notifications
from db import RESET_CODE_MINUTES, RESET_MAX_ATTEMPTS

T0 = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(backend):
    d = backend()
    d.create_user("a@b.com", "oldpassword", "A", "B", household_name="Home")
    return d


def test_happy_path(db):
    r = db.create_reset_code("A@B.com ", now=T0)
    assert r["status"] == "ok" and len(r["code"]) == 6 and r["code"].isdigit()
    ok, reason = db.reset_password_with_code("a@b.com", r["code"], "newpassword1", now=T0 + timedelta(minutes=5))
    assert ok and reason == "ok"
    assert db.authenticate("a@b.com", "newpassword1")
    assert db.authenticate("a@b.com", "oldpassword") is None
    # the code is single-use
    assert db.reset_password_with_code("a@b.com", r["code"], "another123", now=T0)[0] is False


def test_unknown_email_reveals_nothing_and_sends_nothing(db):
    assert db.create_reset_code("nobody@x.com", now=T0) == {"status": "unknown"}
    assert db.reset_password_with_code("nobody@x.com", "123456", "whatever1") == (False, "invalid")


def test_wrong_code_then_lockout(db):
    r = db.create_reset_code("a@b.com", now=T0)
    wrong = "000000" if r["code"] != "000000" else "111111"
    for i in range(RESET_MAX_ATTEMPTS - 1):
        assert db.reset_password_with_code("a@b.com", wrong, "newpassword1", now=T0) == (False, "invalid")
    assert db.reset_password_with_code("a@b.com", wrong, "newpassword1", now=T0) == (False, "locked")
    # even the right code no longer works once locked
    assert db.reset_password_with_code("a@b.com", r["code"], "newpassword1", now=T0) == (False, "locked")
    assert db.authenticate("a@b.com", "oldpassword")


def test_expiry(db):
    r = db.create_reset_code("a@b.com", now=T0)
    late = T0 + timedelta(minutes=RESET_CODE_MINUTES, seconds=1)
    assert db.reset_password_with_code("a@b.com", r["code"], "newpassword1", now=late) == (False, "expired")
    assert db.authenticate("a@b.com", "oldpassword")


def test_resend_cooldown_and_new_code_replaces_old(db):
    first = db.create_reset_code("a@b.com", now=T0)
    assert db.create_reset_code("a@b.com", now=T0 + timedelta(seconds=10)) == {"status": "cooldown"}
    second = db.create_reset_code("a@b.com", now=T0 + timedelta(seconds=61))
    assert second["status"] == "ok"
    if first["code"] != second["code"]:
        assert db.reset_password_with_code("a@b.com", first["code"], "x" * 8, now=T0 + timedelta(minutes=2))[0] is False
    assert db.reset_password_with_code("a@b.com", second["code"], "newpassword1", now=T0 + timedelta(minutes=2))[0]


def test_code_is_not_stored_in_plain_text(db):
    r = db.create_reset_code("a@b.com", now=T0)
    with db.connect() as c:
        stored = c.execute("SELECT code_hash FROM password_resets").fetchone()[0]
    assert r["code"] not in stored


def test_reset_email_language(monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def login(self, *a): pass
        def sendmail(self, s, to, msg): sent.append(msg); return {}
        def quit(self): pass

    monkeypatch.setattr(notifications.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setenv("EMAIL_SENDER", "bot@x.com")
    monkeypatch.setenv("EMAIL_PASSWORD", "pw")
    notifications.send_reset_email("a@b.com", "123456", "he")
    import email
    from email.header import decode_header, make_header
    m = email.message_from_string(sent[0])
    assert "איפוס" in str(make_header(decode_header(m["Subject"])))
    body = next(p for p in m.walk() if p.get_content_type() == "text/html").get_payload(decode=True).decode()
    assert "123456" in body and 'dir="rtl"' in body
