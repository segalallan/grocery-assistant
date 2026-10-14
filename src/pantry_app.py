"""Smart Pantry – Streamlit UI.

This file only renders and routes. All data changes go through PantryService,
so every way of adding or using up an item feeds the learning model.
"""
import logging
import os
import re
from datetime import timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import DB_PATH, RESTOCK_THRESHOLD, today as app_today
from db import RESET_CODE_MINUTES
from db import Database
from engine import PantryDepletionEngine
from i18n import make_translator
from matcher import ProductMatcher
from normalize import Size, extract_size, parse_size_text
from notifications import send_reset_email
from pantry_service import PantryService
from text_parser import TextListParser
from ui_theme import apply_theme, cart_spinner_html

try:
    from streamlit_sortables import sort_items
    HAS_SORTABLES = True
except ImportError:
    HAS_SORTABLES = False

st.set_page_config(page_title="Smart Pantry Assistant", page_icon="🛒", layout="wide",
                   initial_sidebar_state="collapsed")
log = logging.getLogger("smart_pantry")
apply_theme()

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ==========================================================================
# Shared resources (created once per server process, not on every click)
# ==========================================================================
@st.cache_resource
def get_service() -> PantryService:
    db = Database()  # Postgres if the DATABASE_URL secret is set, else local SQLite
    # First start on Postgres: carry over the household data from the old SQLite file, if it's still here.
    if db.is_postgres and os.path.exists(DB_PATH) and db.is_empty():
        try:
            counts = db.import_from_sqlite(DB_PATH)
            log.warning("Imported existing SQLite data into Postgres: %s", counts)
        except Exception:
            log.exception("SQLite -> Postgres import failed; starting with an empty database")
    return PantryService(db, PantryDepletionEngine())


@st.cache_resource
def get_parser() -> TextListParser:
    return TextListParser()


svc = get_service()
db = svc.db
engine = svc.engine


@st.cache_data(show_spinner=False, max_entries=2000)
def _translate_cached(name_en: str, name_he: str) -> tuple[str, str]:
    return get_parser().translate_pairs([(name_en, name_he)])[0]


def fill_names(name_en: str, name_he: str) -> tuple[str, str]:
    """Return (English, Hebrew), translating whichever one the user left out."""
    return _translate_cached((name_en or "").strip(), (name_he or "").strip())


def parser_ready() -> bool:
    return get_parser().available


def fix_missing_names() -> int:
    """Translate names on existing pantry and shopping-list items that only have one language."""
    from text_parser import normalize_name_pair
    pantry = svc.get_pantry(household_id)
    shop = svc.get_shopping_list(household_id)
    targets = []
    for it in pantry:
        en, he = normalize_name_pair(it["name_en"], it["name_he"])
        if bool(en) != bool(he):
            targets.append(("p", it, (en, he)))
    for r in shop:
        en, he = normalize_name_pair(r["item_name_en"], r["item_name_he"])
        if bool(en) != bool(he):
            targets.append(("s", r, (en, he)))
    if not targets:
        return 0
    filled = get_parser().translate_pairs([t[2] for t in targets])
    for (kind, row, _), (en, he) in zip(targets, filled):
        if kind == "p":
            svc.update_item(household_id, row["id"], en, he, row["category"], row["units"])
        else:
            svc.update_shopping_item(household_id, row["id"], en, he, row["category"])
    return len(targets)


def send_feedback(message: str):
    """Save the tester's message and email it to the admin."""
    import html as _html
    from notifications import send_admin_email
    ctx = f"lang={st.session_state.get('lang')} items={len(pantry_all) if 'pantry_all' in globals() else '?'}"
    db.add_feedback(st.session_state.get("user_id"), st.session_state.get("household_id"),
                    st.session_state.get("email"), message, ctx)
    send_admin_email(f"Smart Pantry feedback from {st.session_state.get('display_name')}",
                     f"<p><b>From:</b> {_html.escape(str(st.session_state.get('email')))} · "
                     f"{_html.escape(str(st.session_state.get('household_name')))}</p>"
                     f"<p style='white-space:pre-wrap'>{_html.escape(message)}</p><p style='color:#888'>{ctx}</p>")


def reset_selection():
    """Untick every pantry item (new widget keys = fresh, unticked checkboxes)."""
    st.session_state["sel_ver"] = st.session_state.get("sel_ver", 0) + 1


# ==========================================================================
# Language
# ==========================================================================
st.session_state.setdefault("lang", "en")
# Small switch at the top right of every page (the sidebar is hidden on phones).
with st.container(horizontal=True, horizontal_alignment="right", key="lang_bar"):
    lang_choice = st.segmented_control("🌐", ["en", "he"], default=st.session_state["lang"], key="lang_switch",
                                       format_func=lambda x: "EN" if x == "en" else "עב", label_visibility="collapsed")
if lang_choice and lang_choice != st.session_state["lang"]:
    st.session_state["lang"] = lang_choice
    st.rerun()
is_he = st.session_state["lang"] == "he"
tr = make_translator(st.session_state["lang"])
if is_he:
    st.markdown("<style>.stMainBlockContainer, section[data-testid='stSidebar'] {direction: rtl; text-align: right;}</style>",
                unsafe_allow_html=True)


# ==========================================================================
# Authentication
# ==========================================================================
SESSION_COOKIE = "pantry_session"


def _google_enabled() -> bool:
    """Google sign-in shows up only once the [auth] secrets are configured."""
    try:
        return "auth" in st.secrets
    except Exception:
        return False


GOOGLE_ON = _google_enabled()


def _google_user():
    """(email, name) of a verified Google login in this browser, else None."""
    if not GOOGLE_ON:
        return None
    try:
        if not st.user.is_logged_in or st.user.get("email_verified") is False:
            return None
        return (st.user.email or "").strip().lower(), st.user.get("name") or ""
    except Exception:
        return None


def _apply_cookie_ops():
    """Set or clear the remember-me cookie in the browser (runs once, after login/logout)."""
    op = st.session_state.pop("_cookie_op", None)
    if not op:
        return
    if op[0] == "set":
        value, max_age = op[1], 60 * 60 * 24 * 30
    else:
        value, max_age = "", 0
    st.html(
        f"""<script>
        document.cookie = "{SESSION_COOKIE}={value}; Max-Age={max_age}; Path=/; SameSite=Lax"
            + (location.protocol === "https:" ? "; Secure" : "");
        </script>""",
        unsafe_allow_javascript=True,
    )


