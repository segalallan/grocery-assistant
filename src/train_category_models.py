import os
import json
import numpy as np
import pandas as pd

try:
    from lifelines import WeibullAFTFitter
    HAS_LIFELINES = True
except ImportError:
    HAS_LIFELINES = False

DATA_OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "data", "category_priors.json")

# Sensible domain anchors for household pantry staples
DEFAULT_CATEGORY_BENCHMARKS = {
    "Milk & Dairy": {"median_days": 10.0, "rho_target": 1.35, "beta_units": 0.15},
    "Eggs": {"median_days": 18.0, "rho_target": 1.40, "beta_units": 0.10},
    "Bakery & Bread": {"median_days": 5.0, "rho_target": 1.45, "beta_units": 0.10},
    "Produce & Fresh Vegetables": {"median_days": 6.0, "rho_target": 1.35, "beta_units": 0.12},
    "Fresh Fruit": {"median_days": 6.0, "rho_target": 1.35, "beta_units": 0.12},
    "Meat & Poultry": {"median_days": 4.0, "rho_target": 1.50, "beta_units": 0.15},
    "Fish & Seafood": {"median_days": 3.0, "rho_target": 1.50, "beta_units": 0.10},
    "Pantry & Canned Goods": {"median_days": 45.0, "rho_target": 1.25, "beta_units": 0.05},
    "Baking Ingredients": {"median_days": 60.0, "rho_target": 1.25, "beta_units": 0.05},
    "Crackers & Snacks": {"median_days": 14.0, "rho_target": 1.30, "beta_units": 0.20},
    "Beverages": {"median_days": 12.0, "rho_target": 1.30, "beta_units": 0.15},
    "Frozen Foods": {"median_days": 45.0, "rho_target": 1.25, "beta_units": 0.05}
}

def fit_category_weibull(df_category: pd.DataFrame, category_name: str) -> dict:
    """
    Fits Weibull AFT to category purchase intervals with correct exponentiation
    and physical wear-out guardrails.
    """
    benchmarks = DEFAULT_CATEGORY_BENCHMARKS.get(
        category_name, {"median_days": 14.0, "rho_target": 1.25, "beta_units": 0.10}
    )

    if not HAS_LIFELINES or df_category is None or len(df_category) < 15:
        # Calibrated parametric prior when category sample size is small
        med = benchmarks["median_days"]
        rho = benchmarks["rho_target"]
        return {
            "lambda_intercept": float(round(np.log(med), 4)),
            "rho": float(round(rho, 2)),
            "median_days": float(med),
            "lambda_beta_units": float(round(benchmarks["beta_units"], 2))
        }

    # Prepare training columns
    # Requires columns: 'interval_days', 'units_bought', and optional 'event' (1 = depleted/repurchased)
    train_df = df_category[['interval_days', 'units_bought']].copy()
    train_df['units_bought'] = train_df['units_bought'].clip(lower=1, upper=5)
    train_df['event'] = 1  # Completed inter-purchase interval

    aft = WeibullAFTFitter()
    aft.fit(train_df, duration_col='interval_days', event_col='event', formula="units_bought")

    # 1. Scale intercept: ln(lambda)
    lambda_intercept = float(aft.params_['lambda_']['Intercept'])
    lambda_beta_units = float(aft.params_['lambda_'].get('units_bought', 0.0))

    # 2. Shape rho: MUST BE EXPONENTIATED FROM LOG-SCALE
    raw_rho_intercept = float(aft.params_['rho_']['Intercept'])
    empirical_rho = float(np.exp(raw_rho_intercept))

    # Enforce minimum aging floor (rho >= 1.25) to avoid Poisson shopping trip flattening
    final_rho = max(benchmarks["rho_target"], empirical_rho)
    median_days = float(np.exp(lambda_intercept))

    return {
        "lambda_intercept": float(round(lambda_intercept, 4)),
        "rho": float(round(final_rho, 2)),
        "median_days": float(round(median_days, 1)),
        "lambda_beta_units": float(round(lambda_beta_units, 4))
    }

def train_and_export_priors(training_csv_path: str = None):
    os.makedirs(os.path.dirname(DATA_OUTPUT_PATH), exist_ok=True)
    priors = {}

    df = None
    if training_csv_path and os.path.exists(training_csv_path):
        df = pd.read_csv(training_csv_path)

    for cat in DEFAULT_CATEGORY_BENCHMARKS.keys():
        cat_df = df[df['category'] == cat] if df is not None and 'category' in df.columns else None
        priors[cat] = fit_category_weibull(cat_df, cat)

    with open(DATA_OUTPUT_PATH, "w") as f:
        json.dump(priors, f, indent=2)

    print(f"✅ Successfully retrained and exported calibrated priors to:\n{DATA_OUTPUT_PATH}")

if __name__ == "__main__":
    train_and_export_priors()