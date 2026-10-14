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
    milk_chk.check()  # inside the form: nothing runs until an action is pressed
    ran_out = next(b for b in at.button if b.label == "❌ Ran out")
    ran_out.click().run()
    assert not at.exception, at.exception
    assert len(svc.get_pantry(hh)) == 1
    assert {s["item_name_en"] for s in svc.get_shopping_list(hh)} == {"Bread", "Milk"}

    # Buy bread from the shopping list: tick it, then the one "Bought" button
    bread = next(c for c in at.checkbox if c.key and c.key.startswith("shop_") and "Bread" in c.label)
    bread.check()
    next(b for b in at.button if b.label == "✅ Bought").click().run()
    assert not at.exception, at.exception
    assert [s["item_name_en"] for s in svc.get_shopping_list(hh)] == ["Milk"]
    assert any(i["name_en"] == "Bread" for i in svc.get_pantry(hh))


def test_hebrew_mode_renders(env):
    at = logged_in(env)
    at.segmented_control(key="lang_switch").set_value("he").run()
    assert at.session_state["lang"] == "he"
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


def test_forgot_password_flow(env, monkeypatch):
    import notifications
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def login(self, *a): pass
        def sendmail(self, s, to, msg): sent.append(msg); return {}
        def quit(self): pass

    monkeypatch.setattr(notifications.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setenv("EMAIL_SENDER", "bot@x.com")
    monkeypatch.setenv("EMAIL_PASSWORD", "pw")

    at = fresh_app(env).run()
    at.text_input(key="reset_email_input").input("a@b.com")
    [b for b in at.button if b.label == "Send code"][0].click().run()
    assert not at.exception, at.exception
    assert len(sent) == 1

    import email, re
    m = email.message_from_string(sent[0])
    body = next(p for p in m.walk() if p.get_content_type() == "text/html").get_payload(decode=True).decode()
    code = re.search(r">(\d{6})<", body).group(1)

    at.text_input(key="reset_code_input").input(code)
    at.text_input(key="reset_pw1").input("brandnew123")
    at.text_input(key="reset_pw2").input("brandnew123")
    [b for b in at.button if b.label == "Set new password"][0].click().run()
    assert not at.exception, at.exception
    assert any("Password changed" in s.value for s in at.success)
    assert env.authenticate("a@b.com", "brandnew123")


def test_forgot_password_unknown_email_looks_the_same(env, monkeypatch):
    import notifications
    monkeypatch.setattr(notifications.smtplib, "SMTP_SSL", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no email")))
    at = fresh_app(env).run()
    at.text_input(key="reset_email_input").input("stranger@x.com")
    [b for b in at.button if b.label == "Send code"][0].click().run()
    assert not at.exception, at.exception
    assert any("If stranger@x.com has an account" in i.value for i in at.info)


def test_action_without_selection_warns(env):
    from config import today
    from engine import PantryDepletionEngine
    from pantry_service import PantryService
    svc = PantryService(env, PantryDepletionEngine())
    hh = env.authenticate("a@b.com", "password1")["household_id"]
    svc.record_purchase(hh, "Rice", "", "Pantry Staples", 1, today(), "Avi")
    at = logged_in(env)
    next(b for b in at.button if b.label == "❌ Ran out").click().run()
    assert not at.exception, at.exception
    assert any("Select at least one" in w.value for w in at.warning)
    assert len(svc.get_pantry(hh)) == 1


def test_google_button_only_when_configured(env):
    at = fresh_app(env).run()
    assert not [b for b in at.button if "Google" in b.label]
    at2 = fresh_app(env)
    at2.secrets["auth"] = {"redirect_uri": "http://localhost:8501/oauth2callback", "cookie_secret": "x",
                           "google": {"client_id": "id", "client_secret": "s",
                                      "server_metadata_url": "https://accounts.google.com/.well-known/openid-configuration"}}
    at2.run()
    assert not at2.exception, at2.exception
    assert [b for b in at2.button if b.label == "Continue with Google"]


def test_first_google_signin_joins_household(env):
    with env.connect() as c:
        code = c.execute("SELECT household_code FROM households").fetchone()[0]
    at = fresh_app(env)
    at.session_state["google_signup"] = {"email": "gal@gmail.com", "name": "Gal Cohen"}
    at.run()
    assert not at.exception, at.exception
    assert any("gal@gmail.com" in h.value for h in at.subheader)
    at.text_input[1].input(code)
    [b for b in at.button if b.label == "Create Account"][0].click().run()
    assert not at.exception, at.exception
    # then the usual email-preferences step
    [b for b in at.button if b.label == "Continue"][0].click().run()
    assert at.session_state["authenticated"] is True
    u = env.get_user(email="gal@gmail.com")
    assert u["display_name"] == "Gal Cohen"
    assert u["household_id"] == env.get_user(email="a@b.com")["household_id"]


def test_names_are_normalized_without_api_key(env):
    """No OpenAI key: a Hebrew-only item still gets both fields filled (copy fallback), never blank."""
    from text_parser import TextListParser, normalize_name_pair
    assert normalize_name_pair("חלב", "") == ("", "חלב")
    assert normalize_name_pair("Milk", "Milk") == ("Milk", "")
    assert normalize_name_pair("", "Milk") == ("Milk", "")
    assert normalize_name_pair("Milk", "חלב") == ("Milk", "חלב")
    assert TextListParser().translate_pairs([("חלב", ""), ("Eggs", "ביצים")]) == [("חלב", "חלב"), ("Eggs", "ביצים")]


def test_translation_fills_missing_names(monkeypatch):
    import json, types
    from text_parser import TextListParser
    p = TextListParser()
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    calls = []

    def fake_create(**kw):
        req = json.loads(kw["messages"][0]["content"].split("\n", 1)[1])
        calls.append(req)
        out = [{"i": r["i"], "en": r["en"] or "Milk", "he": r["he"] or "חלב"} for r in req]
        return types.SimpleNamespace(choices=[types.SimpleNamespace(
            message=types.SimpleNamespace(content=json.dumps({"items": out})))])

    p._client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=fake_create)))
    assert p.translate_pairs([("חלב", ""), ("Bread", "לחם"), ("Milk", "")]) == \
        [("Milk", "חלב"), ("Bread", "לחם"), ("Milk", "חלב")]
    assert len(calls) == 1 and len(calls[0]) == 2  # one API call, only for the incomplete items