def login(user: dict, remember: bool = False):
    st.session_state.update({
        "authenticated": True,
        "user_id": user["id"],
        "username": user["username"],
        "display_name": user["display_name"] or user["username"],
        "household_id": user["household_id"],
        "household_name": user["household_name"],
        "household_code": user["household_code"],
        "email": user["email"],
        "intro_dismissed": bool(user.get("intro_dismissed")),
    })
    if remember:
        token = db.create_session(user["id"])
        st.session_state["session_token"] = token
        st.session_state["_cookie_op"] = ("set", token)


def logout():
    db.end_session(st.session_state.get("session_token") or st.context.cookies.get(SESSION_COOKIE))
    google = _google_user() is not None
    lang = st.session_state.get("lang", "en")
    for k in list(st.session_state.keys()):
        del st.session_state[k]
    st.session_state["lang"] = lang
    st.session_state["_cookie_op"] = ("clear",)
    if google:
        st.logout()  # also ends the Google session (Streamlit reloads the page)


def try_auto_login() -> bool:
    """Log in silently from a remember-me cookie or an existing Google login."""
    token = st.context.cookies.get(SESSION_COOKIE)
    user = db.user_for_session(token)
    if user:
        login(user)
        st.session_state["session_token"] = token
        return True
    g = _google_user()
    if g:
        user = db.get_user(email=g[0])
        if user:
            login(user)
            return True
        st.session_state["google_signup"] = {"email": g[0], "name": g[1]}
    return False


def render_google_button(key: str):
    if GOOGLE_ON:
        if st.button(tr("google_btn"), key=key, width="stretch"):
            st.login("google")
        st.caption(f"<div style='text-align:center'>{tr('or_divider')}</div>", unsafe_allow_html=True)


def render_google_signup(info: dict):
    """First Google sign-in for this email: pick or join a household."""
    st.title(tr("title"))
    st.subheader(tr("google_welcome", email=info["email"]))
    with st.form("google_signup_form"):
        name = st.text_input(tr("your_name"), value=info.get("name", "")).strip()
        st.markdown(f"**{tr('household_setup')}**")
        join_code = st.text_input(tr("join_code")).strip()
        house_name = st.text_input(tr("new_house_name"), placeholder="e.g. Herzliya Apartment").strip()
        if st.form_submit_button(tr("create_acct_btn"), type="primary"):
            if not join_code and not house_name:
                st.error(tr("err_need_house"))
            else:
                ok, err = db.create_oauth_user(info["email"], name, house_name, join_code or None)
                if ok:
                    st.session_state.pop("google_signup", None)
                    st.session_state["onboarding_user_id"] = db.get_user(email=info["email"])["id"]
                    st.rerun()
                else:
                    st.error(err)
    if st.button(tr("google_cancel")):
        st.session_state.pop("google_signup", None)
        st.logout()


def render_auth_view():
    st.title(tr("title"))
    st.subheader(tr("login_sub"))
    tab_login, tab_signup, tab_reset = st.tabs([tr("tab_login"), tr("tab_signup"), tr("tab_reset")])

    with tab_login:
        if st.session_state.pop("reset_done", False):
            st.success(tr("reset_ok"))
        render_google_button("google_login")
        with st.form("login_form"):
            email = st.text_input(tr("email")).strip().lower()
            password = st.text_input(tr("password"), type="password")
            remember = st.checkbox(tr("remember_me"), value=True)
            if st.form_submit_button(tr("sign_in_btn"), type="primary"):
                user = db.authenticate(email, password) if email and password else None
                if user:
                    login(user, remember=remember)
                    st.rerun()
                else:
                    st.error(tr("err_creds"))

    with tab_signup:
        render_google_button("google_signup_btn")
        with st.form("signup_form"):
            c1, c2 = st.columns(2)
            first = c1.text_input(tr("first_name")).strip()
            last = c2.text_input(tr("last_name")).strip()
            email = st.text_input(tr("email"), key="su_email").strip().lower()
            password = st.text_input(tr("password"), type="password", key="su_pw")
            st.markdown(f"**{tr('household_setup')}**")
            join_code = st.text_input(tr("join_code")).strip()
            house_name = st.text_input(tr("new_house_name"), placeholder="e.g. Herzliya Apartment").strip()

            if st.form_submit_button(tr("create_acct_btn"), type="primary"):
                if not (first and last and email and password):
                    st.error(tr("err_fill_all"))
                elif not join_code and not house_name:
                    st.error(tr("err_need_house"))
                elif not EMAIL_RE.match(email):
                    st.error(tr("err_email"))
                elif len(password) < 8:
                    st.error(tr("err_pw_short"))
                else:
                    ok, err = db.create_user(email, password, first, last, house_name, join_code or None)
                    if ok:
                        st.session_state["onboarding_user_id"] = db.get_user(email=email)["id"]
                        st.rerun()
                    else:
                        st.error(err)

    with tab_reset:
        render_password_reset()


def render_password_reset():
    """Step 1: email → code is emailed. Step 2: code + new password."""
    reset_email = st.session_state.get("reset_email")

    if not reset_email:
        with st.form("reset_request"):
            st.write(tr("reset_intro"))
            email = st.text_input(tr("email"), key="reset_email_input").strip().lower()
            if st.form_submit_button(tr("reset_send_btn"), type="primary"):
                if not EMAIL_RE.match(email):
                    st.error(tr("err_email"))
                else:
                    result = db.create_reset_code(email)
                    if result["status"] == "ok":
                        lang = st.session_state.get("lang", "en")
                        try:
                            send_reset_email(email, result["code"], lang, RESET_CODE_MINUTES)
                        except Exception:
                            log.exception("Password reset email failed")
                            st.error(tr("reset_send_failed"))
                            return
                    # Same outcome whether or not the email has an account.
                    st.session_state["reset_email"] = email
                    st.rerun()
        return

    st.info(tr("reset_sent", email=reset_email))
    with st.form("reset_confirm"):
        code = st.text_input(tr("reset_code"), max_chars=6, key="reset_code_input").strip()
        new_pw = st.text_input(tr("new_pw"), type="password", key="reset_pw1")
        new_pw2 = st.text_input(tr("confirm_pw"), type="password", key="reset_pw2")
        if st.form_submit_button(tr("reset_confirm_btn"), type="primary"):
            if len(new_pw) < 8:
                st.error(tr("err_pw_short"))
            elif new_pw != new_pw2:
                st.error(tr("pw_mismatch"))
            else:
                ok, reason = db.reset_password_with_code(reset_email, code, new_pw)
                if ok:
                    st.session_state.pop("reset_email", None)
                    st.session_state["reset_done"] = True
                    st.rerun()
                st.error(tr(f"reset_{reason}"))
    if st.button(tr("reset_start_over")):
        st.session_state.pop("reset_email", None)
        st.rerun()


