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

from config import DB_PATH, today as app_today
from db import RESET_CODE_MINUTES
from db import Database
from engine import PantryDepletionEngine
from i18n import make_translator
from notifications import send_reset_email
from pantry_service import PantryService
from text_parser import TextListParser
from ui_theme import apply_theme, cart_spinner_html

try:
    from streamlit_sortables import sort_items
    HAS_SORTABLES = True
except ImportError:
    HAS_SORTABLES = False

st.set_page_config(page_title="Smart Pantry Assistant", page_icon="🛒", layout="wide")
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


def reset_selection():
    """Untick every pantry item (new widget keys = fresh, unticked checkboxes)."""
    st.session_state["sel_ver"] = st.session_state.get("sel_ver", 0) + 1


# ==========================================================================
# Language
# ==========================================================================
st.session_state.setdefault("lang", "en")
lang_choice = st.sidebar.selectbox("🌐 Language / שפה", ["English", "עברית"],
                                   index=0 if st.session_state["lang"] == "en" else 1)
st.session_state["lang"] = "en" if lang_choice == "English" else "he"
is_he = st.session_state["lang"] == "he"
tr = make_translator(st.session_state["lang"])
if is_he:
    st.markdown("<style>.stMainBlockContainer, section[data-testid='stSidebar'] {direction: rtl; text-align: right;}</style>",
                unsafe_allow_html=True)


# ==========================================================================
# Authentication
# ==========================================================================
def login(user: dict):
    st.session_state.update({
        "authenticated": True,
        "user_id": user["id"],
        "username": user["username"],
        "display_name": user["display_name"] or user["username"],
        "household_id": user["household_id"],
        "household_name": user["household_name"],
        "household_code": user["household_code"],
        "email": user["email"],
    })


def logout():
    for k in list(st.session_state.keys()):
        if k != "lang":
            del st.session_state[k]


def render_auth_view():
    st.title(tr("title"))
    st.subheader(tr("login_sub"))
    tab_login, tab_signup, tab_reset = st.tabs([tr("tab_login"), tr("tab_signup"), tr("tab_reset")])

    with tab_login:
        if st.session_state.pop("reset_done", False):
            st.success(tr("reset_ok"))
        with st.form("login_form"):
            email = st.text_input(tr("email")).strip().lower()
            password = st.text_input(tr("password"), type="password")
            if st.form_submit_button(tr("sign_in_btn"), type="primary"):
                user = db.authenticate(email, password) if email and password else None
                if user:
                    login(user)
                    st.rerun()
                else:
                    st.error(tr("err_creds"))

    with tab_signup:
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
        login(user)
        st.rerun()


if st.session_state.get("onboarding_user_id"):
    render_email_onboarding(st.session_state["onboarding_user_id"])
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

head_l, head_r = st.columns([4, 1.4])
with head_l:
    st.title(tr("title"))
    st.markdown(f"👤 **{display_name}** · 🏠 **{household_name}** · {tr('invite')} `{st.session_state['household_code']}`")
with head_r:
    with st.popover(tr("account"), width="stretch"):
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
                    st.success(tr("pw_changed"))
                else:
                    st.error(tr("pw_wrong"))
    if st.button(tr("logout_btn"), width="stretch"):
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
        if st.form_submit_button(tr("update_btn"), type="primary"):
            svc.update_item(household_id, it["id"], e_en, e_he, e_cat, e_qty)
            reset_selection()
            st.rerun()


@st.dialog("🔀")
def merge_dialog(selected):
    if len(selected) < 2:
        st.warning("Select at least 2 items to merge.")
        return
    st.write("Pick the item to keep. The others' units are added to it and they are removed.")
    keep = st.selectbox("Keep", selected,
                        format_func=lambda x: f"{x['display_name']} ×{x['units']} ({x['category_display']})")
    if st.button("Confirm merge", type="primary"):
        svc.merge_items(household_id, keep["id"], [i["id"] for i in selected])
        reset_selection()
        st.rerun()


