import json
import os
import numpy as np

PRIORS_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "category_priors.json")

class PantryDepletionEngine:
    def __init__(self):
        self.priors = self._load_priors()

    def _load_priors(self):
        if os.path.exists(PRIORS_FILE):
            with open(PRIORS_FILE, "r") as f:
                return json.load(f)
        return {}

    def get_category_parameters(self, category: str):
        cat_lower = category.lower()
        for key, vals in self.priors.items():
            if key.lower() in cat_lower or cat_lower in key.lower():
                return vals
        return {"lambda_intercept": float(np.log(14.0)), "rho": 1.25, "median_days": 14.0, "lambda_beta_units": 0.0}

    def compute_depletion_probability(self, category: str, days_elapsed: float, user_lambda: float = None, units_qty: int = 1) -> float:
        """
        Two different models depending on whether we have personal history:

        - Personalized (user_lambda present): user_lambda is a per-unit
          day-rate. The correct scale for a batch of units_qty items is 
          user_lambda * units_qty - the full duration scales up linearly 
          with how many units were bought.

        - Cold start (no personal history yet): falls back to the population
          Weibull-AFT model, which fits log(scale) = lambda_intercept + lambda_beta_units * units_bought.
        """
        params = self.get_category_parameters(category)
        
        # Defensive guardrail: prevent sub-1.0 or unexponentiated log-rho artifacts
        raw_rho = params.get("rho", 1.25)
        shape_rho = float(raw_rho) if raw_rho and float(raw_rho) >= 1.15 else 1.25
        
        safe_units = max(1, units_qty)

        if user_lambda and user_lambda > 0:
            scale_lambda = user_lambda * safe_units
        else:
            lambda_intercept = params.get("lambda_intercept", np.log(14.0))
            lambda_beta_units = params.get("lambda_beta_units", 0.0)
            capped_units = min(safe_units, 5)
            scale_lambda = np.exp(lambda_intercept + lambda_beta_units * capped_units)

        prob = 1.0 - np.exp(- (days_elapsed / scale_lambda) ** shape_rho)
        return float(np.clip(prob, 0.0, 1.0))

    def update_user_scale(self, category: str, recorded_intervals: list[int]) -> float:
        params = self.get_category_parameters(category)
        prior_lambda = np.exp(params.get("lambda_intercept", np.log(14.0)))

        if not recorded_intervals:
            return float(prior_lambda)

        sample_mean = np.mean(recorded_intervals)
        n = len(recorded_intervals)
        pseudo_prior_n = 3.0
        shrinkage_lambda = (pseudo_prior_n * prior_lambda + n * sample_mean) / (pseudo_prior_n + n)
        return float(shrinkage_lambda)