def render_email_onboarding(user_id: int):
    """Shown once, right after a successful signup."""
    st.title(tr("title"))
    st.success(tr("signup_done"))
    st.subheader(tr("email_q_title"))
    wants = st.radio(tr("email_q"), ["yes", "no"], horizontal=True, key="ob_wants",
                     format_func=lambda x: tr(f"answer_{x}"))
    lang = "en"
    if wants == "yes":
        lang = st.radio(tr("email_lang_q"), ["en", "he"], horizontal=True, key="ob_lang",
                        format_func=lambda x: "English" if x == "en" else "עברית")
    if st.button(tr("continue_btn"), type="primary"):
        db.set_email_prefs(user_id, wants == "yes", lang)
        user = db.get_user(user_id=user_id)
        st.session_state.pop("onboarding_user_id", None)
        login(user, remember=_google_user() is None)  # Google keeps its own login
        st.rerun()


_apply_cookie_ops()

if st.session_state.get("onboarding_user_id"):
    render_email_onboarding(st.session_state["onboarding_user_id"])
    st.stop()

if not st.session_state.get("authenticated") and not st.session_state.get("google_signup"):
    try_auto_login()

if st.session_state.get("google_signup") and not st.session_state.get("authenticated"):
    render_google_signup(st.session_state["google_signup"])
    st.stop()

if not st.session_state.get("authenticated"):
    render_auth_view()
    st.stop()

# ==========================================================================
# Session context
# ==========================================================================
household_id = st.session_state["household_id"]
display_name = st.session_state["display_name"]
household_name = st.session_state["household_name"]
today = app_today()

head_l, head_r = st.columns([3, 1.6], vertical_alignment="center")
with head_l:
    st.title(tr("title"))
    st.markdown(f"👤 **{display_name}** · 🏠 **{household_name}** · {tr('invite')} `{st.session_state['household_code']}`")
with head_r, st.container(horizontal=True, wrap=True, gap="small", horizontal_alignment="right"):
    with st.popover(tr("account"), width="content"):
        prefs = db.get_email_prefs(st.session_state["user_id"])
        with st.form("email_prefs"):
            st.markdown(f"**{tr('email_settings')}**")
            p_on = st.toggle(tr("email_toggle"), value=prefs["enabled"])
            p_lang = st.radio(tr("email_lang_q"), ["en", "he"], horizontal=True,
                              index=0 if prefs["lang"] == "en" else 1,
                              format_func=lambda x: "English" if x == "en" else "עברית")
            if st.form_submit_button(tr("save_btn")):
                db.set_email_prefs(st.session_state["user_id"], p_on, p_lang)
                st.success(tr("email_saved"))
        with st.form("change_pw"):
            cur_pw = st.text_input(tr("current_pw"), type="password")
            new_pw = st.text_input(tr("new_pw"), type="password")
            if st.form_submit_button(tr("change_pw_btn")):
                if len(new_pw) < 8:
                    st.error(tr("err_pw_short"))
                elif db.change_password(st.session_state["user_id"], cur_pw, new_pw):
                    # Log out every other device; keep this one.
                    db.end_all_sessions(st.session_state["user_id"], keep_token=st.session_state.get("session_token"))
                    st.success(tr("pw_changed"))
                else:
                    st.error(tr("pw_wrong"))
        if st.button(tr("intro_show"), key="show_intro_btn"):
            st.session_state["intro_dismissed"] = False
            st.rerun()
        with st.expander(tr("danger_title")):
            with st.form("leave_household", border=False):
                st.markdown(f"**{tr('leave_title')}**")
                st.caption(tr("leave_help"))
                new_home = st.text_input(tr("new_house_name"), key="leave_new_name")
                if st.form_submit_button(tr("leave_btn")):
                    if not new_home.strip():
                        st.error(tr("err_need_house"))
                    else:
                        db.leave_household(st.session_state["user_id"], new_home.strip())
                        login(db.get_user(user_id=st.session_state["user_id"]))
                        reset_selection()
                        st.rerun()
            with st.form("delete_account", border=False):
                st.markdown(f"**{tr('delete_acct_title')}**")
                st.caption(tr("delete_acct_help"))
                confirm = st.text_input(tr("delete_acct_confirm"), key="delete_confirm")
                if st.form_submit_button(tr("delete_acct_btn"), type="primary"):
                    if confirm.strip().lower() != (st.session_state.get("email") or "").lower():
                        st.error(tr("delete_acct_mismatch"))
                    else:
                        db.delete_user(st.session_state["user_id"])
                        logout()
                        st.rerun()
        if parser_ready():
            st.markdown(f"**{tr('fix_names_title')}**")
            st.caption(tr("fix_names_help"))
            if st.button(tr("fix_names_btn"), key="fix_names"):
                n = fix_missing_names()
                st.success(tr("fix_names_done", n=n))
    with st.popover(tr("feedback_btn"), width="content"):
        with st.form("feedback_form", clear_on_submit=True, border=False):
            st.markdown(f"**{tr('feedback_title')}**")
            fb = st.text_area(tr("feedback_prompt"), height=120, label_visibility="collapsed",
                              placeholder=tr("feedback_prompt"))
            if st.form_submit_button(tr("feedback_send"), type="primary"):
                if fb.strip():
                    send_feedback(fb)
                    st.success(tr("feedback_thanks"))
                else:
                    st.warning(tr("feedback_empty"))
    if st.button(tr("logout_btn"), width="content"):
        logout()
        st.rerun()

categories = db.get_household_categories(household_id)
cat_keys = [c["en"] for c in categories]
cat_label = {c["en"]: (c["he"] if is_he else c["en"]) for c in categories}


def cat_fmt(key):
    return cat_label.get(key, key or "—")


def name_of(row) -> str:
    he = row.get("name_he", row.get("item_name_he"))
    en = row.get("name_en", row.get("item_name_en"))
    return (he if is_he and he else en) or he or ""


