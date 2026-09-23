import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from datetime import datetime, date
import uuid
import pypdfium2 as pdfium
import io

try:
    from streamlit_sortables import sort_items
    HAS_SORTABLES = True
except ImportError:
    HAS_SORTABLES = False

from database import PantryDatabase
from engine import PantryDepletionEngine
# from parser import ReceiptIngestor
from notifications import send_daily_alert

RECEIPT_SCAN_WEEKLY_LIMIT = 10

st.set_page_config(page_title="Smart Pantry Assistant", layout="wide")

init_db()
engine = PantryDepletionEngine()
ingestor = ReceiptIngestor()

# --- LOCALIZATION STRINGS ---
TRANSLATIONS = {
    "en": {
        "title": "🛒 Smart Pantry Assistant",
        "login_sub": "Login or Join a Household",
        "tab_login": "🔑 Log In",
        "tab_signup": "🏠 Sign Up / Join Household",
        "username": "Username",
        "password": "Password",
        "sign_in_btn": "Sign In",
        "choose_user": "Choose Username",
        "your_name": "Your Name",
        "house_options": "Household Options:",
        "create_house": "Create New Household",
        "join_house": "Join Existing Household with Code",
        "house_name": "Household Name",
        "share_code": "Household Share Code",
        "create_acct_btn": "Create Account",
        "logout_btn": "🚪 Log Out",
        "tab_pantry": "📦 Shared Pantry",
        "tab_shopping": "📝 Shopping List",
        "tab_categories": "🏷️ Category Manager",
        "tab_curves": "📈 Survival Curves",
        "sort_pantry_label": "Sort Pantry By:",
        "sort_category": "Category",
        "sort_risk": "Depletion Risk (Highest First)",
        "sort_name": "Item Name (A-Z)",
        "sort_date": "Purchase Date (Newest First)",
        "depletion_risk": "Depletion Risk",
        "household_velocity": "Household Velocity (λ)",
        "delete_btn": "🗑️",
        "delete_tooltip": "Delete from database",
        "manual_add_pantry": "➕ Add Item Manually to Pantry",
        "manual_add_shop": "➕ Add Item to Shopping List",
        "item_name_en": "Item Name (English)",
        "item_name_he": "Item Name (Hebrew / עברית)",
        "units": "Units / Qty",
        "purchase_date": "Purchase Date",
        "save_btn": "Save Item",
        "update_btn": "Update",
        "edit_tooltip": "✏️ Edit item",
        "org_label": "Shopping List Organization:",
        "org_custom": "Custom Order (Drag & Drop)",
        "org_category": "By Category",
        "org_name": "Item Name (A-Z)",
        "bought_btn": "🛒 Bought",
        "sidebar_receipt": "📷 Receipt Ingestion",
        "upload_label": "Upload receipt file (Photo or PDF)",
        "scan_btn": "Scan & Ingest Receipt",
        "empty_pantry": "Pantry is empty. Add items manually or scan a receipt!",
        "empty_list": "Shopping list is clear! 🎉",
        "drag_drop_caption": "↕️ Drag and drop cards below to set your aisle walking path:",
        "install_sortables_warning": "To use drag-and-drop reordering, run: `pip install streamlit-sortables`"
    },
    "he": {
        "title": "🛒 עוזר המזווה החכם",
        "login_sub": "התחברות או הצטרפות למשק בית",
        "tab_login": "🔑 התחברות",
        "tab_signup": "🏠 הרשמה / הצטרפות למשק בית",
        "username": "שם משתמש",
        "password": "סיסמה",
        "sign_in_btn": "התחבר",
        "choose_user": "בחר שם משתמש",
        "your_name": "השם שלך",
        "house_options": "אפשרויות משק בית:",
        "create_house": "צור משק בית חדש",
        "join_house": "הצטרף עם קוד משק בית קיים",
        "house_name": "שם משק הבית",
        "share_code": "קוד שיתוף של משק הבית",
        "create_acct_btn": "צור חשבון",
        "logout_btn": "🚪 התנתק",
        "tab_pantry": "📦 המזווה המשותף",
        "tab_shopping": "📝 רשימת קניות",
        "tab_categories": "🏷️ ניהול קטגוריות",
        "tab_curves": "📈 עקומות הישרדות",
        "sort_pantry_label": "מיין מזווה לפי:",
        "sort_category": "קטגוריה",
        "sort_risk": "סיכון מחסור (מהגבוה לנמוך)",
        "sort_name": "שם מוצר (א-ת)",
        "sort_date": "תאריך קנייה (מהחדש לישן)",
        "depletion_risk": "סיכון שנגמר",
        "household_velocity": "קצב צריכה ביתי (λ)",
        "delete_btn": "🗑️",
        "delete_tooltip": "מחק מהמאגר",
        "manual_add_pantry": "➕ הוסף פריט ידנית למזווה",
        "manual_add_shop": "➕ הוסף פריט לרשימת הקניות",
        "item_name_en": "שם באנגלית",
        "item_name_he": "שם בעברית",
        "units": "כמות",
        "purchase_date": "תאריך קנייה",
        "save_btn": "שמור פריט",
        "update_btn": "עדכן",
        "edit_tooltip": "✏️ ערוך פריט",
        "org_label": "סדר רשימת הקניות:",
        "org_custom": "סדר מותאם (גרירה ושחרור)",
        "org_category": "לפי קטגוריה",
        "org_name": "שם פריט (א-ת)",
        "bought_btn": "🛒 נקנה",
        "sidebar_receipt": "📷 סריקת קבלות",
        "upload_label": "העלאת קובץ קבלה (תמונה או PDF)",
        "scan_btn": "סרוק והזן פריטים",
        "empty_pantry": "המזווה ריק. הוסף פריטים ידנית או סרוק קבלה!",
        "empty_list": "רשימת הקניות ריקה! 🎉",
        "drag_drop_caption": "↕️ גרור ושחרר את הכרטיסיות כדי לקבוע את מסלול ההליכה בסופר:",
        "install_sortables_warning": "כדי להשתמש בגרירה ושחרור, הרץ: `pip install streamlit-sortables`"
    }
}

if "authenticated" not in st.session_state:
    st.session_state["authenticated"] = False
    st.session_state["user_id"] = None
    st.session_state["username"] = None
    st.session_state["display_name"] = None
    st.session_state["household_id"] = None
    st.session_state["household_name"] = None
    st.session_state["household_code"] = None

if "lang" not in st.session_state:
    st.session_state["lang"] = "en"

if "staged_receipt_items" not in st.session_state:
    st.session_state["staged_receipt_items"] = None