def _svc(env):
    from engine import PantryDepletionEngine
    from pantry_service import PantryService
    return PantryService(env, PantryDepletionEngine())


def test_review_table_commits_matches_and_new_items(env):
    from config import today
    svc = _svc(env)
    hh = env.authenticate("a@b.com", "password1")["household_id"]
    milk = svc.record_purchase(hh, "3% Milk 1L", "חלב 3%", "Dairy & Eggs", 1, today() - timedelta(days=8), "Avi")
    at = logged_in(env)
    at.session_state["staged_items"] = [
        {"raw_name": "חלב טרה 3% 2 ליטר", "name_en": "3% Milk", "name_he": "חלב 3%", "category": "Dairy & Eggs",
         "units": 1, "product_id": milk.product_id, "match": "ai", "size": "2 L"},
        {"raw_name": "פסטה פנה 500 גרם", "name_en": "Penne pasta", "name_he": "פסטה פנה", "category": "Pantry Staples",
         "units": 2, "product_id": None, "match": "new", "size": "500 g"},
    ]
    at.session_state["staged_date"] = today()
    at.run()
    assert not at.exception, at.exception
    next(b for b in at.button if b.label == "✅ Save to pantry").click().run()
    assert not at.exception, at.exception
    items = {i["name_en"]: i for i in svc.get_pantry(hh)}
    assert items["3% Milk"]["eff_units"] == 2.0                    # 2 L with a 1 L reference
    assert items["Penne pasta"]["size"].amount == 500
    assert svc.get_aliases(hh)["חלב טרה 3%"] == milk.product_id     # the receipt wording is now remembered
    assert len(svc.get_products(hh)) == 2


def test_manual_add_existing_product_with_size(env):
    from config import today
    svc = _svc(env)
    hh = env.authenticate("a@b.com", "password1")["household_id"]
    milk = svc.record_purchase(hh, "Milk 1L", "", "Dairy & Eggs", 1, today() - timedelta(days=5), "Avi")
    at = logged_in(env)
    form_select = next(s for s in at.selectbox if s.label == "Product")
    form_select.select(milk.product_id)
    next(t for t in at.text_input if t.label == "Package size").input("1.5 L")
    next(b for b in at.button if b.label == "Save" and b.proto.form_id == "add_purchase").click().run()
    assert not at.exception, at.exception
    item = svc.get_pantry(hh)[0]
    assert item["product_id"] == milk.product_id and item["eff_units"] == 1.5


