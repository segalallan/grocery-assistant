"""Single source of truth for paths, thresholds and secrets lookup."""
import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SRC_DIR = Path(__file__).resolve().parent          # .../Grocery Assistant/src
PROJECT_ROOT = SRC_DIR.parent                       # .../Grocery Assistant
DATA_DIR = PROJECT_ROOT / "data"
PRIORS_FILE = DATA_DIR / "category_priors.json"
# Repo root, same place the old code created it when run from the project folder.
DB_PATH = os.environ.get("PANTRY_DB_PATH", str(PROJECT_ROOT / "pantry_v1.db"))

# One threshold for the UI banner, the email job and everything else.
RESTOCK_THRESHOLD = 0.65

# "Today" means today in Israel, not on the (UTC) server.
APP_TZ = ZoneInfo(os.environ.get("PANTRY_TZ", "Asia/Jerusalem"))

DATE_FMT = "%Y-%m-%d"

DEFAULT_CATEGORIES = [
    ("Dairy & Eggs", "מוצרי חלב וביצים"),
    ("Produce", "פירות וירקות"),
    ("Meat & Poultry", "בשר ועוף"),
    ("Pantry Staples", "מזווה בסיסי"),
    ("Frozen Foods", "קפואים"),
    ("Beverages", "משקאות"),
    ("Snacks", "חטיפים"),
    ("Cleaning", "חומרי ניקוי"),
]


def today() -> date:
    return datetime.now(APP_TZ).date()


def today_str() -> str:
    return today().strftime(DATE_FMT)


def get_secret(*names: str):
    """Look up a secret by any of several names: env vars first, then st.secrets.

    Env-first means headless jobs (cron / GitHub Actions) work without Streamlit.
    """
    for name in names:
        val = os.environ.get(name)
        if val:
            return val
    try:
        import streamlit as st
        for name in names:
            val = st.secrets.get(name)
            if val:
                return val
    except Exception:
        pass
    return None