def cat_options_with(current):
    """Category list that always contains the item's current category (avoids silent reassignment)."""
    opts = list(cat_keys)
    if current and current not in opts:
        opts.append(current)
    return opts



def product_labels(products: list[dict]) -> dict[int, str]:
    """Readable, unique dropdown label per product id: 'Milk 3% · חלב 3%'."""
    labels, seen = {}, {}
    for p in products:
        first, second = (p["name_he"], p["name_en"]) if is_he else (p["name_en"], p["name_he"])
        first = first or second
        lbl = first if not second or second == first else f"{first} · {second}"
        seen[lbl] = seen.get(lbl, 0) + 1
        labels[p["id"]] = lbl if seen[lbl] == 1 else f"{lbl} (#{p['id']})"
    return labels


def size_label(size) -> str:
    return size.label("he" if is_he else "en") if size else ""


def risk_color(p: float) -> str:
    if p >= RESTOCK_THRESHOLD:
        return "#D64545"
    if p >= 0.4:
        return "#E0A030"
    return "#2E7D5B"


def render_pantry_row(it: dict, sel_ver: int):
    """One item as a compact row: [✓ name ……… 90%] with details and a thin risk bar underneath.
    Tapping the name ticks the box, which matters on a phone."""
    p = it["prob_depleted"]
    color = risk_color(p)
    qty = f" ×{it['units']}" if it["units"] > 1 else ""
    badges = []
    if it["on_shopping_list"]:
        badges.append(tr("on_list_badge"))
    if it["confirmed_until"]:
        badges.append(tr("confirmed_badge", date=it["confirmed_until"]))
    with st.container(key=f"prow_{it['id']}", gap=None):
        with st.container(horizontal=True, wrap=False, vertical_alignment="center", gap="small"):
            st.checkbox(f"**{it['display_name']}**{qty}", key=f"chk_{sel_ver}_{it['id']}", width="stretch")
            st.markdown(f"<span class='risk-pill' style='background:{color}1A;color:{color}'>{int(p * 100)}%</span>",
                        unsafe_allow_html=True, width="content")
        size_txt = f"📦 {size_label(it['size'])} · " if it.get("size") else ""
        meta = (f"{size_txt}🏷️ {it['category_display']} · {it['purchase_date']} ({tr('days_short', n=it['days_elapsed'])}) · "
                + tr("lasts", d=round(it["typical_days"])))
        badge_html = "".join(f"<span class='row-badge'>{b}</span>" for b in badges)
        st.markdown(
            f"<div class='row-meta'>{meta}{badge_html}</div>"
            f"<div class='risk-track'><div style='width:{max(2, int(p * 100))}%;background:{color}'></div></div>",
            unsafe_allow_html=True)


def render_shop_row(r: dict, sel_ver: int):
    with st.container(key=f"srow_{r['id']}", gap=None):
        st.checkbox(f"**{r['display_name']}**", key=f"shop_{sel_ver}_{r['id']}")
        who = f" · {tr('by', who=r['added_by'])}" if r["added_by"] else ""
        st.markdown(f"<div class='row-meta row-meta-last'>🏷️ {r['category_display']}{who}</div>",
                    unsafe_allow_html=True)


@st.dialog("✏️")
def edit_shop_dialog(r):
    with st.form(f"edit_shop_{r['id']}", border=False):
        n_en = st.text_input(tr("item_name_en"), value=r["item_name_en"] or "")
        n_he = st.text_input(tr("item_name_he"), value=r["item_name_he"] or "")
        opts = cat_options_with(r["category"])
        n_cat = st.selectbox(tr("category"), opts, format_func=cat_fmt,
                             index=opts.index(r["category"]) if r["category"] in opts else 0)
        if st.form_submit_button(tr("update_btn"), type="primary"):
            n_en, n_he = fill_names(n_en, n_he)
            svc.update_shopping_item(household_id, r["id"], n_en, n_he, n_cat)
            st.session_state["shop_ver"] = st.session_state.get("shop_ver", 0) + 1
            st.rerun()


pantry_all = svc.get_pantry(household_id, today)
for it in pantry_all:
    it["display_name"] = name_of(it)
    it["category_display"] = cat_fmt(it["category"])
candidates = svc.restock_candidates(pantry_all)


# ==========================================================================
# Dialogs
# ==========================================================================
@st.dialog("🔔")
def restock_dialog(items):
    st.subheader(tr("restock_title"))
    st.caption(tr("restock_help"))
    rv = st.session_state.get("restk_ver", 0)
    # Inside a form, ticking boxes doesn't rerun the app; only the action buttons do.
    with st.form("restock_form", border=False):
        with st.container(height=320, border=True):
            for it in items:
                c1, c2, c3 = st.columns([0.12, 0.66, 0.22])
                c1.checkbox(" ", key=f"restk_{rv}_{it['id']}", label_visibility="collapsed")
                c2.markdown(f"**{it['display_name']}**  \n:gray[{it['category_display']} · {it['days_elapsed']}d]")
                c3.markdown(f"🚨 **{int(it['prob_depleted'] * 100)}%**")
        b1, b2, b3 = st.columns(3)
        do_add = b1.form_submit_button(tr("add_to_list"), type="primary", width="stretch")
        do_still = b2.form_submit_button(tr("still_going"), width="stretch")
        do_out = b3.form_submit_button(tr("ran_out"), width="stretch")

    if do_add or do_still or do_out:
        chosen = [it for it in items if st.session_state.get(f"restk_{rv}_{it['id']}")]
        if not chosen:
            st.warning(tr("select_first"))
            return
        ids = [i["id"] for i in chosen]
        if do_add:
            svc.add_to_shopping_list(household_id, chosen, "smart_alert", display_name, today)
        elif do_still:
            svc.mark_still_going(household_id, ids, today)
        else:
            svc.mark_depleted(household_id, ids, today, display_name)
        st.session_state["restk_ver"] = rv + 1
        st.rerun()


