import json
import types

from matcher import ProductMatcher
from normalize import Size, parse_size_text

PRODUCTS = [
    {"id": 1, "name_en": "3% Milk", "name_he": "חלב 3%", "category": "Dairy & Eggs"},
    {"id": 2, "name_en": "1% Milk", "name_he": "חלב 1%", "category": "Dairy & Eggs"},
    {"id": 3, "name_en": "Banana", "name_he": "בננה", "category": "Produce"},
    {"id": 4, "name_en": "Cottage cheese", "name_he": "קוטג'", "category": "Dairy & Eggs"},
]
ALIASES = {"חלב תנובה 3%": 1}


def test_alias_wins():
    s = ProductMatcher(PRODUCTS, ALIASES).suggest([{"raw_name": "חלב תנובה 3% 2 ליטר", "name_en": "Milk"}])[0]
    assert (s.product_id, s.reason) == (1, "alias")


def test_clear_similarity_matches_without_ai():
    s = ProductMatcher(PRODUCTS, {}).suggest([{"raw_name": "Bananas 1.2 kg", "name_en": "Bananas"}])[0]
    assert (s.product_id, s.reason) == (3, "similar")


def test_unrelated_is_new():
    s = ProductMatcher(PRODUCTS, {}).suggest([{"raw_name": "נייר טואלט 32 גלילים", "name_en": "Toilet paper"}])[0]
    assert s.product_id is None and s.reason == "new"


def test_ambiguous_goes_to_ai_and_respects_its_answer():
    calls = []

    class FakeParser:
        available = True

        def choose_products(self, reqs):
            calls.append(reqs)
            return {r["i"]: (1 if "3%" in r["line"] else None) for r in reqs}

    items = [{"raw_name": "חלב טרה 3% קרטון", "name_he": "חלב טרה 3%", "name_en": "Tara milk 3%"},
             {"raw_name": "חלב סויה", "name_he": "חלב סויה", "name_en": "Soy milk"}]
    out = ProductMatcher(PRODUCTS, {}, FakeParser()).suggest(items)
    assert len(calls) == 1                                   # one batched call
    assert out[0].product_id == 1 and out[0].reason == "ai"
    assert out[1].product_id is None and out[1].reason == "new"
    assert out[1].candidates                                 # still offers milks in the dropdown


def test_without_ai_only_strong_matches():
    out = ProductMatcher(PRODUCTS, {}).suggest([{"raw_name": "חלב טרה 3%", "name_he": "חלב טרה 3%"}])
    assert out[0].product_id == 1                            # "חלב 3%" ⊂ "חלב טרה 3%" is strong


def test_parse_size_text():
    assert parse_size_text("1 L") == Size(1000, "ml")
    assert parse_size_text("500 גרם") == Size(500, "g")
    assert parse_size_text("12") == Size(12, "pc")
    assert parse_size_text("") is None and parse_size_text("big") is None


def test_choose_products_validates_ai_output(monkeypatch):
    from text_parser import TextListParser
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    p = TextListParser()
    reply = {"matches": [{"i": 0, "product_id": 1}, {"i": 1, "product_id": 99}, {"i": 7, "product_id": 2}]}
    p._client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(
        create=lambda **kw: types.SimpleNamespace(choices=[types.SimpleNamespace(
            message=types.SimpleNamespace(content=json.dumps(reply)))]))))
    reqs = [{"i": 0, "line": "a", "en": "", "he": "", "candidates": [{"id": 1}]},
            {"i": 1, "line": "b", "en": "", "he": "", "candidates": [{"id": 2}]}]
    assert p.choose_products(reqs) == {0: 1, 1: None}       # invented id → new; unknown i ignored
