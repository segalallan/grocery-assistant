"""Products, aliases and package sizes — SQLite and (with TEST_DATABASE_URL) Postgres."""
from datetime import date, timedelta

import pytest

import pantry_service
from engine import PantryDepletionEngine
from normalize import Size
from pantry_service import PantryService

TODAY = date(2026, 10, 14)


@pytest.fixture
def svc(backend, monkeypatch):
    monkeypatch.setattr(pantry_service, "app_today", lambda: TODAY)
    db = backend()
    db.create_user("a@b.com", "pw", "A", "B", household_name="Home")
    return PantryService(db, PantryDepletionEngine())


def hh(svc):
    with svc.db.connect() as c:
        return c.execute("SELECT id FROM households").fetchone()[0]


def d(n):
    return TODAY - timedelta(days=n)


def history(svc):
    with svc.db.connect() as c:
        return [{k: r[k] for k in r.keys()} for r in c.execute("SELECT * FROM purchase_history ORDER BY id")]


def test_same_text_different_sizes_is_one_product(svc):
    h = hh(svc)
    a = svc.record_purchase(h, "", "חלב תנובה 3% 1 ל", "Dairy & Eggs", 1, d(10), "A")
    b = svc.record_purchase(h, "", "חלב תנובה 3% 2 ליטר", "Dairy & Eggs", 1, d(0), "A")
    assert a.product_id == b.product_id
    assert len(svc.get_products(h)) == 1


def test_sizes_become_reference_units(svc):
    h = hh(svc)
    svc.record_purchase(h, "Milk 1L", "", "Dairy & Eggs", 1, d(8), "A")       # reference = 1 L
    svc.record_purchase(h, "Milk 2L", "", "Dairy & Eggs", 2, d(0), "A")       # 2 x 2 L = 4 units
    item = svc.get_pantry(h)[0]
    assert item["eff_units"] == 4.0 and item["units"] == 2
    assert item["ref_size"] == Size(1000, "ml")
    assert history(svc)[0]["interval_days"] == pytest.approx(8.0)             # 8 days / 1 litre


def test_bigger_package_predicts_longer(svc):
    h = hh(svc)
    svc.record_purchase(h, "Juice 1L", "", "Beverages", 1, d(5), "A")
    svc.record_purchase(h, "Coffee 250g", "", "Beverages", 1, d(5), "A")
    small = {i["name_en"]: i for i in svc.get_pantry(h)}["Juice"]["prob_depleted"]
    svc.record_purchase(h, "Juice 3L", "", "Beverages", 1, d(5), "A")         # same day → adds 3 units
    big = {i["name_en"]: i for i in svc.get_pantry(h)}["Juice"]["prob_depleted"]
    assert big < small


def test_size_learning_is_per_litre(svc):
    """A 2 L carton that lasted 8 days teaches the same rate as a 1 L carton that lasted 4."""
    h = hh(svc)
    svc.record_purchase(h, "Milk 1L", "", "Dairy & Eggs", 1, d(16), "A")
    svc.record_purchase(h, "Milk 2L", "", "Dairy & Eggs", 1, d(12), "A")      # 1 L lasted 4 days
    svc.record_purchase(h, "Milk 1L", "", "Dairy & Eggs", 1, d(4), "A")       # 2 L lasted 8 days
    assert [round(x["interval_days"], 6) for x in history(svc)] == [4.0, 4.0]


def test_alias_learned_from_receipt_text(svc):
    h = hh(svc)
    r1 = svc.record_purchase(h, "3% Milk", "חלב 3%", "Dairy & Eggs", 1, d(5), "A")
    svc.learn_aliases(h, r1.product_id, ["חלב טרה 3% קרטון"])                  # user confirmed a match once
    r2 = svc.record_purchase(h, "", "חלב טרה 3% קרטון 1 ל", "Dairy & Eggs", 1, d(0), "A")
    assert r2.product_id == r1.product_id


def test_variants_stay_separate(svc):
    h = hh(svc)
    a = svc.record_purchase(h, "3% Milk", "", "Dairy & Eggs", 1, d(1), "A")
    b = svc.record_purchase(h, "1% Milk", "", "Dairy & Eggs", 1, d(1), "A")
    assert a.product_id != b.product_id