if "staged_purchase_date" not in st.session_state:
    st.session_state["staged_purchase_date"] = date.today().strftime("%Y-%m-%d")
if "staged_file_bytes" not in st.session_state:
    st.session_state["staged_file_bytes"] = None
if "staged_filename" not in st.session_state:
    st.session_state["staged_filename"] = ""

selected_lang = st.sidebar.selectbox(
    "🌐 Language / שפה",
    ["English", "עברית"],
    index=0 if st.session_state["lang"] == "en" else 1
)
st.session_state["lang"] = "en" if selected_lang == "English" else "he"
L = TRANSLATIONS[st.session_state["lang"]]
is_he = st.session_state["lang"] == "he"

def render_auth_view():
    st.title(L["title"])
    st.subheader(L["login_sub"])

    tab_login, tab_signup = st.tabs([L["tab_login"], L["tab_signup"]])

    with tab_login:
        with st.form("login_form"):
            username = st.text_input(L["username"]).strip().lower()
            password = st.text_input(L["password"], type="password")
            login_btn = st.form_submit_button(L["sign_in_btn"])

            if login_btn:
                if not username or not password:
                    st.error("Please enter both username and password.")
                else:
                    conn = get_connection()
                    cursor = conn.cursor()
                    cursor.execute("""
                        SELECT u.id, u.username, u.password_hash, u.display_name, u.household_id, h.name as household_name, h.household_code
                        FROM users u
                        JOIN households h ON u.household_id = h.id
                        WHERE u.username = ?
                    """, (username,))
                    user = cursor.fetchone()
                    conn.close()

                    if user and verify_password(password, user["password_hash"]):
                        st.session_state["authenticated"] = True
                        st.session_state["user_id"] = user["id"]
                        st.session_state["username"] = user["username"]
                        st.session_state["display_name"] = user["display_name"] or user["username"]
                        st.session_state["household_id"] = user["household_id"]
                        st.session_state["household_name"] = user["household_name"]
                        st.session_state["household_code"] = user["household_code"]
                        st.success("Welcome back!")
                        st.rerun()
                    else:
                        st.error("Invalid credentials.")

    with tab_signup:
        signup_mode = st.radio(L["house_options"], [L["create_house"], L["join_house"]])
        with st.form("signup_form"):
            new_username = st.text_input(L["choose_user"]).strip().lower()
            new_display = st.text_input(L["your_name"])
            new_password = st.text_input(L["password"], type="password")

            if signup_mode == L["create_house"]:
                house_name = st.text_input(L["house_name"], placeholder="e.g. Herzliya Apartment")
                join_code = None
            else:
                house_name = None
                join_code = st.text_input(L["share_code"]).strip()

            signup_btn = st.form_submit_button(L["create_acct_btn"])

            if signup_btn:
                if not new_username or not new_password or not new_display:
                    st.error("Please fill in all fields.")
                else:
                    conn = get_connection()
                    cursor = conn.cursor()

                    cursor.execute("SELECT id FROM users WHERE username = ?", (new_username,))
                    if cursor.fetchone():
                        st.error("Username already taken.")
                        conn.close()
                    else:
                        if signup_mode == L["create_house"]:
                            new_code = str(uuid.uuid4())[:8].upper()
                            h_name = house_name if house_name else f"{new_display}'s House"
                            cursor.execute("INSERT INTO households (household_code, name) VALUES (?, ?)", (new_code, h_name))
                            household_id = cursor.lastrowid
                            seed_default_categories(household_id, existing_conn=conn)
                        else:
                            cursor.execute("SELECT id FROM households WHERE household_code = ?", (join_code,))
                            house = cursor.fetchone()
                            if not house:
                                st.error("Invalid Household code!")
                                conn.close()
                                return
                            household_id = house["id"]

                        cursor.execute("""
                            INSERT INTO users (username, password_hash, household_id, display_name)
                            VALUES (?, ?, ?, ?)
                        """, (new_username, hash_password(new_password), household_id, new_display))
                        conn.commit()
                        conn.close()
                        st.success("Account created successfully! Please log in above.")

if not st.session_state["authenticated"]:
    render_auth_view()
    st.stop()

# --- APP CONTEXT ---
household_id = st.session_state["household_id"]
display_name = st.session_state["display_name"]
household_name = st.session_state["household_name"]
household_code = st.session_state["household_code"]
today_str = date.today().strftime("%Y-%m-%d")
today = date.today()

head_c1, head_c2 = st.columns([4, 1])
with head_c1:
    st.title(L["title"])
    st.markdown(f"👤 **{display_name}** | 🏠 **{household_name}** (`{household_code}`)")
with head_c2:
    if st.button(L["logout_btn"]):
        st.session_state["authenticated"] = False
        st.rerun()

categories_raw = get_household_categories(household_id)
if not categories_raw:
    seed_default_categories(household_id)
    categories_raw = get_household_categories(household_id)

cat_en_to_he = {c["en"]: c["he"] for c in categories_raw}
cat_display_map = {c["en"]: (c["he"] if is_he else c["en"]) for c in categories_raw}

# Fetch Inventory
conn = get_connection()
cursor = conn.cursor()
cursor.execute(
    "SELECT id, item_name_en, item_name_he, category, units, purchase_date, user_lambda FROM inventory WHERE household_id = ?",
    (household_id,)
)
raw_inventory = cursor.fetchall()
conn.close()

pantry_items = []
low_stock_candidates = []

for row in raw_inventory:
    p_date = datetime.strptime(row["purchase_date"], "%Y-%m-%d").date()
    days_elapsed = (today - p_date).days
    units_qty = max(1, row["units"] if row["units"] else 1)

    prob_depleted = engine.compute_depletion_probability(
        category=row["category"],
        days_elapsed=days_elapsed,
        user_lambda=row["user_lambda"],
        units_qty=units_qty
    )
    name_display = row["item_name_he"] if (is_he and row["item_name_he"]) else row["item_name_en"]
    category_display = cat_display_map.get(row["category"], row["category"])

    item_dict = {
        "id": row["id"],
        "name_en": row["item_name_en"],
        "name_he": row["item_name_he"],
        "display_name": name_display,
        "category": row["category"],
        "category_display": category_display,
        "units": units_qty,
        "purchase_date": row["purchase_date"],
        "days_elapsed": days_elapsed,
        "lambda": row["user_lambda"],
        "prob_depleted": prob_depleted
    }
    pantry_items.append(item_dict)
    if prob_depleted >= 0.65:
        low_stock_candidates.append(item_dict)

