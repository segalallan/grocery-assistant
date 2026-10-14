from datetime import date, timedelta

import pytest

import pantry_service
from engine import PantryDepletionEngine
from pantry_service import PantryService

TODAY = date(2026, 10, 13)


@pytest.fixture
def svc(backend, monkeypatch):
    monkeypatch.setattr(pantry_service, "app_today", lambda: TODAY)
    db = backend()
    ok, err = db.create_user("a@b.com", "pw", "A", "B", household_name="Home")
    assert ok, err
    return PantryService(db, PantryDepletionEngine())


def hh(svc):
    with svc.db.connect() as c:
        return c.execute("SELECT id FROM households").fetchone()[0]


def d(days_ago):
    return TODAY - timedelta(days=days_ago)


def history(svc):
    with svc.db.connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM purchase_history ORDER BY id")]


def test_manual_purchase_gets_a_learned_scale(svc):
    h = hh(svc)
    svc.record_purchase(h, "Milk", "חלב", "Dairy & Eggs", 1, d(2), "A")
    items = svc.get_pantry(h)
    assert len(items) == 1
    assert items[0]["per_unit_scale"] > 0
    assert items[0]["days_elapsed"] == 2


def test_repurchase_logs_per_item_interval(svc):
    h = hh(svc)
    svc.record_purchase(h, "Milk", "חלב", "Dairy & Eggs", 2, d(10), "A")
    res = svc.record_purchase(h, "milk ", "חלב", "Dairy & Eggs", 1, d(0), "A")
    assert res.status == "restocked"
    hist = history(svc)
    assert len(hist) == 1
    assert hist[0]["item_key"] == svc.get_pantry(h)[0]["item_key"]
    assert hist[0]["interval_days"] == pytest.approx(5.0)  # 10 days / 2 units
    assert len(svc.get_pantry(h)) == 1  # no duplicate rows


def test_learning_is_per_item(svc):
    h = hh(svc)
    for i in range(4):  # milk every 3 days
        svc.record_purchase(h, "Milk", "", "Dairy & Eggs", 1, d(12 - 3 * i), "A")
    for i in range(3):  # cheese every 20 days
        svc.record_purchase(h, "Cheese", "", "Dairy & Eggs", 1, d(60 - 20 * i), "A")
    pantry = {i["name_en"]: i for i in svc.get_pantry(h)}
    assert pantry["Milk"]["per_unit_scale"] < pantry["Cheese"]["per_unit_scale"]


def test_same_day_purchase_adds_units(svc):
    h = hh(svc)
    svc.record_purchase(h, "Eggs", "", "Dairy & Eggs", 1, d(0), "A")
    res = svc.record_purchase(h, "Eggs", "", "Dairy & Eggs", 2, d(0), "A")
    assert res.status == "added_units"
    assert svc.get_pantry(h)[0]["units"] == 3


def test_backdated_purchase_keeps_current_stock(svc):
    h = hh(svc)
    svc.record_purchase(h, "Rice", "", "Pantry Staples", 1, d(1), "A")
    res = svc.record_purchase(h, "Rice", "", "Pantry Staples", 1, d(30), "A")
    assert res.status == "backdated"
    items = svc.get_pantry(h)
    assert len(items) == 1 and items[0]["days_elapsed"] == 1


def test_future_date_rejected(svc):
    with pytest.raises(ValueError):
        svc.record_purchase(hh(svc), "Milk", "", "Dairy & Eggs", 1, TODAY + timedelta(days=1), "A")


def test_ran_out_logs_interval_and_adds_to_list(svc):
    h = hh(svc)
    svc.record_purchase(h, "Bread", "", "Pantry Staples", 1, d(4), "A")
    inv_id = svc.get_pantry(h)[0]["id"]
    svc.mark_depleted(h, [inv_id], TODAY, "A")
    assert svc.get_pantry(h) == []
    assert history(svc)[0]["kind"] == "depleted"
    assert history(svc)[0]["interval_days"] == pytest.approx(4.0)
    assert [s["item_name_en"] for s in svc.get_shopping_list(h)] == ["Bread"]


def test_still_going_keeps_purchase_date(svc):
    h = hh(svc)
    svc.record_purchase(h, "Milk", "", "Dairy & Eggs", 1, d(20), "A")
    item = svc.get_pantry(h)[0]
    before = item["prob_depleted"]
    svc.mark_still_going(h, [item["id"]], TODAY)
    after = svc.get_pantry(h)[0]
    assert after["purchase_date"] == item["purchase_date"]
    assert after["prob_depleted"] < before
    # Rebuying later records the true 20-day interval, not 0
    svc.record_purchase(h, "Milk", "", "Dairy & Eggs", 1, TODAY, "A")
    assert history(svc)[0]["interval_days"] == pytest.approx(20.0)


def test_shopping_list_dedupes(svc):
    h = hh(svc)
    item = {"name_en": "Milk", "name_he": "חלב", "category": "Dairy & Eggs"}
    assert svc.add_to_shopping_list(h, [item], "manual", "A") == 1
    assert svc.add_to_shopping_list(h, [item], "smart_alert", "A") == 0
    assert len(svc.get_shopping_list(h)) == 1


def test_bought_from_list_records_purchase_and_clears(svc):
    h = hh(svc)
    svc.record_purchase(h, "Milk", "", "Dairy & Eggs", 1, d(6), "A")
    svc.add_to_shopping_list(h, [{"name_en": "Milk", "name_he": "", "category": "Dairy & Eggs"}], "manual", "A")
    shop_id = svc.get_shopping_list(h)[0]["id"]
    res = svc.bought_from_list(h, shop_id, 1, "A")
    assert res.status == "restocked"
    assert svc.get_shopping_list(h) == []
    assert len(history(svc)) == 1


def test_restock_candidates_skip_items_on_list(svc):
    h = hh(svc)
    svc.record_purchase(h, "Milk", "", "Dairy & Eggs", 1, d(40), "A")
    assert len(svc.restock_candidates(svc.get_pantry(h))) == 1
    svc.add_to_shopping_list(h, [{"name_en": "Milk", "name_he": "", "category": "Dairy & Eggs"}], "manual", "A")
    assert svc.restock_candidates(svc.get_pantry(h)) == []


def test_category_in_use_cannot_be_deleted(svc):
    h = hh(svc)
    svc.record_purchase(h, "Milk", "", "Dairy & Eggs", 1, d(1), "A")
    ok, n = svc.db.delete_category(h, "Dairy & Eggs")
    assert not ok and n == 1
    ok, _ = svc.db.delete_category(h, "Snacks")
    assert ok


def test_duplicate_signup_leaves_no_orphan_household(svc):
    ok, err = svc.db.create_user("A@b.com", "pw", "X", "Y", household_name="Other")
    assert not ok and "exists" in err
    with svc.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM households").fetchone()[0] == 1


def test_auth(svc):
    assert svc.db.authenticate("a@b.com", "pw")["display_name"] == "A B"
    assert svc.db.authenticate("a@b.com", "wrong") is None
    assert svc.db.authenticate("nobody@b.com", "pw") is None