if candidates:
    if st.button(tr("restock_banner", n=len(candidates)), type="primary", width="stretch"):
        restock_dialog(candidates)

tab_pantry, tab_shopping, tab_categories, tab_curves = st.tabs(
    [tr("tab_pantry"), tr("tab_shopping"), tr("tab_categories"), tr("tab_curves")])

# ==========================================================================
# Tab 1: Pantry
# ==========================================================================
with tab_pantry:
    with st.expander(tr("manual_add_pantry")):
        with st.form("add_purchase", clear_on_submit=True):
            c1, c2, c3 = st.columns([2, 2, 1])
            p_en = c1.text_input(tr("item_name_en"), placeholder="e.g. 3% Milk")
            p_cat = c1.selectbox(tr("category"), cat_keys, format_func=cat_fmt)
            p_he = c2.text_input(tr("item_name_he"), placeholder="למשל: חלב 3%")
            p_date = c2.date_input(tr("purchase_date"), value=today, max_value=today,
                                   min_value=today - timedelta(days=365))
            p_qty = c3.number_input(tr("units"), min_value=1, value=1, step=1)
            if st.form_submit_button(tr("save_btn"), type="primary"):
                if not (p_en.strip() or p_he.strip()):
                    st.error(tr("need_name"))
                else:
                    svc.record_purchase(household_id, p_en, p_he, p_cat, int(p_qty), p_date, display_name)
                    st.rerun()

    with st.expander(tr("audit")):
        if st.toggle("Show", key="show_audit"):  # only query when someone actually looks
            recent = svc.recent_purchases(household_id, 60, today)
            if recent:
                df = pd.DataFrame(recent)
                df["category"] = df["category"].map(cat_fmt)
                st.dataframe(df, width="stretch", hide_index=True)
            else:
                st.info(tr("audit_none"))

    if not pantry_all:
        st.info(tr("empty_pantry"))
    else:
        f1, f2, f3 = st.columns([2, 1, 1])
        search = f1.text_input(tr("search"), key="pantry_search", label_visibility="collapsed",
                               placeholder=tr("search")).strip().lower()
        cat_filter = f2.selectbox(tr("category"), [None] + cat_keys, key="pantry_cat", label_visibility="collapsed",
                                  format_func=lambda x: tr("all_categories") if x is None else cat_fmt(x))
        sort_opts = ["risk", "category", "name", "date"]
        sort_by = f3.selectbox(tr("sort_by"), sort_opts, key="pantry_sort", label_visibility="collapsed",
                               format_func=lambda x: tr(f"sort_{x}"))

        items = [i for i in pantry_all
                 if (not search or search in (i["name_en"] or "").lower() or search in (i["name_he"] or "").lower())
                 and (cat_filter is None or i["category"] == cat_filter)]
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
            with st.container(horizontal=True):
                act_out = st.form_submit_button(tr("ran_out"))
                act_still = st.form_submit_button(tr("still_going"))
                act_list = st.form_submit_button(tr("add_to_list"))
                act_edit = st.form_submit_button(tr("edit_btn"))
                act_merge = st.form_submit_button(tr("merge"))
                act_del = st.form_submit_button(tr("delete"),
                                                help="Remove a mistaken entry (doesn't teach the model)")
            st.caption(tr("select_hint"))

            for it in items:
                c_chk, c1, c2 = st.columns([0.4, 3.8, 2.8])
                c_chk.checkbox(" ", key=f"chk_{sel_ver}_{it['id']}", label_visibility="collapsed")
                with c1:
                    qty = f" ×{it['units']}" if it["units"] > 1 else ""
                    badges = []
                    if it["on_shopping_list"]:
                        badges.append(tr("on_list_badge"))
                    if it["confirmed_until"]:
                        badges.append(tr("confirmed_badge", date=it["confirmed_until"]))
                    st.markdown(f"**{it['display_name']}**{qty}  " + ("  ".join(f":blue-badge[{b}]" for b in badges)))
                    st.caption(f"🏷️ {it['category_display']} · {it['purchase_date']} ({it['days_elapsed']}d) · "
                               + tr("lasts", d=round(it["effective_scale"])))
                with c2:
                    p = it["prob_depleted"]
                    st.progress(p)
                    st.caption(f"{tr('depletion_risk')}: **{int(p * 100)}%**")

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
            "Item (EN)": i["name_en"], "Item (HE)": i["name_he"], "Category": i["category"], "Units": i["units"],
            "Purchase date": i["purchase_date"], "Days since purchase": i["days_elapsed"],
            "Still had it on": i["confirmed_until"], "Chance it's gone": round(i["prob_depleted"], 3),
            "Per-unit scale (days)": round(i["per_unit_scale"], 1),
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
                    svc.add_to_shopping_list(household_id, [{
                        "name_en": s_en.strip() or s_he.strip(), "name_he": s_he.strip() or s_en.strip(),
                        "category": s_cat}], "manual", display_name, today)
                    st.rerun()

    shop_all = svc.get_shopping_list(household_id)
    if not shop_all:
        st.write(tr("empty_list"))
    else:
        for r in shop_all:
            r["display_name"] = name_of(r)
            r["category_display"] = cat_fmt(r["category"])

        f1, f2, f3 = st.columns([2, 1, 1.4])
        s_search = f1.text_input(tr("search"), key="shop_search", label_visibility="collapsed",
                                 placeholder=tr("search")).strip().lower()
        s_filter = f2.selectbox(tr("category"), [None] + cat_keys, key="shop_cat", label_visibility="collapsed",
                                format_func=lambda x: tr("all_categories") if x is None else cat_fmt(x))
        order = f3.selectbox(tr("org_label"), ["custom", "category", "name"], key="shop_order",
                             label_visibility="collapsed", format_func=lambda x: tr(f"org_{x}"))

        rows = [r for r in shop_all
                if (not s_search or s_search in (r["item_name_en"] or "").lower()
                    or s_search in (r["item_name_he"] or "").lower())
                and (s_filter is None or r["category"] == s_filter)]
        is_filtered = len(rows) != len(shop_all)

        if order == "category":
            rows.sort(key=lambda x: (x["category_display"].lower(), x["display_name"].lower()))
        elif order == "name":
            rows.sort(key=lambda x: x["display_name"].lower())
        elif HAS_SORTABLES and not is_filtered and len(rows) > 1:
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

        st.divider()
        for r in rows:
            c1, c2, c3, c4, c5 = st.columns([3.6, 1.8, 0.6, 0.6, 1.2])
            who = f" :gray[{tr('by', who=r['added_by'])}]" if r["added_by"] else ""
            c1.markdown(f"**{r['display_name']}**{who}")
            c2.caption(f"🏷️ {r['category_display']}")
            with c3:
                with st.popover("✏️"):
                    with st.form(f"edit_shop_{r['id']}"):
                        n_en = st.text_input(tr("item_name_en"), value=r["item_name_en"] or "")
                        n_he = st.text_input(tr("item_name_he"), value=r["item_name_he"] or "")
                        opts = cat_options_with(r["category"])
                        n_cat = st.selectbox(tr("category"), opts, format_func=cat_fmt,
                                             index=opts.index(r["category"]) if r["category"] in opts else 0)
                        if st.form_submit_button(tr("update_btn")):
                            svc.update_shopping_item(household_id, r["id"], n_en, n_he, n_cat)
                            st.rerun()
            if c4.button("🗑️", key=f"del_shop_{r['id']}"):
                svc.remove_from_shopping_list(household_id, r["id"])
                st.rerun()
            if c5.button(tr("bought_btn"), key=f"bought_{r['id']}", width="stretch"):
                svc.bought_from_list(household_id, r["id"], 1, display_name, today)
                st.toast(f"✅ {r['display_name']}")
                st.rerun()