# --- DIALOGS ---
@st.dialog("🔔 Morning Restock Alert")
def restock_alert_dialog(candidates, h_id, d_name, today_s):
    st.write("These items have a high probability of being depleted. Select items to restock or extend their lifespan.")
    selected_ids = [item['id'] for item in candidates if st.session_state.get(f"restk_{item['id']}")]
    
    if selected_ids:
        st.markdown(f"**Actions for {len(selected_ids)} selected items:**")
        b1, b2 = st.columns(2)
        
        if b1.button("🛒 Add to List", use_container_width=True, type="primary"):
            conn = get_connection()
            c = conn.cursor()
            c.execute("SELECT COALESCE(MAX(sort_order), 0) AS max_o FROM shopping_list WHERE household_id = ?", (h_id,))
            curr_order = c.fetchone()["max_o"]
            
            for i_id in selected_ids:
                item = next(i for i in candidates if i['id'] == i_id)
                curr_order += 1
                c.execute(
                    "INSERT INTO shopping_list (household_id, item_name_en, item_name_he, category, source, sort_order, added_at, added_by) VALUES (?, ?, ?, ?, 'smart_alert', ?, ?, ?)",
                    (h_id, item["name_en"], item["name_he"], item["category"], curr_order, today_s, d_name)
                )
            conn.commit()
            conn.close()
            for cid in selected_ids:
                st.session_state[f"restk_{cid}"] = False
            st.success("Items added to shopping list!")
            st.rerun()
            
        if b2.button("🕰️ Still Going", use_container_width=True):
            conn = get_connection()
            c = conn.cursor()
            for i_id in selected_ids:
                c.execute(
                    "UPDATE inventory SET purchase_date = ? WHERE id = ? AND household_id = ?",
                    (today_s, i_id, h_id)
                )
            conn.commit()
            conn.close()
            for cid in selected_ids:
                st.session_state[f"restk_{cid}"] = False
            st.toast("Item lifespans successfully extended!")
            st.rerun()
            
        st.markdown("---")

    with st.container(height=350, border=True):
        for item in candidates:
            c_chk, c_name, c_prob = st.columns([0.15, 0.65, 0.2])
            with c_chk:
                st.checkbox(" ", key=f"restk_{item['id']}", label_visibility="collapsed")
            with c_name:
                st.markdown(f"**{item['display_name']}**")
            with c_prob:
                st.markdown(f"🚨 **{int(item['prob_depleted']*100)}%**")

@st.dialog("🔀 Merge Selected Items")
def merge_dialog_ui(selected_ids, items_list, h_id):
    selected_items = [i for i in items_list if i["id"] in selected_ids]
    
    if len(selected_items) < 2:
        st.warning("Please select at least 2 items to merge.")
        return

    st.write("Choose the **'father'** item. All other selected items will be deleted, and their units will be folded into the father.")
    father = st.selectbox(
        "Select Father Item:", 
        options=selected_items, 
        format_func=lambda x: f"{x['display_name']} ({x['units']} units - {x['category_display']})"
    )
    
    if st.button("Confirm Merge", type="primary"):
        children = [i for i in selected_items if i['id'] != father['id']]
        extra_units = sum([c['units'] for c in children])
        child_ids = [c['id'] for c in children]
        
        conn = get_connection()
        c = conn.cursor()
        c.execute("UPDATE inventory SET units = units + ? WHERE id = ?", (extra_units, father['id']))
        placeholders = ",".join("?" * len(child_ids))
        c.execute(f"DELETE FROM inventory WHERE id IN ({placeholders}) AND household_id = ?", (*child_ids, h_id))
        conn.commit()
        conn.close()
        
        for cid in selected_ids:
            if f"chk_{cid}" in st.session_state:
                st.session_state[f"chk_{cid}"] = False
        
        st.success("Items successfully merged!")
        st.rerun()

# --- RESTOCK BANNER ---
if low_stock_candidates:
    if st.button("🔔 Morning Restock Alert: High probability of depletion!", type="primary", use_container_width=True):
        restock_alert_dialog(low_stock_candidates, household_id, display_name, today_str)
    st.markdown("---")

tab_pantry, tab_shopping, tab_categories, tab_curves = st.tabs([
    L["tab_pantry"],
    L["tab_shopping"],
    L["tab_categories"],
    L["tab_curves"]
])

