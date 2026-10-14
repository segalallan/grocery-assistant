"""Runs the real Streamlit script headlessly and clicks through the main flows."""
from datetime import timedelta
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parent.parent / "src" / "app.py")


@pytest.fixture
def env(backend, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    database = backend()
    ok, _ = database.create_user("a@b.com", "password1", "Avi", "S", household_name="Home")
    assert ok
    return database


def fresh_app(env):
    at = AppTest.from_file(APP, default_timeout=30)
    # cache_resource would otherwise keep a service bound to an earlier test's DB
    import streamlit as st
    st.cache_resource.clear()
    return at


def logged_in(env):
    at = fresh_app(env)
    user = env.authenticate("a@b.com", "password1")
    at.session_state["authenticated"] = True
    for k in ["household_id", "household_name", "household_code", "display_name", "email", "username"]:
        at.session_state[k] = user[k]
    at.session_state["user_id"] = user["id"]
    return at.run()


def test_login_page_renders(env):
    at = fresh_app(env).run()
    assert not at.exception
    assert at.button[0].label  # sign in button exists


def test_login_flow(env):
    at = fresh_app(env).run()
    at.text_input[0].input("a@b.com")
    at.text_input[1].input("password1")
    at.button[0].click().run()
    assert not at.exception
    assert at.session_state["authenticated"] is True


def test_empty_dashboard(env):
    at = logged_in(env)
    assert not at.exception, at.exception


def test_add_purchase_and_full_dashboard(env):
    from config import today
    from engine import PantryDepletionEngine
    from pantry_service import PantryService
    svc = PantryService(env, PantryDepletionEngine())
    hh = env.authenticate("a@b.com", "password1")["household_id"]
    svc.record_purchase(hh, "Milk", "חלב", "Dairy & Eggs", 1, today() - timedelta(days=30), "Avi")
    svc.record_purchase(hh, "Rice", "אורז", "Pantry Staples", 2, today() - timedelta(days=3), "Avi")
    svc.add_to_shopping_list(hh, [{"name_en": "Bread", "name_he": "לחם", "category": "Pantry Staples"}], "manual", "Avi")

    at = logged_in(env)
    assert not at.exception, at.exception
    banner = [b for b in at.button if "running out" in b.label]
    assert banner, "high-risk milk should trigger the restock banner"

    # Ran Out via bulk action
    milk_chk = next(c for c in at.checkbox if c.key and c.key.startswith("chk_"))
    milk_chk.check().run()
    ran_out = next(b for b in at.button if b.label == "❌ Ran out")
    ran_out.click().run()
    assert not at.exception, at.exception
    assert len(svc.get_pantry(hh)) == 1
    assert {s["item_name_en"] for s in svc.get_shopping_list(hh)} == {"Bread", "Milk"}

    # Buy bread from the shopping list
    bought = next(b for b in at.button if b.key and b.key.startswith("bought_"))
    bought.click().run()
    assert not at.exception, at.exception
    assert len(svc.get_shopping_list(hh)) == 1


def test_hebrew_mode_renders(env):
    at = logged_in(env)
    at.selectbox[0].select("עברית").run()
    assert not at.exception, at.exception


def test_signup_then_email_preferences(env):
    at = fresh_app(env).run()
    at.text_input(key="su_email").input("new@user.com")
    at.text_input(key="su_pw").input("password123")
    first_name = [t for t in at.text_input if t.label == "First name"][0]
    last_name = [t for t in at.text_input if t.label == "Last name"][0]
    first_name.input("New"); last_name.input("User")
    [t for t in at.text_input if t.label.startswith("New household")][0].input("New Home")
    [b for b in at.button if b.label == "Create Account"][0].click().run()
    assert not at.exception, at.exception
    assert at.session_state["onboarding_user_id"]

    at.radio(key="ob_wants").set_value("yes").run()
    at.radio(key="ob_lang").set_value("he").run()
    [b for b in at.button if b.label == "Continue"][0].click().run()
    assert not at.exception, at.exception
    assert at.session_state["authenticated"] is True
    user = env.get_user(email="new@user.com")
    assert user["email_alerts"] == 1 and user["email_lang"] == "he"


def test_signup_decline_emails(env):
    at = fresh_app(env).run()
    at.text_input(key="su_email").input("no@mail.com")
    at.text_input(key="su_pw").input("password123")
    [t for t in at.text_input if t.label == "First name"][0].input("No")
    [t for t in at.text_input if t.label == "Last name"][0].input("Mail")
    [t for t in at.text_input if t.label.startswith("New household")][0].input("Quiet Home")
    [b for b in at.button if b.label == "Create Account"][0].click().run()
    at.radio(key="ob_wants").set_value("no").run()
    assert not [r for r in at.radio if r.key == "ob_lang"]  # language only asked after "yes"
    [b for b in at.button if b.label == "Continue"][0].click().run()
    assert env.get_user(email="no@mail.com")["email_alerts"] == 0