@st.dialog("✏️")
def edit_dialog(it):
    st.markdown(f"**{tr('edit')}**")
    with st.form(f"edit_{it['id']}", border=False):
        e_en = st.text_input(tr("item_name_en"), value=it["name_en"] or "")
        e_he = st.text_input(tr("item_name_he"), value=it["name_he"] or "")
        e_qty = st.number_input(tr("units"), min_value=1, value=int(it["units"]), step=1)
        opts = cat_options_with(it["category"])
        e_cat = st.selectbox(tr("category"), opts, format_func=cat_fmt,
                             index=opts.index(it["category"]) if it["category"] in opts else 0)
        ref_txt = size_label(it.get("ref_size"))
        e_ref = st.text_input(tr("usual_size"), value=ref_txt, placeholder="1 L / 500 g / 12", help=tr("usual_size_help"))
        if st.form_submit_button(tr("update_btn"), type="primary"):
            new_ref = parse_size_text(e_ref) if e_ref.strip() != ref_txt else None
            if e_ref.strip() and e_ref.strip() != ref_txt and new_ref is None:
                st.error(tr("size_bad"))
                return
            e_en, e_he = fill_names(e_en, e_he)
            svc.update_item(household_id, it["id"], e_en, e_he, e_cat, e_qty)
            if new_ref and it.get("product_id"):
                svc.set_reference_size(household_id, it["product_id"], new_ref.amount, new_ref.unit)
            reset_selection()
            st.rerun()


@st.dialog("🔀")
def merge_dialog(selected):
    if len(selected) < 2:
        st.warning(tr("merge_need_two"))
        return
    st.write(tr("merge_pick_keep"))
    keep = st.selectbox(tr("merge_keep"), selected,
                        format_func=lambda x: f"{x['display_name']} ×{x['units']} ({x['category_display']})")
    if st.button(tr("merge_confirm"), type="primary"):
        svc.merge_items(household_id, keep["id"], [i["id"] for i in selected])
        reset_selection()
        st.rerun()



# ==========================================================================
# Add groceries: one place for every way of logging a purchase
# ==========================================================================
def render_single_item():
    all_products = svc.get_products(household_id)
    p_labels = product_labels(all_products)
    with st.form("add_purchase", clear_on_submit=True):
        p_pid = st.selectbox(tr("pick_product"), [None] + list(p_labels), index=0,
                             format_func=lambda x: tr("new_product") if x is None else p_labels[x])
        st.caption(tr("pick_product_help"))
        c1, c2, c3 = st.columns([2, 2, 1])
        p_en = c1.text_input(tr("item_name_en"), placeholder="e.g. 3% Milk")
        p_cat = c1.selectbox(tr("category"), cat_keys, format_func=cat_fmt)
        p_he = c2.text_input(tr("item_name_he"), placeholder="למשל: חלב 3%")
        p_date = c2.date_input(tr("purchase_date"), value=today, max_value=today,
                               min_value=today - timedelta(days=365))
        p_qty = c3.number_input(tr("units"), min_value=1, value=1, step=1)
        p_size = c3.text_input(tr("size_col"), placeholder="1 L")
        if st.form_submit_button(tr("save_btn"), type="primary"):
            size = parse_size_text(p_size)
            if p_size.strip() and size is None:
                st.error(tr("size_bad"))
            elif p_pid is not None:
                svc.record_purchase(household_id, "", "", None, int(p_qty), p_date, display_name,
                                    product_id=p_pid, size=size)
                st.rerun()
            elif not (p_en.strip() or p_he.strip()):
                st.error(tr("need_name"))
            else:
                p_en, p_he = fill_names(p_en, p_he)
                svc.record_purchase(household_id, p_en, p_he, p_cat, int(p_qty), p_date, display_name, size=size)
                st.rerun()


def render_paste_list():
    parser = get_parser()
    st.caption(tr("quick_add_help"))
    pasted = st.text_area(tr("quick_add_items"), placeholder="Milk\n2 eggs\nלחם", height=140, key="paste_text")
    paste_date = st.date_input(tr("purchase_date"), value=today, max_value=today, key="paste_date")
    if st.button(tr("process"), type="primary", disabled=not parser.available,
                         help=None if parser.available else "Set OPENAI_API_KEY to enable"):
        if not pasted.strip():
            st.error(tr("paste_first"))
        else:
            loader = st.empty()
            loader.markdown(cart_spinner_html(tr("quick_add_working")), unsafe_allow_html=True)
            try:
                parsed = parser.parse(pasted, categories)
                products_now = svc.get_products(household_id)
                by_id = {p["id"]: p for p in products_now}
                suggestions = ProductMatcher(products_now, svc.get_aliases(household_id), parser).suggest(parsed)
                for it, sug in zip(parsed, suggestions):
                    it["product_id"] = sug.product_id
                    it["match"] = sug.reason
                    _, sz = extract_size(it.get("raw_name"))
                    it["size"] = size_label(sz)
                    if sug.product_id:  # matched products keep their own names and category
                        it["category"] = by_id[sug.product_id]["category"] or it.get("category")
                loader.empty()
                st.session_state["staged_items"] = parsed
                st.session_state["staged_date"] = paste_date
                st.rerun()
            except RuntimeError as e:
                loader.empty()
                st.error(str(e))


