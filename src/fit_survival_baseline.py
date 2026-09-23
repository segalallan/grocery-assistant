import os
import pandas as pd
import matplotlib.pyplot as plt
from lifelines import KaplanMeierFitter, WeibullFitter

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
data_path = os.path.join(DATA_DIR, "survival_staples_sample.csv")

print("Loading survival data...")
df = pd.read_csv(data_path)

print("\n--- Clean Duration Summary (T in Days) ---")
print(df["T"].describe())

# 1. Fit Kaplan-Meier Non-Parametric Estimator
kmf = KaplanMeierFitter()
kmf.fit(durations=df["T"], event_observed=df["event"], label="Aggregate Staples Baseline")
print(f"\nMedian survival time: {kmf.median_survival_time_:.1f} days")

# 2. Fit Parametric Weibull Model
wf = WeibullFitter()
wf.fit(durations=df["T"], event_observed=df["event"])
print(f"Weibull parameters -> Scale (lambda): {wf.lambda_:.2f}, Shape (rho): {wf.rho_:.2f}")

# 3. Plot and save survival decay curve
plt.figure(figsize=(9, 5))
kmf.plot_survival_function(ci_show=True, color="#1f77b4", lw=2)
plt.title("Household Staple Depletion Curve (Kaplan-Meier Baseline)", fontsize=13)
plt.xlabel("Days Since Purchase (t)", fontsize=11)
plt.ylabel("Probability Item Is Still in Pantry: S(t)", fontsize=11)
plt.grid(True, linestyle="--", alpha=0.5)
plt.tight_layout()

plot_path = os.path.join(DATA_DIR, "survival_curve.png")
plt.savefig(plot_path)
print(f"\nSaved survival plot to {plot_path}")
plt.show()