# ==========================================================================
# Tab 3: Categories
# ==========================================================================
with tab_categories:
    col_add, col_list = st.columns([2, 3])
    with col_add:
        with st.form("add_cat", clear_on_submit=True):
            n_en = st.text_input("Category (English)", placeholder="e.g. Vegan Alternatives")
            n_he = st.text_input("Category (Hebrew)", placeholder="למשל: תחליפי טבעונות")
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
            l2.caption(f"~{prior['median_days']:.0f}d typical" + ("" if prior["prior_key"] else " (default)"))
            if l3.button("🗑️", key=f"del_cat_{c['id']}"):
                ok, n = db.delete_category(household_id, c["en"])
                if ok:
                    st.rerun()
                else:
                    st.error(tr("cat_in_use", n=n))

# ==========================================================================
# Tab 4: How it predicts
# ==========================================================================
with tab_curves:
    if pantry_all:
        by_id = {i["id"]: i for i in pantry_all}
        pick = st.selectbox(tr("curve_pick"), list(by_id), key="curve_item",
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
# Sidebar: Quick add from pasted text
# ==========================================================================
parser = get_parser()
st.sidebar.header(tr("quick_add"))
st.sidebar.caption(tr("quick_add_help"))
pasted = st.sidebar.text_area(tr("quick_add_items"), placeholder="Milk\n2 eggs\nלחם", height=140, key="paste_text")
paste_date = st.sidebar.date_input(tr("purchase_date"), value=today, max_value=today, key="paste_date")
if st.sidebar.button(tr("process"), type="primary", disabled=not parser.available,
                     help=None if parser.available else "Set OPENAI_API_KEY to enable"):
    if not pasted.strip():
        st.sidebar.error(tr("paste_first"))
    else:
        loader = st.sidebar.empty()
        loader.markdown(cart_spinner_html(tr("quick_add_working")), unsafe_allow_html=True)
        try:
            parsed = parser.parse(pasted, categories)
            loader.empty()
            st.session_state["staged_items"] = parsed
            st.session_state["staged_date"] = paste_date
            st.rerun()
        except RuntimeError as e:
            loader.empty()
            st.sidebar.error(str(e))

if st.session_state.get("staged_items"):
    st.divider()
    st.subheader(tr("verify_title"))
    staged = pd.DataFrame(st.session_state["staged_items"])
    staged.insert(0, "include", True)
    edited = st.data_editor(
        staged[["include", "name_en", "name_he", "category", "units"]],
        column_config={
            "include": st.column_config.CheckboxColumn(tr("keep"), default=True),
            "name_en": st.column_config.TextColumn(tr("item_name_en")),
            "name_he": st.column_config.TextColumn(tr("item_name_he")),
            "category": st.column_config.SelectboxColumn(tr("category"), options=cat_keys, required=True),
            "units": st.column_config.NumberColumn(tr("units"), min_value=1, step=1, default=1),
        },
        hide_index=True, width="stretch", key="staged_editor",
    )
    c1, c2 = st.columns(2)
    if c1.button(tr("commit"), type="primary"):
        keep = edited[edited["include"]].copy()
        keep["name_en"] = keep["name_en"].fillna("").astype(str).str.strip()
        keep["name_he"] = keep["name_he"].fillna("").astype(str).str.strip()
        keep = keep[(keep["name_en"] != "") | (keep["name_he"] != "")]
        missing = keep[keep["category"].isna() | ~keep["category"].isin(cat_keys)]
        if not missing.empty:
            st.error(tr("need_category", names=", ".join(missing["name_en"].fillna(missing["name_he"]))))
        else:
            records = [{"name_en": r.name_en, "name_he": r.name_he, "category": r.category,
                        "units": int(r.units or 1)} for r in keep.itertuples()]
            svc.record_purchases(household_id, records, st.session_state["staged_date"], display_name)
            st.session_state.pop("staged_items", None)
            st.toast(tr("saved_n", n=len(records)))
            st.rerun()
    if c2.button(tr("discard")):
        st.session_state.pop("staged_items", None)
        st.rerun()