def render_review():
    if st.session_state.get("staged_items"):
        st.subheader(tr("verify_title"))
        st.caption(tr("verify_help"))
        rv_products = svc.get_products(household_id)
        rv_labels = product_labels(rv_products)
        label_to_id = {v: k for k, v in rv_labels.items()}
        new_lbl = tr("new_product")
        match_icon = {"alias": tr("match_alias"), "similar": tr("match_similar"), "ai": tr("match_ai"), "new": tr("match_new")}

        staged = pd.DataFrame(st.session_state["staged_items"])
        staged.insert(0, "include", True)
        staged["product"] = [rv_labels.get(pid, new_lbl) if pid else new_lbl for pid in staged.get("product_id", [None] * len(staged))]
        staged["match"] = staged.get("match", "new").map(lambda r: match_icon.get(r, r))
        for col in ("raw_name", "size"):
            if col not in staged:
                staged[col] = ""
        edited = st.data_editor(
            staged[["include", "raw_name", "product", "units", "size", "match", "name_en", "name_he", "category"]],
            column_config={
                "include": st.column_config.CheckboxColumn(tr("keep"), default=True),
                "raw_name": st.column_config.TextColumn(tr("line_col"), disabled=True),
                "product": st.column_config.SelectboxColumn(tr("product_col"), options=[new_lbl] + list(rv_labels.values()),
                                                             required=True, width="medium"),
                "match": st.column_config.TextColumn(tr("match_col"), disabled=True, width="small"),
                "name_en": st.column_config.TextColumn(tr("item_name_en")),
                "name_he": st.column_config.TextColumn(tr("item_name_he")),
                "category": st.column_config.SelectboxColumn(tr("category"), options=cat_keys),
                "units": st.column_config.NumberColumn(tr("units"), min_value=1, step=1, default=1),
                "size": st.column_config.TextColumn(tr("size_col"), help=tr("size_help")),
            },
            hide_index=True, width="stretch", key="staged_editor",
        )
        c1, c2 = st.columns(2)
        if c1.button(tr("commit"), type="primary"):
            keep = edited[edited["include"]].copy()
            for col in ("name_en", "name_he", "raw_name", "size"):
                keep[col] = keep[col].fillna("").astype(str).str.strip()
            keep["pid"] = keep["product"].map(lambda lbl: label_to_id.get(lbl))
            is_new = keep["pid"].isna()
            nameless = keep[is_new & (keep["name_en"] == "") & (keep["name_he"] == "")]
            missing = keep[is_new & (keep["category"].isna() | ~keep["category"].isin(cat_keys))]
            bad_size = [r.raw_name or r.name_en for r in keep.itertuples() if r.size and parse_size_text(r.size) is None]
            if not nameless.empty:
                st.error(tr("need_name"))
            elif not missing.empty:
                st.error(tr("need_category", names=", ".join(missing["name_en"].where(missing["name_en"] != "", missing["name_he"]))))
            elif bad_size:
                st.error(tr("size_bad") + " " + ", ".join(bad_size))
            else:
                records = []
                for r in keep.itertuples():
                    pid = None if pd.isna(r.pid) else int(r.pid)
                    en, he = (r.name_en, r.name_he) if pid else fill_names(r.name_en, r.name_he)
                    records.append({"product_id": pid, "name_en": en, "name_he": he, "category": r.category,
                                    "units": int(r.units or 1), "size": parse_size_text(r.size),
                                    "raw_name": r.raw_name or None})
                svc.record_purchases(household_id, records, st.session_state["staged_date"], display_name)
                st.session_state.pop("staged_items", None)
                st.toast(tr("saved_n", n=len(records)))
                st.rerun()
        if c2.button(tr("discard")):
            st.session_state.pop("staged_items", None)
            st.rerun()


def render_receipt_tab():
    st.info(tr("receipt_soon"))


def render_add_groceries():
    staged = bool(st.session_state.get("staged_items"))
    with st.expander(tr("add_groceries"), expanded=staged or not pantry_all):
        t_receipt, t_paste, t_single = st.tabs([tr("add_tab_receipt"), tr("add_tab_paste"), tr("add_tab_single")])
        with t_receipt:
            render_receipt_tab()
        with t_paste:
            render_paste_list()
        with t_single:
            render_single_item()
    if staged:
        with st.container(border=True):
            render_review()


def render_intro():
    with st.container(border=True, key="intro_card"):
        st.markdown(f"#### {tr('intro_title')}")
        st.markdown(tr("intro_body"))
        if st.button(tr("intro_ok"), type="primary", key="intro_ok"):
            db.set_intro_dismissed(st.session_state["user_id"])
            st.session_state["intro_dismissed"] = True
            st.rerun()


if not st.session_state.get("intro_dismissed"):
    render_intro()

render_add_groceries()

if candidates:
    if st.button(tr("restock_banner", n=len(candidates)), type="primary", width="stretch"):
        restock_dialog(candidates)

from notifications import admin_emails  # noqa: E402
IS_ADMIN = (st.session_state.get("email") or "").lower() in admin_emails()
_tabs = st.tabs([tr("tab_pantry"), tr("tab_shopping"), tr("tab_categories"), tr("tab_curves")]
                + ([tr("tab_admin")] if IS_ADMIN else []))
tab_pantry, tab_shopping, tab_categories, tab_curves = _tabs[:4]
tab_admin = _tabs[4] if IS_ADMIN else None

# ==========================================================================
# Tab 1: Pantry
# ==========================================================================
with tab_pantry:
    with st.expander(tr("audit")):
        if st.toggle(tr("show"), key="show_audit"):  # only query when someone actually looks
            recent = svc.recent_purchases(household_id, 60, today)
            if recent:
                df = pd.DataFrame(recent)
                df["category"] = df["category"].map(cat_fmt)
                df = df.rename(columns={"purchase_date": tr("purchase_date"), "item_name_en": tr("item_name_en"),
                                        "item_name_he": tr("item_name_he"), "category": tr("category"),
                                        "units": tr("units"), "recorded_by": tr("added_by_col")})
                st.dataframe(df, width="stretch", hide_index=True)
            else:
                st.info(tr("audit_none"))

    if not pantry_all:
        st.info(tr("empty_pantry"))
    else:
        f1, f_rest = st.columns([2, 2])
        f_row = f_rest.container(horizontal=True, wrap=False, gap="small")
        f2, f3 = f_row, f_row
        search = f1.text_input(tr("search"), key="pantry_search", label_visibility="collapsed",
                               placeholder=tr("search")).strip().lower()
        cat_filter = f2.selectbox(tr("category"), ["__all__"] + cat_keys, key=f"pantry_cat_{st.session_state['lang']}", label_visibility="collapsed",
                                  format_func=lambda x: tr("all_categories") if x == "__all__" else cat_fmt(x))
        sort_opts = ["risk", "category", "name", "date"]
        sort_by = f3.selectbox(tr("sort_by"), sort_opts, key=f"pantry_sort_{st.session_state['lang']}", label_visibility="collapsed",
                               format_func=lambda x: tr(f"sort_{x}"))

        items = [i for i in pantry_all
                 if (not search or search in (i["name_en"] or "").lower() or search in (i["name_he"] or "").lower())
                 and (cat_filter == "__all__" or i["category"] == cat_filter)]
        sorters = {
            "risk": lambda x: -x["prob_depleted"],
            "category": lambda x: (x["category_display"].lower(), x["display_name"].lower()),
            "name": lambda x: x["display_name"].lower(),
            "date": lambda x: x["purchase_date"],
        }
        items.sort(key=sorters[sort_by], reverse=(sort_by == "date"))

        sel_ver = st.session_state.setdefault("sel_ver", 0)
        # The list is a form: ticking items is instant and runs nothing.
        # The app only runs when one of the action buttons is pressed.
        with st.form("pantry_select", border=False):
            with st.container(horizontal=True, key="pantry_actions"):  # sticky, see ui_theme.py
                act_out = st.form_submit_button(tr("ran_out"))
                act_still = st.form_submit_button(tr("still_going"))
                act_list = st.form_submit_button(tr("add_to_list"))
                act_edit = st.form_submit_button(tr("edit_btn"))
                act_merge = st.form_submit_button(tr("merge"))
                act_del = st.form_submit_button(tr("delete"),
                                                help=tr("delete_help"))
            st.caption(tr("select_hint"))

            for it in items:
                render_pantry_row(it, sel_ver)

        if any([act_out, act_still, act_list, act_edit, act_merge, act_del]):
            selected = [i for i in items if st.session_state.get(f"chk_{sel_ver}_{i['id']}")]
            ids = [i["id"] for i in selected]
            if not selected:
                st.warning(tr("select_first"))
            elif act_edit:
                if len(selected) == 1:
                    edit_dialog(selected[0])
                else:
                    st.warning(tr("select_one"))
            elif act_merge:
                merge_dialog(selected)
            else:
                if act_out:
                    svc.mark_depleted(household_id, ids, today, display_name)
                elif act_still:
                    svc.mark_still_going(household_id, ids, today)
                elif act_list:
                    svc.add_to_shopping_list(household_id, selected, "manual", display_name, today)
                elif act_del:
                    svc.delete_items(household_id, ids)
                reset_selection()
                st.rerun()

        export = pd.DataFrame([{
            tr("item_name_en"): i["name_en"], tr("item_name_he"): i["name_he"], tr("category"): cat_fmt(i["category"]),
            tr("units"): i["units"], tr("size_col"): size_label(i.get("size")), tr("purchase_date"): i["purchase_date"],
            tr("x_days_since"): i["days_elapsed"], tr("x_confirmed"): i["confirmed_until"],
            tr("depletion_risk"): round(i["prob_depleted"], 3), tr("x_typical"): round(i["typical_days"], 1),
        } for i in pantry_all])
        st.download_button(tr("export"), export.to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"pantry_{today}.csv", mime="text/csv")

