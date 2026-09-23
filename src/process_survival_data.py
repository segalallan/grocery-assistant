import os
import pandas as pd
import numpy as np

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

print("Loading metadata...")
products = pd.read_csv(os.path.join(DATA_DIR, "products.csv"))
aisles = pd.read_csv(os.path.join(DATA_DIR, "aisles.csv"))
departments = pd.read_csv(os.path.join(DATA_DIR, "departments.csv"))

product_meta = products.merge(aisles, on="aisle_id").merge(departments, on="department_id")

target_aisles = [
    "baking ingredients",
    "flour bakers ingredients",
    "milk",
    "eggs"
]
staple_products = product_meta[
    product_meta["aisle"].str.lower().isin(target_aisles) | 
    product_meta["product_name"].str.lower().str.contains("flour")
][["product_id", "product_name", "aisle", "department"]]

staple_ids = set(staple_products["product_id"])
print(f"Tracking {len(staple_ids)} staple products.")

print("Loading orders...")
orders = pd.read_csv(os.path.join(DATA_DIR, "orders.csv"))
orders = orders[orders["eval_set"] == "prior"].sort_values(["user_id", "order_number"])

print("Loading order items...")
chunks = []
for chunk in pd.read_csv(
    os.path.join(DATA_DIR, "order_products__prior.csv"), 
    usecols=["order_id", "product_id"],
    chunksize=500_000
):
    filtered = chunk[chunk["product_id"].isin(staple_ids)]
    chunks.append(filtered)

order_items = pd.concat(chunks, ignore_index=True)

# 1. Merge and establish user day timeline
orders["days_since_prior_order"] = orders["days_since_prior_order"].fillna(0)
orders["user_timeline_days"] = orders.groupby("user_id")["days_since_prior_order"].cumsum()

df = order_items.merge(orders[["order_id", "user_id", "user_timeline_days"]], on="order_id")

# 2. Collapse same-day repeat purchases into a single trip record
# Each occurrence on the same day counts toward total units purchased
print("Consolidating same-day purchases into daily totals...")
daily_purchases = (
    df.groupby(["user_id", "product_id", "user_timeline_days"])
    .size()
    .reset_index(name="units_bought")
)

# 3. Sort by timeline and compute real elapsed days (T)
daily_purchases = daily_purchases.sort_values(["user_id", "product_id", "user_timeline_days"])

daily_purchases["prev_timeline_days"] = daily_purchases.groupby(["user_id", "product_id"])["user_timeline_days"].shift(1)
daily_purchases["duration_days"] = daily_purchases["user_timeline_days"] - daily_purchases["prev_timeline_days"]

# Keep valid duration intervals (E = 1)
df_events = daily_purchases.dropna(subset=["duration_days"]).copy()
df_events["event"] = 1
df_events["T"] = df_events["duration_days"]

# All durations between distinct days will now be strictly positive
print(f"Total valid depletion cycles: {len(df_events):,}")
print(f"Minimum T: {df_events['T'].min()} days | Maximum T: {df_events['T'].max()} days")

output_path = os.path.join(DATA_DIR, "survival_staples_sample.csv")
df_events[["user_id", "product_id", "units_bought", "T", "event"]].to_csv(output_path, index=False)
print(f"Saved merged dataset to {output_path}")