# --- TAB 1: SHARED PANTRY ---
with tab_pantry:
    st.subheader(household_name)

    with st.expander(L["manual_add_pantry"]):
        with st.form("manual_add_pantry_form"):
            col_p1, col_p2, col_p3 = st.columns([2, 2, 1])
            with col_p1:
                p_name_en = st.text_input(L["item_name_en"], placeholder="e.g. 3% Milk")
                p_cat_en = st.selectbox("Category", [c["en"] for c in categories_raw], format_func=lambda x: cat_display_map.get(x, x), key="p_cat_select")
            with col_p2:
                p_name_he = st.text_input(L["item_name_he"], placeholder="למשל: חלב 3%")
                p_date = st.date_input(L["purchase_date"], value=today, key="p_date_picker")
            with col_p3:
                p_qty = st.number_input(L["units"], min_value=1, value=1, step=1)
                st.markdown("<br>", unsafe_allow_html=True)
                p_submit = st.form_submit_button(L["save_btn"])

            if p_submit:
                final_en = p_name_en.strip() or p_name_he.strip()
                final_he = p_name_he.strip() or p_name_en.strip()
                if final_en:
                    ingestor.process_receipt_item(
                        item_name_en=final_en,
                        item_name_he=final_he,
                        category=p_cat_en,
                        purchase_date_str=p_date.strftime("%Y-%m-%d"),
                        household_id=household_id,
                        user_name=display_name,
                        units=int(p_qty)
                    )
                    st.success("Item added to pantry!")
                    st.rerun()

    # --- RECENT PURCHASES (MATH AUDIT & EXPORT) ---
    with st.expander("📊 Recent Purchases & Mathematical Audit (Last 60 Days)"):
        st.caption("Inspect and export all raw parameters, Bayesian shrinkage calculations, and Weibull probabilities to CSV.")
        
        conn = get_connection()
        c = conn.cursor()
        c.execute("""
            SELECT id, item_name_en, item_name_he, category, units, purchase_date, recorded_at, recorded_by
            FROM purchases
            WHERE household_id = ? AND purchase_date >= date(?, '-60 days')
            ORDER BY purchase_date DESC
        """, (household_id, today_str))
        recent_purchases_rows = [dict(r) for r in c.fetchall()]
        
        # Fallback backfill from active inventory if purchases ledger was just initialized
        if not recent_purchases_rows:
            c.execute("""
                SELECT id, item_name_en, item_name_he, category, units, purchase_date, 'inventory' as recorded_at, 'system' as recorded_by
                FROM inventory
                WHERE household_id = ? AND purchase_date >= date(?, '-60 days')
                ORDER BY purchase_date DESC
            """, (household_id, today_str))
            recent_purchases_rows = [dict(r) for r in c.fetchall()]

        audit_records = []
        for p in recent_purchases_rows:
            p_date = datetime.strptime(p["purchase_date"], "%Y-%m-%d").date()
            days_el = (today - p_date).days
            units_q = max(1, p["units"] or 1)
            cat = p["category"]

            params = engine.get_category_parameters(cat)
            rho = params.get("rho", 1.25)
            prior_lambda = np.exp(params.get("lambda_intercept", np.log(14.0)))

            c.execute("SELECT interval_days FROM purchase_history WHERE household_id = ? AND category = ?", (household_id, cat))
            recorded_intervals = [r["interval_days"] for r in c.fetchall()]
            n_intervals = len(recorded_intervals)
            mean_interval = float(np.mean(recorded_intervals)) if recorded_intervals else None

            user_lam = engine.update_user_scale(cat, recorded_intervals) if recorded_intervals else prior_lambda
            scale_lambda = user_lam * units_q
            
            depletion_p = engine.compute_depletion_probability(
                category=cat,
                days_elapsed=days_el,
                user_lambda=user_lam,
                units_qty=units_q
            )

            audit_records.append({
                "Purchase ID": p["id"],
                "Item Name": p["item_name_en"],
                "Category": cat,
                "Units": units_q,
                "Purchase Date": p["purchase_date"],
                "Days Elapsed": days_el,
                "Baseline Prior (λ₀)": round(prior_lambda, 2),
                "Weibull Rho (ρ)": rho,
                "History Count (n)": n_intervals,
                "Mean Interval (x̄)": round(mean_interval, 2) if mean_interval else "N/A",
                "Household Velocity (λ)": round(user_lam, 2),
                "Effective Scale (λ * units)": round(scale_lambda, 2),
                "Depletion Probability": round(depletion_p, 4),
                "Depletion Risk (%)": f"{int(depletion_p * 100)}%"
            })
        conn.close()

        if audit_records:
            audit_df = pd.DataFrame(audit_records)
            st.dataframe(audit_df, use_container_width=True)

            csv_buffer = audit_df.to_csv(index=False).encode("utf-8")
            st.download_button(
                label="📥 Download Recent Purchases CSV (60 Days)",
                data=csv_buffer,
                file_name=f"recent_purchases_audit_{household_code}_{today_str}.csv",
                mime="text/csv",
                key="btn_download_recent_purchases"
            )
        else:
            st.info("No purchases recorded in the last 60 days.")

    if not pantry_items:
        st.info(L["empty_pantry"])
    else:
        # --- SEARCH & FILTER BAR ---
        col_search, col_filter = st.columns([2, 1])
        with col_search:
            search_term = st.text_input("🔍 Search Pantry...", "").strip().lower()
        with col_filter:
            cat_options = ["All Categories"] + [c["en"] for c in categories_raw]
            selected_cat = st.selectbox(
                "🏷️ Filter by Category", 
                options=cat_options, 
                format_func=lambda x: "All Categories" if x == "All Categories" else cat_display_map.get(x, x),
                label_visibility="collapsed"
            )

        if search_term:
            pantry_items = [
                i for i in pantry_items 
                if search_term in str(i["name_en"]).lower() or search_term in str(i["name_he"]).lower()
            ]
            
        if selected_cat != "All Categories":
            pantry_items = [i for i in pantry_items if i["category"] == selected_cat]

        # --- DYNAMIC BULK ACTION BAR ---
        selected_ids = [item["id"] for item in pantry_items if st.session_state.get(f"chk_{item['id']}")]
        
        if selected_ids:
            st.markdown(f"**Bulk Actions ({len(selected_ids)} items selected):**")
            b1, b2, b3, b4, b5 = st.columns(5)
            
            if b1.button("🗑️ Delete", use_container_width=True):
                conn = get_connection()
                c = conn.cursor()
                placeholders = ",".join("?" * len(selected_ids))
                c.execute(f"DELETE FROM inventory WHERE id IN ({placeholders}) AND household_id = ?", (*selected_ids, household_id))
                conn.commit()
                conn.close()
                for cid in selected_ids:
                    st.session_state[f"chk_{cid}"] = False
                st.rerun()
                
            if b2.button("🔀 Merge", use_container_width=True):
                merge_dialog_ui(selected_ids, pantry_items, household_id)
                
            if b3.button("🛒 Add to List", use_container_width=True):
                conn = get_connection()
                c = conn.cursor()
                c.execute("SELECT COALESCE(MAX(sort_order), 0) AS max_o FROM shopping_list WHERE household_id = ?", (household_id,))
                curr_order = c.fetchone()["max_o"]
                
                for i_id in selected_ids:
                    item = next(i for i in pantry_items if i['id'] == i_id)
                    curr_order += 1
                    c.execute(
                        "INSERT INTO shopping_list (household_id, item_name_en, item_name_he, category, source, sort_order, added_at, added_by) VALUES (?, ?, ?, ?, 'manual', ?, ?, ?)",
                        (household_id, item["name_en"], item["name_he"], item["category"], curr_order, today_str, display_name)
                    )
                conn.commit()
                conn.close()
                for cid in selected_ids:
                    st.session_state[f"chk_{cid}"] = False
                st.success(f"Added {len(selected_ids)} items to shopping list!")
                st.rerun()
                
            if b4.button("❌ Ran Out", use_container_width=True):
                for cid in selected_ids:
                    ingestor.mark_item_depleted_manually(cid, household_id, today_str, user_name=display_name)
                    st.session_state[f"chk_{cid}"] = False
                st.rerun()

            if b5.button("🕰️ Still Going", use_container_width=True):
                conn = get_connection()
                c = conn.cursor()
                for cid in selected_ids:
                    c.execute(
                        "UPDATE inventory SET purchase_date = ? WHERE id = ? AND household_id = ?",
                        (today_str, cid, household_id)
                    )
                    st.session_state[f"chk_{cid}"] = False
                conn.commit()
                conn.close()
                st.toast("Item lifespan extended - countdown restarted from today!")
                st.rerun()
                
            st.markdown("---")

        sort_opts = [L["sort_category"], L["sort_risk"], L["sort_name"], L["sort_date"]]
        export_df = pd.DataFrame(pantry_items)[[
            "id", "name_en", "name_he", "category", "units", 
            "purchase_date", "days_elapsed", "prob_depleted", "lambda"
        ]].rename(columns={
            "id": "Item ID",
            "name_en": "Item Name (EN)",
            "name_he": "Item Name (HE)",
            "category": "Category",
            "units": "Units Qty",
            "purchase_date": "Purchase Date",
            "days_elapsed": "Days Since Purchase",
            "prob_depleted": "Depletion Probability",
            "lambda": "Household Velocity (Lambda, Per Unit)"
        })

        excel_buffer = io.BytesIO()
        with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
            export_df.to_excel(writer, sheet_name="Pantry_Inventory", index=False)
        excel_data = excel_buffer.getvalue()

        col_sort, col_export = st.columns([3, 1])
        with col_sort:
            sort_choice = st.selectbox(L["sort_pantry_label"], sort_opts, key="pantry_sort")
        with col_export:
            st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
            st.download_button(
                label="📥 Export to Excel",
                data=excel_data,
                file_name=f"pantry_inventory_{today_str}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="export_pantry_excel"
            )

        if sort_choice == L["sort_category"]:
            pantry_items = sorted(pantry_items, key=lambda x: (x["category_display"].lower(), x["display_name"].lower()))
        elif sort_choice == L["sort_risk"]:
            pantry_items = sorted(pantry_items, key=lambda x: x["prob_depleted"], reverse=True)
        elif sort_choice == L["sort_name"]:
            pantry_items = sorted(pantry_items, key=lambda x: x["display_name"].lower())
        elif sort_choice == L["sort_date"]:
            pantry_items = sorted(pantry_items, key=lambda x: x["purchase_date"], reverse=True)

        for item in pantry_items:
            c_chk, c1, c2, c3, c_edit = st.columns([0.4, 3, 2, 1.5, 0.8])
            
            with c_chk:
                st.markdown("<div style='margin-top: 10px;'></div>", unsafe_allow_html=True)
                st.checkbox(" ", key=f"chk_{item['id']}", label_visibility="collapsed")
                
            with c1:
                qty_badge = f" **(x{item['units']})**" if item['units'] > 1 else ""
                st.markdown(f"**{item['display_name']}**{qty_badge}")
                st.caption(f"🏷️ `{item['category_display']}` | {item['purchase_date']} ({item['days_elapsed']}d)")
                
            with c2:
                prob = item["prob_depleted"]
                st.progress(min(1.0, prob))
                st.caption(f"{L['depletion_risk']}: **{int(prob*100)}%**")
                
            with c3:
                lambda_val = f"{item['lambda']:.1f}d" if item['lambda'] else "14.0d"
                st.caption(f"{L['household_velocity']}: **{lambda_val}**")

            with c_edit:
                with st.popover("✏️"):
                    st.markdown(f"**{L['edit_tooltip']}**")
                    with st.form(f"edit_pantry_{item['id']}"):
                        new_en = st.text_input(L["item_name_en"], value=item["name_en"])
                        new_he = st.text_input(L["item_name_he"], value=item["name_he"])
                        new_qty = st.number_input(L["units"], min_value=1, value=item["units"], step=1)
                        curr_cat_idx = [c["en"] for c in categories_raw].index(item["category"]) if item["category"] in [c["en"] for c in categories_raw] else 0
                        new_cat = st.selectbox("Category", [c["en"] for c in categories_raw], index=curr_cat_idx, format_func=lambda x: cat_display_map.get(x, x))
                        if st.form_submit_button(L["update_btn"]):
                            conn = get_connection()
                            c = conn.cursor()
                            c.execute(
                                "UPDATE inventory SET item_name_en = ?, item_name_he = ?, category = ?, units = ? WHERE id = ? AND household_id = ?",
                                (new_en.strip(), new_he.strip(), new_cat, int(new_qty), item["id"], household_id)
                            )
                            conn.commit()
                            conn.close()
                            st.rerun()