# ==========================================================================
# Tab 2: Shopping list
# ==========================================================================
with tab_shopping:
    with st.expander(tr("manual_add_shop")):
        with st.form("add_shop", clear_on_submit=True):
            c1, c2, c3 = st.columns([2, 2, 2])
            s_en = c1.text_input(tr("item_name_en"), placeholder="e.g. Sourdough bread")
            s_he = c2.text_input(tr("item_name_he"), placeholder="למשל: לחם מחמצת")
            s_cat = c3.selectbox(tr("category"), cat_keys, format_func=cat_fmt)
            if st.form_submit_button(tr("save_btn"), type="primary"):
                if not (s_en.strip() or s_he.strip()):
                    st.error(tr("need_name"))
                else:
                    s_en, s_he = fill_names(s_en, s_he)
                    svc.add_to_shopping_list(household_id, [{"name_en": s_en, "name_he": s_he, "category": s_cat}],
                                             "manual", display_name, today)
                    st.rerun()

    shop_all = svc.get_shopping_list(household_id)
    if not shop_all:
        st.write(tr("empty_list"))
    else:
        for r in shop_all:
            r["display_name"] = name_of(r)
            r["category_display"] = cat_fmt(r["category"])

        f1, f_rest = st.columns([2, 2])
        f_row = f_rest.container(horizontal=True, wrap=False, gap="small")
        f2, f3 = f_row, f_row
        s_search = f1.text_input(tr("search"), key="shop_search", label_visibility="collapsed",
                                 placeholder=tr("search")).strip().lower()
        s_filter = f2.selectbox(tr("category"), ["__all__"] + cat_keys, key=f"shop_cat_{st.session_state['lang']}", label_visibility="collapsed",
                                format_func=lambda x: tr("all_categories") if x == "__all__" else cat_fmt(x))
        order = f3.selectbox(tr("org_label"), ["custom", "category", "name"], key=f"shop_order_{st.session_state['lang']}",
                             label_visibility="collapsed", format_func=lambda x: tr(f"org_{x}"))

        rows = [r for r in shop_all
                if (not s_search or s_search in (r["item_name_en"] or "").lower()
                    or s_search in (r["item_name_he"] or "").lower())
                and (s_filter == "__all__" or r["category"] == s_filter)]
        is_filtered = len(rows) != len(shop_all)

        if order == "category":
            rows.sort(key=lambda x: (x["category_display"].lower(), x["display_name"].lower()))
        elif order == "name":
            rows.sort(key=lambda x: x["display_name"].lower())
        elif HAS_SORTABLES and not is_filtered and len(rows) > 1:
            with st.expander(tr("reorder_title")):
                st.caption(tr("drag_caption"))
                labels = {r["id"]: f"{r['display_name']} · {r['category_display']}  #{r['id']}" for r in rows}
                current = [labels[r["id"]] for r in rows]
                reordered = sort_items(current, direction="vertical", key=f"dnd_{household_id}")
                if reordered and reordered != current:
                    by_label = {v: k for k, v in labels.items()}
                    svc.reorder_shopping_list(household_id, [by_label[lbl] for lbl in reordered])
                    st.rerun()
        elif is_filtered:
            st.caption(tr("drag_filtered"))
        elif not HAS_SORTABLES:
            st.caption(tr("sortables_missing"))

        shop_ver = st.session_state.setdefault("shop_ver", 0)
        # Like the pantry: tick items as you shop (instant), then press one button.
        with st.form("shop_select", border=False):
            with st.container(horizontal=True, key="shop_actions"):  # sticky, see ui_theme.py
                act_bought = st.form_submit_button(tr("bought_btn"), type="primary")
                act_sedit = st.form_submit_button(tr("edit_btn"))
                act_remove = st.form_submit_button(tr("remove_btn"))
            for r in rows:
                render_shop_row(r, shop_ver)

        if act_bought or act_sedit or act_remove:
            chosen = [r for r in rows if st.session_state.get(f"shop_{shop_ver}_{r['id']}")]
            if not chosen:
                st.warning(tr("select_first"))
            elif act_sedit:
                if len(chosen) == 1:
                    edit_shop_dialog(chosen[0])
                else:
                    st.warning(tr("select_one"))
            else:
                for r in chosen:
                    if act_bought:
                        svc.bought_from_list(household_id, r["id"], 1, display_name, today)
                    else:
                        svc.remove_from_shopping_list(household_id, r["id"])
                if act_bought:
                    st.toast(tr("bought_n", n=len(chosen)))
                st.session_state["shop_ver"] = shop_ver + 1
                st.rerun()