def test_unknown_size_counts_packages(svc):
    h = hh(svc)
    svc.record_purchase(h, "Bread", "לחם", "Pantry Staples", 2, d(1), "A")
    assert svc.get_pantry(h)[0]["eff_units"] == 2.0


def test_user_changes_reference_size_without_changing_predictions(svc):
    h = hh(svc)
    svc.record_purchase(h, "Milk 1L", "", "Dairy & Eggs", 1, d(14), "A")
    svc.record_purchase(h, "Milk 1L", "", "Dairy & Eggs", 2, d(6), "A")
    before = svc.get_pantry(h)[0]
    svc.set_reference_size(h, before["product_id"], 2000, "ml")              # user: "my usual is 2 L"
    after = svc.get_pantry(h)[0]
    assert after["eff_units"] == pytest.approx(1.0) and after["ref_size"] == Size(2000, "ml")
    assert after["prob_depleted"] == pytest.approx(before["prob_depleted"], abs=1e-9)
    assert history(svc)[0]["interval_days"] == pytest.approx(16.0)           # 8 days per litre = 16 per 2 L


def test_merge_products_combines_history_aliases_and_stock(svc):
    h = hh(svc)
    a = svc.record_purchase(h, "Banana", "", "Produce", 1, d(9), "A")
    svc.record_purchase(h, "Banana", "", "Produce", 1, d(6), "A")            # one cycle on "Banana"
    b = svc.record_purchase(h, "Bananot", "בננה", "Produce", 2, d(2), "A")   # a duplicate product
    svc.add_to_shopping_list(h, [{"item_key": f"prod:{b.product_id}"}], "manual", "A")
    svc.merge_products(h, a.product_id, b.product_id)
    assert len(svc.get_products(h)) == 1
    items = svc.get_pantry(h)
    assert len(items) == 1 and items[0]["units"] == 3
    assert {x["item_key"] for x in history(svc)} == {f"prod:{a.product_id}"}
    assert svc.get_shopping_list(h)[0]["item_key"] == f"prod:{a.product_id}"
    r = svc.record_purchase(h, "", "בננה", "Produce", 1, d(0), "A")           # old alias now points to the kept one
    assert r.product_id == a.product_id


def test_pantry_merge_of_different_products_merges_them(svc):
    h = hh(svc)
    svc.record_purchase(h, "Tomato", "", "Produce", 1, d(3), "A")
    svc.record_purchase(h, "Tomatoes cherry", "", "Produce", 1, d(3), "A")
    rows = svc.get_pantry(h)
    svc.merge_items(h, rows[0]["id"], [rows[1]["id"]])
    assert len(svc.get_products(h)) == 1 and len(svc.get_pantry(h)) == 1


def test_rename_product_updates_everywhere(svc):
    h = hh(svc)
    r = svc.record_purchase(h, "Milk", "", "Dairy & Eggs", 1, d(1), "A")
    svc.add_to_shopping_list(h, [{"item_key": r.item_key}], "manual", "A")
    inv_id = svc.get_pantry(h)[0]["id"]
    svc.update_item(h, inv_id, "3% Milk", "חלב 3%", "Dairy & Eggs", 1)
    assert svc.get_pantry(h)[0]["name_he"] == "חלב 3%"
    assert svc.get_shopping_list(h)[0]["item_name_en"] == "3% Milk"
    # the old name still finds the product
    assert svc.record_purchase(h, "milk", "", "Dairy & Eggs", 1, d(0), "A").product_id == r.product_id


def test_user_correction_repoints_alias(svc):
    """A line that was matched to the wrong product, corrected once, maps correctly next time."""
    h = hh(svc)
    milk = svc.record_purchase(h, "3% Milk", "", "Dairy & Eggs", 1, d(9), "A")
    soy = svc.record_purchase(h, "Soy milk", "", "Dairy & Eggs", 1, d(9), "A")
    svc.record_purchase(h, "", "", "Dairy & Eggs", 1, d(5), "A", product_id=milk.product_id, raw_name="משקה סויה")
    svc.record_purchase(h, "", "", "Dairy & Eggs", 1, d(1), "A", product_id=soy.product_id, raw_name="משקה סויה")
    assert svc.get_aliases(h)["משקה סויה"] == soy.product_id
