import math

import pytest

from engine import PantryDepletionEngine, mean_to_scale, median_to_scale, survival


@pytest.fixture
def engine():
    return PantryDepletionEngine()


def test_priors_file_loads(engine):
    assert engine.priors, "category_priors.json was not found — path is wrong"


@pytest.mark.parametrize("category,expected", [
    ("Dairy & Eggs", "Dairy & Eggs"),
    ("Pantry Staples", "Pantry Staples"),
    ("dairy & eggs", "Dairy & Eggs"),
    ("Milk", "Dairy & Eggs"),
    ("Vegan Cheese", "Dairy & Eggs"),
    ("Bread", "Bakery & Bread"),
    ("Toilet Paper", "Cleaning"),
    ("Something Unknown", None),
    (None, None),
])
def test_category_resolution(engine, category, expected):
    assert engine.resolve_category(category) == expected


def test_every_default_category_has_a_prior(engine):
    from config import DEFAULT_CATEGORIES
    for en, _ in DEFAULT_CATEGORIES:
        assert engine.resolve_category(en) is not None, en


def test_median_really_is_median(engine):
    """At t = median days, P(empty) should be 50% (the old code gave ~63%)."""
    p = engine.category_params("Dairy & Eggs")
    prob = engine.depletion_probability("Dairy & Eggs", p["median_days"])
    assert prob == pytest.approx(0.5, abs=1e-9)


def test_negative_days_do_not_crash(engine):
    assert engine.depletion_probability("Produce", -5) == 0.0


def test_units_scale_linearly(engine):
    one = engine.depletion_probability("Produce", 6, None, 1)
    two = engine.depletion_probability("Produce", 12, None, 2)
    assert one == pytest.approx(two)


def test_conditional_survival_lowers_risk(engine):
    plain = engine.depletion_probability("Dairy & Eggs", 12)
    confirmed = engine.depletion_probability("Dairy & Eggs", 12, days_confirmed=10)
    assert confirmed < plain
    # Confirmed today → zero risk right now
    assert engine.depletion_probability("Dairy & Eggs", 12, days_confirmed=12) == pytest.approx(0.0)


def test_conversions_roundtrip():
    rho, scale = 1.35, 10.0
    median = scale * math.log(2) ** (1 / rho)
    assert median_to_scale(median, rho) == pytest.approx(scale)
    mean = scale * math.gamma(1 + 1 / rho)
    assert mean_to_scale(mean, rho) == pytest.approx(scale)
    assert survival(0, scale, rho) == 1.0


def test_learning_moves_toward_data(engine):
    prior = engine.prior_scale("Dairy & Eggs")
    fast = engine.estimate_item_scale("Dairy & Eggs", [3, 3, 3, 3], [])
    slow = engine.estimate_item_scale("Dairy & Eggs", [30, 30, 30, 30], [])
    assert fast < prior < slow


def test_item_learning_beats_category(engine):
    """Milk's own history should matter more than what cheese does."""
    cat = [20.0] * 10  # rest of dairy lasts long
    milk = engine.estimate_item_scale("Dairy & Eggs", [4.0] * 5, cat + [4.0] * 5)
    cheese_like = engine.estimate_item_scale("Dairy & Eggs", [], cat + [4.0] * 5)
    assert milk < cheese_like


def test_curve_matches_prediction(engine):
    ts = [0, 5, 10, 20]
    curve = engine.survival_curve("Snacks", 9.0, 2, ts)
    for t, s in zip(ts, curve):
        assert s == pytest.approx(1 - engine.depletion_probability("Snacks", t, 9.0, 2))
