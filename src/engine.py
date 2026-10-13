"""Depletion model. Pure math, no database or Streamlit.

Conventions (used everywhere, including the UI):
  * Consumption time per UNIT follows Weibull(scale=λ, shape=ρ).
  * A stock row with `units` packages has scale λ * units (linear in units).
  * Learned values (`user_lambda`) are per-unit SCALES, not means or medians.
  * Category priors are stored as an intuitive per-unit MEDIAN and converted to a scale.
"""
import json
import logging
import math

from config import PRIORS_FILE

log = logging.getLogger(__name__)

MIN_RHO = 1.1
FALLBACK = {"median_days": 14.0, "rho": 1.25}
CATEGORY_PSEUDO_N = 3.0   # how many observations the population prior is "worth"
ITEM_PSEUDO_N = 2.0       # how many observations the household-category estimate is "worth"

# Custom category names are matched to a prior by keyword (checked in order).
CATEGORY_ALIASES = [
    ("Dairy & Eggs", ["dairy", "milk", "cheese", "egg", "yogurt", "חלב", "ביצים", "גבינ"]),
    ("Bakery & Bread", ["bread", "bakery", "baked", "לחם", "מאפ"]),
    ("Produce", ["produce", "fruit", "vegetable", "veg", "פירות", "ירקות"]),
    ("Meat & Poultry", ["meat", "poultry", "chicken", "fish", "seafood", "בשר", "עוף", "דג"]),
    ("Frozen Foods", ["frozen", "קפוא"]),
    ("Beverages", ["beverage", "drink", "juice", "coffee", "tea", "water", "משקא", "קפה"]),
    ("Snacks", ["snack", "sweet", "candy", "chocolate", "חטיפ", "ממתק"]),
    ("Cleaning", ["clean", "detergent", "paper", "toilet", "soap", "ניקוי", "נייר"]),
    ("Pantry Staples", ["pantry", "staple", "canned", "baking", "rice", "pasta", "grain", "spice", "מזווה", "שימורים"]),
]


def mean_to_scale(mean: float, rho: float) -> float:
    return mean / math.gamma(1.0 + 1.0 / rho)


def median_to_scale(median: float, rho: float) -> float:
    return median / (math.log(2.0) ** (1.0 / rho))


def survival(t: float, scale: float, rho: float) -> float:
    t = max(0.0, float(t))
    return math.exp(-((t / scale) ** rho))


class PantryDepletionEngine:
    def __init__(self, priors_path=PRIORS_FILE):
        self.priors = self._load_priors(priors_path)
        self._lower = {k.lower(): k for k in self.priors}

    @staticmethod
    def _load_priors(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            log.warning("Priors file not found at %s; every category will use the %s-day fallback.",
                        path, FALLBACK["median_days"])
            return {}

    # ---------- priors ----------
    def resolve_category(self, category: str | None) -> str | None:
        """Which priors entry a household category maps to (exact name first, then keywords)."""
        if not category:
            return None
        cat = category.strip().lower()
        if cat in self._lower:
            return self._lower[cat]
        for target, keywords in CATEGORY_ALIASES:
            if target in self.priors and any(k in cat for k in keywords):
                return target
        return None

    def category_params(self, category: str | None) -> dict:
        key = self.resolve_category(category)
        params = dict(self.priors.get(key, FALLBACK)) if key else dict(FALLBACK)
        params["rho"] = max(MIN_RHO, float(params.get("rho", FALLBACK["rho"])))
        params["median_days"] = float(params.get("median_days", FALLBACK["median_days"]))
        params["prior_key"] = key
        return params

    def rho(self, category: str | None) -> float:
        return self.category_params(category)["rho"]

    def prior_scale(self, category: str | None) -> float:
        p = self.category_params(category)
        return median_to_scale(p["median_days"], p["rho"])

    # ---------- learning ----------
    def estimate_item_scale(self, category: str | None, item_intervals: list[float],
                            category_intervals: list[float]) -> float:
        """Per-unit scale for one item, shrunk item → household category → population prior.

        `item_intervals` and `category_intervals` are observed per-unit consumption durations.
        """
        rho = self.rho(category)
        cat_scale = self.prior_scale(category)

        if category_intervals:
            n = len(category_intervals)
            obs = mean_to_scale(sum(category_intervals) / n, rho)
            cat_scale = (CATEGORY_PSEUDO_N * cat_scale + n * obs) / (CATEGORY_PSEUDO_N + n)

        if not item_intervals:
            return cat_scale
        n = len(item_intervals)
        obs = mean_to_scale(sum(item_intervals) / n, rho)
        return (ITEM_PSEUDO_N * cat_scale + n * obs) / (ITEM_PSEUDO_N + n)

    # ---------- prediction ----------
    def effective_scale(self, category: str | None, per_unit_scale: float | None, units: int) -> float:
        base = per_unit_scale if per_unit_scale and per_unit_scale > 0 else self.prior_scale(category)
        return float(base) * max(1, int(units or 1))

    def depletion_probability(self, category: str | None, days_elapsed: float,
                              per_unit_scale: float | None = None, units: int = 1,
                              days_confirmed: float | None = None) -> float:
        """P(item is used up by now).

        If someone confirmed the item was still there `days_confirmed` days after purchase,
        this is conditional: 1 - S(t) / S(c).
        """
        t = max(0.0, float(days_elapsed))
        scale = self.effective_scale(category, per_unit_scale, units)
        rho = self.rho(category)
        s_t = survival(t, scale, rho)

        if days_confirmed is not None and days_confirmed > 0:
            c = min(float(days_confirmed), t)
            s_c = survival(c, scale, rho)
            if s_c <= 1e-12:
                return 1.0 if t > c else 0.0
            return min(1.0, max(0.0, 1.0 - s_t / s_c))

        return min(1.0, max(0.0, 1.0 - s_t))

    def survival_curve(self, category, per_unit_scale, units, t_values, days_confirmed=None):
        """Survival probabilities for charting, using exactly the same math as predictions.

        Before the confirmation date the item is known to be there, so S = 1.
        """
        out = []
        for t in t_values:
            if days_confirmed and t < days_confirmed:
                out.append(1.0)
            else:
                out.append(1.0 - self.depletion_probability(category, t, per_unit_scale, units, days_confirmed))
        return out