# ==========================================================================
# Tab 3: Categories
# ==========================================================================
with tab_categories:
    col_add, col_list = st.columns([2, 3])
    with col_add:
        with st.form("add_cat", clear_on_submit=True):
            n_en = st.text_input(tr("cat_name_en"), placeholder="e.g. Vegan Alternatives")
            n_he = st.text_input(tr("cat_name_he"), placeholder="למשל: תחליפי טבעונות")
            if st.form_submit_button(tr("cat_add")) and n_en.strip():
                if db.add_category(household_id, n_en, n_he):
                    st.rerun()
                else:
                    st.error(tr("cat_exists"))
    with col_list:
        for c in categories:
            l1, l2, l3 = st.columns([3, 2, 0.6])
            l1.markdown(f"**{c['en']}** / {c['he']}")
            prior = engine.category_params(c["en"])
            l2.caption(tr("cat_typical", d=f"{prior['median_days']:.0f}") + ("" if prior["prior_key"] else tr("cat_default")))
            if l3.button("🗑️", key=f"del_cat_{c['id']}"):
                ok, n = db.delete_category(household_id, c["en"])
                if ok:
                    st.rerun()
                else:
                    st.error(tr("cat_in_use", n=n))

    with st.expander(tr("products_title")):
        prods = svc.get_products(household_id)
        labels = product_labels(prods)
        st.caption(tr("products_help"))
        if prods:
            st.dataframe(pd.DataFrame([{
                tr("product_col"): labels[p["id"]],
                tr("category"): cat_fmt(p["category"]),
                tr("usual_size"): size_label(Size(p["ref_amount"], p["ref_unit"])) if p["ref_amount"] else "—",
                tr("names_known"): p["alias_count"],
            } for p in prods]), hide_index=True, width="stretch")
        if len(prods) >= 2:
            with st.form("merge_products"):
                m1, m2 = st.columns(2)
                drop = m1.selectbox(tr("merge_from"), list(labels), format_func=lambda x: labels[x])
                keep_p = m2.selectbox(tr("merge_into"), list(labels), index=1, format_func=lambda x: labels[x])
                if st.form_submit_button(tr("merge_btn")):
                    if drop == keep_p:
                        st.error(tr("merge_same"))
                    else:
                        svc.merge_products(household_id, keep_p, drop)
                        st.success(tr("merged_ok"))
                        st.rerun()

# ==========================================================================
# Tab 4: How it predicts
# ==========================================================================
with tab_curves:
    if pantry_all:
        by_id = {i["id"]: i for i in pantry_all}
        pick = st.selectbox(tr("curve_pick"), list(by_id), key=f"curve_item_{st.session_state['lang']}",
                            format_func=lambda i: f"{by_id[i]['display_name']} ({by_id[i]['category_display']})")
        it = by_id[pick]
        stats = svc.item_learning_stats(household_id, it["item_key"], it["category"])

        horizon = max(30, int(it["effective_scale"] * 2.5), it["days_elapsed"] + 5)
        ts = list(range(0, horizon + 1))
        prior_curve = engine.survival_curve(it["category"], stats["prior_scale"], it["units"], ts)
        your_curve = engine.survival_curve(it["category"], it["per_unit_scale"], it["units"], ts, it["days_confirmed"])

        fig = go.Figure()
        fig.add_scatter(x=ts, y=prior_curve, mode="lines", name=tr("curve_prior"),
                        line=dict(dash="dash", color="gray"))
        fig.add_scatter(x=ts, y=your_curve, mode="lines", name=tr("curve_yours"), line=dict(color="#2ca02c", width=3))
        fig.add_scatter(x=[it["days_elapsed"]], y=[1 - it["prob_depleted"]], mode="markers+text",
                        name=tr("curve_today"), marker=dict(size=12, color="red"),
                        text=[tr("curve_today")], textposition="top right")
        fig.update_layout(xaxis_title=tr("curve_x"), yaxis_title=tr("curve_y"), template="plotly_white",
                          yaxis=dict(range=[0, 1.05], tickformat=".0%"), margin=dict(t=20))
        st.plotly_chart(fig, width="stretch")
        st.caption(tr("curve_stats", n=stats["item_cycles"], c=stats["category_cycles"],
                      prior=stats["prior_key"] or "default"))
    else:
        st.info(tr("empty_pantry"))


# ==========================================================================
# Tab 5: Admin (only for ADMIN_EMAILS)
# ==========================================================================
if tab_admin is not None:
    with tab_admin:
        stats = db.admin_stats(today.isoformat())
        m1, m2, m3, m4 = st.columns(4)
        m1.metric(tr("adm_households"), len(stats))
        m2.metric(tr("adm_users"), sum(r["members"] for r in stats))
        m3.metric(tr("adm_buys_7d"), sum(r["buys_7d"] for r in stats))
        m4.metric(tr("adm_active_7d"), sum(1 for r in stats if r["buys_7d"]))
        st.markdown(f"**{tr('adm_by_household')}**")
        st.dataframe(pd.DataFrame([{
            tr("adm_household"): r["name"], tr("adm_code"): r["household_code"], tr("adm_members"): r["members"],
            tr("adm_products"): r["products"], tr("adm_in_pantry"): r["in_pantry"], tr("adm_buys_7d"): r["buys_7d"],
            tr("adm_buys_30d"): r["buys_30d"], tr("adm_cycles"): r["cycles"], tr("adm_last"): r["last_purchase"] or "—",
        } for r in stats]), hide_index=True, width="stretch")
        st.caption(tr("adm_cycles_help"))

        st.markdown(f"**{tr('adm_feedback')}**")
        fb = db.list_feedback(50)
        if fb:
            st.dataframe(pd.DataFrame([{tr("adm_when"): f["created_at"][:16].replace("T", " "),
                                         tr("email"): f["email"] or "—", tr("adm_message"): f["message"],
                                         tr("adm_context"): f["context"]} for f in fb]),
                         hide_index=True, width="stretch")
        else:
            st.caption(tr("adm_none"))

        st.markdown(f"**{tr('adm_errors')}**")
        errs = db.list_errors(30)
        if not errs:
            st.caption(tr("adm_no_errors"))
        for e in errs:
            with st.expander(f"⚠️ `{e['signature']}` · ×{e['count']} · {e['last_seen'][:16].replace('T', ' ')}"):
                st.caption(f"{tr('adm_last_user')}: {e['last_user']} · {tr('adm_first_seen')}: {e['first_seen'][:16]}")
                st.code(e["traceback"] or e["message"], language="text")