# --- TAB 2: SHOPPING LIST ---
with tab_shopping:
    st.subheader(L["tab_shopping"])

    with st.expander(L["manual_add_shop"], expanded=False):
        with st.form("manual_add_shop_form"):
            col_s1, col_s2 = st.columns(2)
            with col_s1:
                s_name_en = st.text_input(L["item_name_en"], placeholder="e.g. Sourdough Bread")
                s_cat_en = st.selectbox("Category", [c["en"] for c in categories_raw], format_func=lambda x: cat_display_map.get(x, x), key="s_cat_select")
            with col_s2:
                s_name_he = st.text_input(L["item_name_he"], placeholder="למשל: לחם מחמצת")

            s_submit = st.form_submit_button(L["save_btn"])
            if s_submit:
                final_en = s_name_en.strip() or s_name_he.strip()
                final_he = s_name_he.strip() or s_name_en.strip()
                if final_en:
                    conn = get_connection()
                    c = conn.cursor()
                    c.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 AS next_o FROM shopping_list WHERE household_id = ?", (household_id,))
                    next_o = c.fetchone()["next_o"]
                    c.execute(
                        "INSERT INTO shopping_list (household_id, item_name_en, item_name_he, category, source, sort_order, added_at, added_by) VALUES (?, ?, ?, ?, 'manual', ?, ?, ?)",
                        (household_id, final_en, final_he, s_cat_en, next_o, today_str, display_name)
                    )
                    conn.commit()
                    conn.close()
                    st.rerun()

    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT id, item_name_en, item_name_he, category, source, sort_order, added_at, added_by FROM shopping_list WHERE household_id = ?", (household_id,))
    shopping_rows = [dict(r) for r in c.fetchall()]
    conn.close()

    if not shopping_rows:
        st.write(L["empty_list"])
    else:
        for r in shopping_rows:
            r["display_name"] = r["item_name_he"] if (is_he and r["item_name_he"]) else r["item_name_en"]
            r["category_display"] = cat_display_map.get(r["category"], r["category"])

        col_s_search, col_s_filter = st.columns([2, 1])
        with col_s_search:
            shop_search_term = st.text_input("🔍 Search Shopping List...", "").strip().lower()
        with col_s_filter:
            shop_cat_options = ["All Categories"] + [c["en"] for c in categories_raw]
            shop_selected_cat = st.selectbox(
                "🏷️ Filter by Category", 
                options=shop_cat_options, 
                format_func=lambda x: "All Categories" if x == "All Categories" else cat_display_map.get(x, x),
                key="shop_cat_filter",
                label_visibility="collapsed"
            )

        if shop_search_term:
            shopping_rows = [
                r for r in shopping_rows 
                if shop_search_term in str(r["item_name_en"]).lower() or shop_search_term in str(r["item_name_he"]).lower()
            ]
            
        if shop_selected_cat != "All Categories":
            shopping_rows = [r for r in shopping_rows if r["category"] == shop_selected_cat]

        shop_sort = st.radio(
            L["org_label"],
            [L["org_custom"], L["org_category"], L["org_name"]],
            horizontal=True
        )

        if shop_sort == L["org_custom"]:
            shopping_rows = sorted(shopping_rows, key=lambda x: x["sort_order"])

            if HAS_SORTABLES:
                st.caption(L["drag_drop_caption"])
                id_to_label = {r["id"]: f"📌 {r['display_name']} ({r['category_display']}) #{r['id']}" for r in shopping_rows}
                label_to_id = {v: k for k, v in id_to_label.items()}
                current_labels = [id_to_label[r["id"]] for r in shopping_rows]

                reordered = sort_items(current_labels, direction="vertical", key=f"dnd_{household_id}")
                if reordered and reordered != current_labels:
                    conn = get_connection()
                    c = conn.cursor()
                    for new_idx, lbl in enumerate(reordered):
                        item_id = label_to_id[lbl]
                        c.execute("UPDATE shopping_list SET sort_order = ? WHERE id = ? AND household_id = ?", (new_idx, item_id, household_id))
                    conn.commit()
                    conn.close()
                    st.rerun()
            else:
                st.warning(L["install_sortables_warning"])

        elif shop_sort == L["org_category"]:
            shopping_rows = sorted(shopping_rows, key=lambda x: (x["category_display"].lower(), x["display_name"].lower()))
        elif shop_sort == L["org_name"]:
            shopping_rows = sorted(shopping_rows, key=lambda x: x["display_name"].lower())

        st.markdown("---")

        for idx, row in enumerate(shopping_rows):
            sc1, sc2, sc3, sc4, sc5 = st.columns([4, 2, 0.6, 0.6, 1.2])

            with sc1:
                added_by = f" *(by {row['added_by']})*" if row['added_by'] else ""
                st.markdown(f"**{row['display_name']}**{added_by}")
            with sc2:
                st.caption(f"🏷️ {row['category_display']}")

            with sc3:
                with st.popover("✏️"):
                    st.markdown(f"**{L['edit_tooltip']}**")
                    with st.form(f"edit_shop_{row['id']}"):
                        new_s_en = st.text_input(L["item_name_en"], value=row["item_name_en"])
                        new_s_he = st.text_input(L["item_name_he"], value=row["item_name_he"])
                        curr_cat_idx = [c["en"] for c in categories_raw].index(row["category"]) if row["category"] in [c["en"] for c in categories_raw] else 0
                        new_s_cat = st.selectbox("Category", [c["en"] for c in categories_raw], index=curr_cat_idx, format_func=lambda x: cat_display_map.get(x, x))
                        if st.form_submit_button(L["update_btn"]):
                            conn = get_connection()
                            c = conn.cursor()
                            c.execute(
                                "UPDATE shopping_list SET item_name_en = ?, item_name_he = ?, category = ? WHERE id = ? AND household_id = ?",
                                (new_s_en.strip(), new_s_he.strip(), new_s_cat, row["id"], household_id)
                            )
                            conn.commit()
                            conn.close()
                            st.rerun()

            with sc4:
                if st.button(L["delete_btn"], key=f"del_shop_{row['id']}", help=L["delete_tooltip"]):
                    conn = get_connection()
                    c = conn.cursor()
                    c.execute("DELETE FROM shopping_list WHERE id = ? AND household_id = ?", (row["id"], household_id))
                    conn.commit()
                    conn.close()
                    st.toast(f"Deleted {row['display_name']} from shopping list.")
                    st.rerun()

            with sc5:
                if st.button(L["bought_btn"], key=f"bought_{row['id']}"):
                    ingestor.process_receipt_item(
                        item_name_en=row["item_name_en"],
                        item_name_he=row["item_name_he"],
                        category=row["category"],
                        purchase_date_str=today_str,
                        household_id=household_id,
                        user_name=display_name,
                        units=1
                    )
                    conn = get_connection()
                    c = conn.cursor()
                    c.execute("DELETE FROM shopping_list WHERE id = ? AND household_id = ?", (row["id"], household_id))
                    conn.commit()
                    conn.close()
                    st.success(f"Restocked {row['display_name']}!")
                    st.rerun()