class _FakeSMTP:
    sent = []

    def __init__(self, *a, **k): pass
    def login(self, *a): pass
    def sendmail(self, s, to, msg): _FakeSMTP.sent.append((to, msg)); return {}
    def quit(self): pass


def _mail(monkeypatch):
    import notifications
    _FakeSMTP.sent = []
    monkeypatch.setattr(notifications.smtplib, "SMTP_SSL", _FakeSMTP)
    monkeypatch.setenv("EMAIL_SENDER", "bot@x.com")
    monkeypatch.setenv("EMAIL_PASSWORD", "pw")
    monkeypatch.setenv("ADMIN_EMAILS", "boss@x.com")


def test_feedback_is_saved_and_emailed(env, monkeypatch):
    _mail(monkeypatch)
    at = logged_in(env)
    next(t for t in at.text_area if t.placeholder.startswith("What's working")).input("Love it, but the bars are tiny")
    next(b for b in at.button if b.proto.form_id == "feedback_form").click().run()
    assert not at.exception, at.exception
    assert env.list_feedback()[0]["message"] == "Love it, but the bars are tiny"
    assert _FakeSMTP.sent and _FakeSMTP.sent[0][0] == ["boss@x.com"]


def test_crash_shows_friendly_message_logs_and_emails_once(env, monkeypatch, tmp_path):
    _mail(monkeypatch)
    crash = tmp_path / "crash_app.py"
    crash.write_text("import streamlit as st\nst.write('hi')\nraise ValueError('boom')\n")
    monkeypatch.setenv("PANTRY_APP_FILE", str(crash))
    at = fresh_app(env).run()
    assert any("Something went wrong" in e.value for e in at.error)
    assert not at.exception                                   # no raw traceback for normal users
    errs = env.list_errors()
    assert len(errs) == 1 and errs[0]["signature"].startswith("ValueError@crash_app.py")
    assert len(_FakeSMTP.sent) == 1
    at.run()                                                   # same error again within the hour
    assert env.list_errors()[0]["count"] == 2 and len(_FakeSMTP.sent) == 1


def test_rerun_and_stop_are_not_errors(env, monkeypatch, tmp_path):
    _mail(monkeypatch)
    ok = tmp_path / "stop_app.py"
    ok.write_text("import streamlit as st\nst.write('x')\nst.stop()\n")
    monkeypatch.setenv("PANTRY_APP_FILE", str(ok))
    at = fresh_app(env).run()
    assert not at.error and env.list_errors() == []


def test_intro_card_shows_until_dismissed(env):
    at = logged_in(env)
    assert any("How Smart Pantry works" in m.value for m in at.markdown)
    next(b for b in at.button if b.key == "intro_ok").click().run()
    assert not at.exception, at.exception
    assert env.get_user(email="a@b.com")["intro_dismissed"] == 1
    assert not any("How Smart Pantry works" in m.value for m in at.markdown)


def test_admin_tab_only_for_admins(env, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "someone@else.com")
    at = logged_in(env)
    assert not [t for t in at.tabs if "Admin" in t.label]
    monkeypatch.setenv("ADMIN_EMAILS", "a@b.com")
    at = logged_in(env)
    assert not at.exception, at.exception
    assert [t for t in at.tabs if "Admin" in t.label]
    assert any(m.label == "Households" for m in at.metric)


def test_delete_account_from_the_app(env):
    at = logged_in(env)
    next(t for t in at.text_input if t.key == "delete_confirm").input("wrong@x.com")
    next(b for b in at.button if b.proto.form_id == "delete_account").click().run()
    assert env.get_user(email="a@b.com") is not None            # mismatch → nothing deleted
    next(t for t in at.text_input if t.key == "delete_confirm").input("a@b.com")
    next(b for b in at.button if b.proto.form_id == "delete_account").click().run()
    assert not at.exception, at.exception
    assert env.get_user(email="a@b.com") is None
    assert not at.session_state["authenticated"] if "authenticated" in at.session_state else True