# --- TAB 3: CATEGORIES ---
with tab_categories:
    st.subheader(L["tab_categories"])
    col_add, col_list = st.columns([2, 3])

    with col_add:
        with st.form("add_cat_form"):
            new_en = st.text_input("Category (English)", placeholder="e.g. Vegan Alternatives")
            new_he = st.text_input("Category (Hebrew / עברית)", placeholder="למשל: תחליפי טבעונות")
            submit = st.form_submit_button("Add / הוסף")
            if submit and new_en.strip():
                conn = get_connection()
                c = conn.cursor()
                try:
                    c.execute("INSERT INTO household_categories (household_id, name_en, name_he) VALUES (?, ?, ?)", 
                              (household_id, new_en.strip(), new_he.strip() or new_en.strip()))
                    conn.commit()
                    conn.close()
                    st.rerun()
                except Exception:
                    conn.close()

    with col_list:
        for cat in categories_raw:
            c1, c2 = st.columns([4, 1])
            with c1:
                st.markdown(f"• **{cat['en']}** / {cat['he']}")
            with c2:
                if st.button("🗑️", key=f"del_cat_{cat['en']}"):
                    conn = get_connection()
                    c = conn.cursor()
                    c.execute("DELETE FROM household_categories WHERE household_id = ? AND name_en = ?", (household_id, cat["en"]))
                    conn.commit()
                    conn.close()
                    st.rerun()

# --- TAB 4: SURVIVAL CURVES ---
with tab_curves:
    st.subheader(L["tab_curves"])
    if pantry_items:
        names = [i["display_name"] for i in pantry_items]
        selected_name = st.selectbox("Inspect staple curve:", names)
        selected_item = next(i for i in pantry_items if i["display_name"] == selected_name)

        params = engine.get_category_parameters(selected_item["category"])
        prior_lambda = np.exp(params.get("lambda_intercept", np.log(14.0)))
        shape_rho = params.get("rho", 1.25)
        per_unit_lambda = selected_item["lambda"] if selected_item["lambda"] else prior_lambda

        units_qty = max(1, selected_item["units"] or 1)
        prior_scale = prior_lambda * units_qty
        user_scale = per_unit_lambda * units_qty

        t_vals = np.linspace(0, max(45, int(user_scale * 2.2)), 100)
        s_prior = np.exp(- (t_vals / prior_scale) ** shape_rho)
        s_user = np.exp(- (t_vals / user_scale) ** shape_rho)

        fig = go.Figure()
        fig.add_trace(go.Scatter(x=t_vals, y=s_prior, mode="lines", name="Population Baseline", line=dict(dash="dash", color="gray")))
        fig.add_trace(go.Scatter(x=t_vals, y=s_user, mode="lines", name=f"{household_name} Model", line=dict(color="#2ca02c", width=3)))

        curr_t = selected_item["days_elapsed"]
        curr_s = np.exp(- (curr_t / user_scale) ** shape_rho)
        fig.add_trace(go.Scatter(x=[curr_t], y=[curr_s], mode="markers+text", name="Today", marker=dict(size=12, color="red"), text=[f"Day {int(curr_t)}"]))

        fig.update_layout(xaxis_title="Days Since Purchase", yaxis_title="Probability S(t)", template="plotly_white")
        st.plotly_chart(fig, use_container_width=True)

# --- SIDEBAR RECEIPT UPLOAD & STAGED CONFIRMATION ---
st.sidebar.header(L["sidebar_receipt"])

receipt_mode = st.sidebar.radio("Ingestion Method:", [
    "📷 Upload Receipt (PDF / Photo)", 
    "📋 Quick Paste List"
])

if receipt_mode == "📷 Upload Receipt (PDF / Photo)":
    with st.sidebar.expander(L["upload_label"], expanded=True):
        scans_used = count_receipt_scans_last_7_days(st.session_state["user_id"])
        scans_left = max(0, RECEIPT_SCAN_WEEKLY_LIMIT - scans_used)
        st.caption(f"📊 {scans_left}/{RECEIPT_SCAN_WEEKLY_LIMIT} receipt scans left this week")

        uploaded_file = st.file_uploader(L["upload_label"], type=["png", "jpg", "jpeg", "pdf"], key="receipt_uploader")
        ocr_date = st.date_input(L["purchase_date"], value=today, key="ocr_date_input")

        if st.button(L["scan_btn"], type="primary"):
            if uploaded_file is None:
                st.error("Select a file first.")
            elif scans_left <= 0:
                st.error(
                    f"You've used all {RECEIPT_SCAN_WEEKLY_LIMIT} receipt scans available this week. "
                    "This limit resets on a rolling 7-day basis - try again later."
                )
            else:
                record_receipt_scan(st.session_state["user_id"], household_id)

                with st.status("🧾 Initializing OCR Pipeline...", expanded=True) as status:
                    def update_status(msg):
                        status.write(msg)

                    file_bytes = uploaded_file.read()
                    result = ingestor.parse_preview(
                        file_bytes=file_bytes,
                        filename=uploaded_file.name,
                        household_id=household_id,
                        status_callback=update_status
                    )
                    raw_items = result["items"]
                    quality_warnings = result["quality_warnings"]

                    if raw_items:
                        for idx, it in enumerate(raw_items):
                            it["include"] = True
                            it["idx"] = idx
                            if "units" not in it:
                                it["units"] = 1
                                
                        st.session_state["staged_receipt_items"] = raw_items
                        st.session_state["staged_purchase_date"] = ocr_date.strftime("%Y-%m-%d")
                        st.session_state["staged_file_bytes"] = file_bytes
                        st.session_state["staged_filename"] = uploaded_file.name
                        
                        status.update(label="✅ Ingestion Complete!", state="complete", expanded=False)
                        if quality_warnings:
                            st.warning("Some pages were too blurry/dark to read and were skipped - see details above. Consider re-scanning just those pages.")
                        st.rerun()
                    elif quality_warnings:
                        status.update(label="⚠️ Image too unclear to read", state="error", expanded=True)
                        st.warning("This photo is too blurry, dark, or low-resolution to read reliably. Please retake it in better lighting, hold the phone steadier, and make sure the receipt fills the frame, then re-upload.")
                    else:
                        status.update(label="⚠️ No items detected", state="error", expanded=True)
                        st.warning("No food items detected.")

else:
    with st.sidebar.expander("Paste Grocery Items", expanded=True):
        pasted_text = st.text_area("Paste items (one per line):", placeholder="חלב 3%\nביצים 18 יח'\nנקניקיות עוף\nצ'יפס קפוא 2 ק\"ג\nפיתות", height=140)
        paste_date = st.date_input("Purchase Date", value=today, key="paste_date_input")

        if st.button("⚡ Ingest Pasted Items", type="primary"):
            if pasted_text.strip():
                with st.spinner("Structuring items with GPT-4o..."):
                    raw_items = ingestor.parse_raw_text(pasted_text, household_id)
                    if raw_items:
                        for idx, it in enumerate(raw_items):
                            it["include"] = True
                            it["idx"] = idx
                            if "units" not in it:
                                it["units"] = 1
                        st.session_state["staged_receipt_items"] = raw_items
                        st.session_state["staged_purchase_date"] = paste_date.strftime("%Y-%m-%d")
                        st.session_state["staged_file_bytes"] = None
                        st.session_state["staged_filename"] = ""
                        st.rerun()
            else:
                st.error("Paste some items first.")

# --- SIDE-BY-SIDE VERIFICATION WORKSPACE ---
if st.session_state["staged_receipt_items"]:
    st.markdown("---")
    st.subheader("🧾 Receipt Review & Verification Workspace")
    st.caption("Verify extracted items and quantities before committing them to the household inventory.")

    col_view_doc, col_view_table = st.columns([1, 1])

    with col_view_doc:
        st.markdown("**Original Receipt Document:**")
        file_bytes = st.session_state.get("staged_file_bytes")
        filename = st.session_state.get("staged_filename", "").lower()

        if file_bytes:
            if filename.endswith(".pdf"):
                try:
                    pdf = pdfium.PdfDocument(file_bytes)
                    for p_idx, page in enumerate(pdf):
                        st.image(
                            page.render(scale=1.5).to_pil(),
                            caption=f"Receipt Page {p_idx + 1}",
                            use_container_width=True
                        )
                except Exception as e:
                    st.error(f"Error rendering PDF preview: {e}")
            else:
                st.image(file_bytes, caption="Uploaded Image", use_container_width=True)
        else:
            st.info("Pasted items mode (no document file uploaded).")

    with col_view_table:
        st.markdown("**Detected Grocery Items:**")
        staged = st.session_state["staged_receipt_items"]
        edited_df = st.data_editor(
            pd.DataFrame(staged)[["include", "item_name_en", "item_name_he", "category", "units"]],
            column_config={
                "include": st.column_config.CheckboxColumn("Keep?", default=True),
                "item_name_en": st.column_config.TextColumn("English Name"),
                "item_name_he": st.column_config.TextColumn("Hebrew Name"),
                "category": st.column_config.SelectboxColumn("Category", options=[c["en"] for c in categories_raw]),
                "units": st.column_config.NumberColumn("Qty", min_value=1, step=1, default=1)
            },
            hide_index=True,
            use_container_width=True,
            key="staged_editor_sidebyside"
        )

        btn_c1, btn_c2 = st.columns([1, 1])
        with btn_c1:
            if st.button("✅ Commit Items to Pantry", type="primary"):
                conn = get_connection()
                try:
                    for _, row in edited_df.iterrows():
                        if row["include"]:
                            ingestor.process_receipt_item(
                                item_name_en=row["item_name_en"],
                                item_name_he=row["item_name_he"],
                                category=row["category"],
                                purchase_date_str=st.session_state["staged_purchase_date"],
                                household_id=household_id,
                                user_name=display_name,
                                units=int(row["units"]),
                                conn=conn
                            )
                    conn.commit()
                finally:
                    conn.close()
                st.session_state["staged_receipt_items"] = None
                st.session_state["staged_file_bytes"] = None
                st.session_state["staged_filename"] = ""
                st.success("All verified items added to pantry!")
                st.rerun()

        with btn_c2:
            if st.button("❌ Discard Scan"):
                st.session_state["staged_receipt_items"] = None
                st.session_state["staged_file_bytes"] = None
                st.session_state["staged_filename"] = ""
                st.rerun()
    st.markdown("---")
    db = PantryDatabase()

# ==========================================
# 1. THE HIDDEN WEBHOOK ENDPOINT (RUN FIRST)
# ==========================================
if st.query_params.get("trigger_daily_alerts") == "TRUE":
    secret_key = st.query_params.get("secret")
    if secret_key == st.secrets.get("CRON_SECRET"):
        st.write("Authorized: Running daily alerts...")
        # Note: In a full production app, you would query db for all households, 
        # run engine.compute_depletion_probability(), filter items > 85%, and email them.
        st.success("Daily alerts triggered and sent.")
        st.stop() # Prevents Streamlit from rendering the UI
    else:
        st.error("Unauthorized webhook call.")
        st.stop()


# ==========================================
# 2. DIALOGS & OVERLAYS (Must be defined top-level)
# ==========================================
@st.dialog("Missing Account Details")
def email_intercept_dialog(user_id):
    st.warning("Welcome back! Before we continue, we're adding daily restock alerts. Please link a valid email address to your account.")
    new_email = st.text_input("Email Address")
    if st.button("Save Email"):
        if "@" in new_email and "." in new_email:
            db.update_user_email(user_id, new_email)
            st.session_state.current_user['email'] = new_email
            st.rerun()
        else:
            st.error("Please enter a valid email format.")

@st.dialog("Review Ingested Items", width="large")
def confirmation_dialog():
    st.write("Please review the items extracted from your input:")
    
    # Render the editable table
    edited_data = st.data_editor(st.session_state.pending_items, use_container_width=True) 
    
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Confirm & Add to Pantry", use_container_width=True, type="primary"):
            # TODO: Save edited_data rows into your database inventory table here
            st.session_state.pending_items = None
            st.session_state.show_confirm = False
            st.success("Items successfully added!")
            st.rerun()
    with col2:
        if st.button("Discard", use_container_width=True):
            st.session_state.pending_items = None
            st.session_state.show_confirm = False
            st.rerun()


# ==========================================
# 3. MAIN APP LOGIC
# ==========================================

# Initialize session state for login
if 'logged_in' not in st.session_state:
    st.session_state.logged_in = False

if not st.session_state.logged_in:
    st.title("Smart Pantry Login")
    tab1, tab2 = st.tabs(["Login", "Sign Up"])
    
    with tab1:
        log_user = st.text_input("Username", key="log_user")
        log_pwd = st.text_input("Password", type="password", key="log_pwd")
        if st.button("Login"):
            user_data = db.verify_user(log_user, log_pwd)
            if user_data:
                st.session_state.logged_in = True
                st.session_state.current_user = user_data
                st.rerun()
            else:
                st.error("Invalid credentials")

    with tab2:
        st.write("Create a new household.")
        new_user = st.text_input("Username")
        new_pwd = st.text_input("Password", type="password")
        new_hh = st.text_input("Household Name (e.g. 'Smith Family Pantry')")
        new_email = st.text_input("Email Address (Required for alerts)")
        
        if st.button("Sign Up"):
            if new_user and new_pwd and new_hh and "@" in new_email:
                if db.create_user(new_user, new_pwd, new_hh, new_email):
                    st.success("Account created! You can now login.")
                else:
                    st.error("Username already exists.")
            else:
                st.error("Please fill all fields with a valid email.")
else:
    # --- EMAIL INTERCEPT CHECK ---
    # If a legacy user logged in and has a NULL email, block the app.
    if not st.session_state.current_user.get('email'):
        email_intercept_dialog(st.session_state.current_user['id'])
        st.stop() 

    # --- MAIN DASHBOARD ---
    st.title(f"Dashboard: {st.session_state.current_user.get('username')}")
    st.write("Welcome to your Smart Pantry!")

    st.divider()

    st.subheader("Add Groceries")
    
    # OCR Upload Hidden for V1 Launch
    # st.info("Camera & Receipt Scanning is currently offline for maintenance.")
    # st.file_uploader("Upload Receipt Image", type=["jpg", "png", "pdf"])

    # Setup session state for the quick paste box so it can wipe itself clean
    if "quick_paste" not in st.session_state:
        st.session_state.quick_paste = ""
    if "show_confirm" not in st.session_state:
        st.session_state.show_confirm = False

    def process_quick_paste():
        text_input = st.session_state.quick_paste
        if text_input.strip():
            # In a real app, call your parser here. E.g.:
            # st.session_state.pending_items = parse_text(text_input)
            
            # Dummy data for demonstration:
            st.session_state.pending_items = [{"Item": "Whole Milk", "Qty": 1}, {"Item": "Bread", "Qty": 2}]
            
            # 1. Instantly clear the text box residue!
            st.session_state.quick_paste = "" 
            # 2. Trigger the modal dialog overlay
            st.session_state.show_confirm = True

    st.text_area("Paste receipt text here:", key="quick_paste")
    st.button("Ingest Text", on_click=process_quick_paste)

    # Pop the dialog if active
    if st.session_state.show_confirm:
        confirmation_dialog()

    if st.button("Logout"):
        st.session_state.logged_in = False
        st.session_state.current_user = None
        st.rerun()