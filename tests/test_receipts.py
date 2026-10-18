"""Receipt reading: image prep, the deterministic assembly and checks, the second look, and the app flow.

The vision model itself is replaced by canned transcriptions shaped like real ones
(an Osher Ad digital receipt and a Carrefour photo). Real-receipt accuracy is measured
separately with notebooks/eval_receipts.py, which needs an API key.
"""
import io
import json
from datetime import timedelta

import pytest

from receipt_reader import (ReceiptError, ReceiptReader, assemble, code_alias, load_images, prepare,
                            slice_tall, _ink_profile)


def I(code, text, qty, price, kg=False):
    return {"kind": "item", "code": code, "text": text, "qty": qty, "qty_unit": "kg" if kg else "unit", "price": price}


# A slice of a real Osher Ad receipt: deal notes, a deposit, repeated lines, weighed produce.
OSHER = {
    "store": "אושר עד", "date": "2026-07-16", "total": 0.0, "item_count": 0,
    "rows": [
        I("7290110559014", "שעועית שחורה שטראוס", 2, 12.00),
        {"kind": "promo", "code": None, "text": "2 ב 12", "qty": None, "qty_unit": "unit", "price": None},
        I("7290013010001", "עגבניות מרוסקות מבור", 4, 18.00),
        {"kind": "promo", "code": None, "text": "4 ב 18", "qty": None, "qty_unit": "unit", "price": None},
        I("7290013010001", "עגבניות מרוסקות מבור", 4, 18.00),
        I("23809", "יין קלאסיק קברנה", 2, 59.40),
        {"kind": "deposit", "code": "901046", "text": "מיחזור אריזה", "qty": 2, "qty_unit": "unit", "price": 0.60},
        I("7290102398065", "חלב 3% טרה 2 ליטר וס", 1, 14.40),
        I("2069041", "חזה עוף טרי פרימה", 1.13, 39.48, kg=True),
        I("9005417", "בננה", 0.94, 8.37, kg=True),
        I("2069041", "חזה עוף טרי פרימה", 0.40, 13.86, kg=True),
    ],
}
OSHER["total"] = round(sum(r["price"] or 0 for r in OSHER["rows"]), 2)
OSHER["item_count"] = 2 + 4 + 4 + 2 + 2 + 1 + 1 + 1 + 1   # units; weighed lines count once; deposit units count

CARREFOUR = {
    "store": "Carrefour City", "date": "2026-09-17", "total": 0.0, "item_count": 4,
    "rows": [
        I("1564", "תפוח עץ סמיט", 0.440, 6.56, kg=True),
        {"kind": "other", "code": None, "text": "0.020 ק\"ג x 14.90", "qty": 0.02, "qty_unit": "kg", "price": None},
        {"kind": "discount", "code": None, "text": "זיכוי אריזה", "qty": None, "qty_unit": "unit", "price": -0.30},
        I("7290004131074", "חלב המוגגני מה", 2, 14.70),
        I("1501", "עגבניה", 0.170, 2.02, kg=True),
        {"kind": "discount", "code": None, "text": "זיכוי אריזה", "qty": None, "qty_unit": "unit", "price": -0.0},
    ],
}
CARREFOUR["total"] = round(sum(r["price"] or 0 for r in CARREFOUR["rows"]), 2)


# ---------------------------------------------------------------- assembly
def test_osher_receipt_reconciles_and_merges():
    r = assemble(OSHER)
    assert not r.sum_mismatch and not r.count_mismatch
    texts = [ln.text for ln in r.lines]
    assert "2 ב 12" not in texts and "מיחזור אריזה" not in texts      # deal notes and deposits aren't products
    assert r.deposits == 0.60
    tomatoes = next(ln for ln in r.lines if ln.code == "7290013010001")
    assert (tomatoes.units, tomatoes.lines, tomatoes.price) == (8, 2, 36.00)
    chicken = next(ln for ln in r.lines if ln.code == "2069041")
    assert chicken.weighed and chicken.units == 1 and chicken.size.amount == 1530 and chicken.size.unit == "g"
    assert chicken.price == pytest.approx(53.34)
    milk = next(ln for ln in r.lines if ln.code == "7290102398065")
    assert milk.size.amount == 2000 and milk.size.unit == "ml"       # "2 ליטר" read from the text
    assert r.merged == 2
    assert r.date == "2026-07-16" and r.store == "אושר עד"


def test_carrefour_credits_net_into_their_item_and_tare_is_ignored():
    r = assemble(CARREFOUR)
    assert not r.sum_mismatch and not r.count_mismatch
    apple = r.lines[0]
    assert apple.price == pytest.approx(6.26) and apple.size.amount == 440
    assert len(r.lines) == 3


def test_missing_line_is_detected_by_both_checks():
    broken = json.loads(json.dumps(OSHER))
    del broken["rows"][5]                       # the wine (2 units, ₪59.40) was missed
    r = assemble(broken)
    assert r.sum_mismatch and r.count_mismatch
    assert r.missing_amount == pytest.approx(59.40)
    assert r.item_count - r.counted == 2
    assert r.needs_second_look


def test_model_slips_are_corrected():
    raw = {"total": "₪21,20", "item_count": "3", "rows": [
        {"kind": "item", "code": "901046", "text": "מיחזור אריזה", "qty": 1, "price": "0.30"},   # deposit as item
        {"kind": "item", "code": None, "text": "3 מוגבל 9.90", "qty": None, "price": None},      # deal note as item
        {"kind": "item", "code": "72900-1", "text": "סילאן", "qty": "1", "price": "19,90"},
        {"kind": "item", "code": None, "text": "הנחה", "qty": None, "price": "1.00-"},           # credit as item
        {"kind": "weird", "code": None, "text": "שקית", "qty": 1, "price": 2.00},
    ]}
    r = assemble(raw)
    assert [ln.text for ln in r.lines] == ["סילאן", "שקית"]
    assert r.lines[0].code == "729001" and r.lines[0].price == pytest.approx(18.90)
    assert r.total == 21.20 and r.deposits == 0.30 and not r.sum_mismatch


# ---- failure patterns seen with the real model on real receipts (eval of 18/10) ----
def _copy(x):
    return json.loads(json.dumps(x))


def test_split_row_with_dropped_price_is_recovered_from_the_total():
    """receipt: '18.90' alone at the bottom of page 1; the model kept the row but lost its price."""
    raw = _copy(OSHER)
    raw["rows"][5]["price"] = None                       # the wine, ₪59.40
    r = assemble(raw)
    assert not r.sum_mismatch and r.fixed == 1
    assert next(ln for ln in r.lines if ln.code == "23809").price == pytest.approx(59.40)


def test_split_row_with_its_price_as_a_separate_row():
    """receipt5: the model output '39.30' as a row of its own and the cola row without a price."""
    raw = _copy(OSHER)
    raw["rows"][5]["price"] = None
    raw["rows"].insert(5, {"kind": "item", "code": None, "text": "59.40", "qty": 1, "qty_unit": "unit",
                           "price": None})
    r = assemble(raw)
    assert not r.sum_mismatch and not r.count_mismatch           # the fake row no longer counts
    assert next(ln for ln in r.lines if ln.code == "23809").price == pytest.approx(59.40)
    assert "59.40" not in [ln.text for ln in r.lines]


def test_lone_price_after_its_item_still_finds_it():
    raw = _copy(OSHER)
    raw["rows"][5]["price"] = None
    raw["rows"].insert(6, {"kind": "other", "code": None, "text": "", "qty": None, "qty_unit": "unit", "price": 59.40})
    r = assemble(raw)
    assert not r.sum_mismatch and next(ln for ln in r.lines if ln.code == "23809").price == pytest.approx(59.40)


def test_payment_lines_are_not_purchases():
    """carrefour: the second card payment (₪5.42) was read as a line."""
    raw = _copy(CARREFOUR)
    raw["rows"] += [
        {"kind": "other", "code": None, "text": "כרטיס: ****4451", "qty": None, "qty_unit": "unit", "price": 5.42},
        {"kind": "item", "code": None, "text": "עודף", "qty": None, "qty_unit": "unit", "price": 0.0},
        {"kind": "item", "code": None, "text": 'סה"כ לתשלום', "qty": None, "qty_unit": "unit", "price": raw["total"]},
    ]
    r = assemble(raw)
    assert not r.sum_mismatch and not r.count_mismatch and len(r.lines) == 3


def test_deal_line_amounts_count_only_if_they_add_up():
    raw = _copy(OSHER)
    raw["rows"].insert(3, {"kind": "discount", "code": None, "text": "רוטב 260 ג 4 ב 15-", "qty": None,
                           "qty_unit": "unit", "price": -15.0})
    assert not assemble(raw).sum_mismatch                 # line prices already include the deal: a note
    shufersal_style = _copy(raw)
    shufersal_style["total"] = round(raw["total"] - 15.0, 2)
    assert not assemble(shufersal_style).sum_mismatch     # here the deal really is a separate discount


def test_misread_barcodes_are_never_remembered():
    from receipt_reader import barcode_ok
    assert barcode_ok("7290010175840") and barcode_ok("7290004131074") and barcode_ok("80913467")
    assert barcode_ok("729010175840") is False and barcode_ok("7290004310174") is False
    assert barcode_ok("96619926657") is None or barcode_ok("96619926657") in (True, False)
    assert code_alias("7290004310174", "x") is None and code_alias("7290004131074", "x") == "code 7290004131074"


def test_photo_is_cropped_to_the_paper():
    from PIL import Image
    from receipt_reader import crop_to_paper
    photo = Image.new("RGB", (1200, 1600), (60, 40, 30))          # dark table
    photo.paste(_receipt_png(rows=40, width=400), (400, 150))      # narrow white receipt
    out = crop_to_paper(photo)
    assert out.width >= 900 and out.height / out.width > 2.5       # tight around the paper, enlarged
    white = Image.new("RGB", (800, 1000), "white")
    assert crop_to_paper(white) is white                           # no background → untouched


# ---- second real run (18/10): deal lines read as credits, products read as credits, footer/date slips ----
def test_deal_line_with_minus_before_the_amount_is_a_note():
    raw = _copy(OSHER)
    raw["rows"].insert(3, {"kind": "discount", "code": None, "text": "רוטב 260 ג 4 ב -15", "qty": 4,
                           "qty_unit": "unit", "price": -15})
    r = assemble(raw)
    assert not r.sum_mismatch and r.fixed == 1


def test_product_misread_as_a_credit_is_restored():
    """receipt3: deal text glued to 'לבבות דקל' made the model call the whole row a −10.90 credit."""
    raw = _copy(OSHER)
    raw["total"] = round(raw["total"] + 10.90, 2)
    raw["item_count"] += 1
    raw["rows"].append({"kind": "discount", "code": "7290010175840", "text": "חסה לבבו 2 ב 18- לבבות דקל שלם טעים 4",
                        "qty": 1, "qty_unit": "unit", "price": -10.90})
    r = assemble(raw)
    assert not r.sum_mismatch and not r.count_mismatch
    palm = next(ln for ln in r.lines if ln.code == "7290010175840")
    assert palm.price == pytest.approx(10.90) and "2 ב 18" not in palm.text


def test_total_taken_from_the_footer_when_a_card_amount_was_picked():
    """carrefour: paid with two cards; the model reported one card's 63.96 as the total."""
    raw = _copy(CARREFOUR)
    real = raw["total"]
    raw["total"], raw["footer_amounts"] = 13.00, [real, 13.00, round(real - 13.00, 2), 0.0]
    r = assemble(raw)
    assert r.total == pytest.approx(real) and not r.sum_mismatch


def test_tare_lines_are_not_products():
    raw = _copy(CARREFOUR)
    raw["rows"][1] = {"kind": "item", "code": None, "text": '0.020 ק"ג x 14.90', "qty": 0.02, "qty_unit": "kg",
                      "price": 0.30}
    raw["rows"].insert(2, {"kind": "item", "code": None, "text": "0.010 x 5.90", "qty": 0.01, "qty_unit": "kg",
                           "price": None})
    r = assemble(raw)
    assert not r.count_mismatch and len(r.lines) == 3 and not r.sum_mismatch


def test_deal_text_is_stripped_from_descriptions():
    raw = {"total": 25.0, "rows": [I("7290010984565", "זית חרוזית בני דרום 2 ב 25", 2, 25.0)]}
    assert assemble(raw).lines[0].text == "זית חרוזית בני דרום"
    raw = {"total": 9.9, "rows": [I("572819", 'טחינה אחוה 1 ק"ג 9.90 מוגבל 3', 1, 9.9)]}
    assert assemble(raw).lines[0].text == 'טחינה אחוה 1 ק"ג'


def test_receipt_dates_are_day_first():
    from config import today
    from receipt_reader import parse_receipt_date
    t = today()
    short = t.strftime("%d/%m/%y")
    assert parse_receipt_date(short) == t.isoformat()
    assert parse_receipt_date(t.strftime("%d.%m.%Y") + " 18:04") == t.isoformat()
    wrong_way = f"20{t:%d}-{t:%m}-{t:%y}"                 # "17/09/26" read as 2017-09-26
    assert parse_receipt_date(wrong_way) == t.isoformat()
    assert parse_receipt_date("31/02/26") is None and parse_receipt_date("") is None
    assert parse_receipt_date(f"{t.year + 3}-01-01") is None   # never in the future


# ---- third real run: products labelled "discount" with positive prices, misread weights, glued prefixes ----
def test_positive_discount_is_a_product():
    raw = _copy(OSHER)
    raw["rows"][0]["kind"] = "discount"                  # 2 × beans, +12.00
    r = assemble(raw)
    assert not r.sum_mismatch and not r.count_mismatch
    assert any(ln.code == "7290110559014" and ln.units == 2 for ln in r.lines)


def test_weight_is_checked_against_price_per_kg():
    raw = {"total": 6.56, "rows": [dict(I("1564", "תפוח עץ סמיט", 0.64, 6.56, kg=True), unit_price=14.90)]}
    assert assemble(raw).lines[0].size.amount == 440           # 6.56 / 14.90 = 0.440 kg, not 0.64


def test_weight_and_quantity_prefixes_are_removed_from_names():
    raw = {"total": 17.29, "rows": [I("9000122", '0.59 ק"ג עגבניה', 0.59, 2.89, kg=True),
                                    I("522319", "2 חלב 3% רמת הגולן 1 ל", 2, 14.40)]}
    assert [ln.text for ln in assemble(raw).lines] == ["עגבניה", "חלב 3% רמת הגולן 1 ל"]


# ---- multi-page PDFs: rows split by a page break are re-joined in the image ----
def _page(rows, width=800, row_h=40, gap=30, top=60):
    """A synthetic receipt page. Each row is a set of columns: P(rice) Q(ty) D(escription) C(ode)."""
    from PIL import Image, ImageDraw
    cols = {"P": (10, 110), "Q": (170, 240), "D": (300, 520), "C": (600, 790)}
    img = Image.new("RGB", (width, top * 2 + len(rows) * (row_h + gap)), "white")
    d = ImageDraw.Draw(img)
    for i, zs in enumerate(rows):
        y = top + i * (row_h + gap)
        for z in zs:
            x0, x1 = cols[z]
            d.rectangle((x0, y + 8, x1, y + row_h - 8), fill="black")
    return img


def _blocks_and_zones(img):
    from receipt_reader import _row_blocks, _zones
    g = img.convert("L")
    return [_zones(g, b) for b in _row_blocks(g)]


def test_lone_price_at_page_bottom_joins_its_row():
    from receipt_reader import stitch_pages
    full = "PQDC"
    out = stitch_pages([_page([full, full, "P"]), _page(["QDC", full])])
    assert _blocks_and_zones(out) == [set(full)] * 4            # 5 pieces → 4 whole rows


def test_description_at_page_top_joins_its_row():
    from receipt_reader import stitch_pages
    full = "PQDC"
    out = stitch_pages([_page([full, "PQC"]), _page(["D", full])])
    assert _blocks_and_zones(out) == [set(full)] * 3


def test_complete_rows_are_never_merged():
    from receipt_reader import stitch_pages
    full = "PQDC"
    out = stitch_pages([_page([full, full]), _page(["D", full])])  # a deal note after a complete row
    assert len(_blocks_and_zones(out)) == 4


def test_local_hand_transcriptions_reconcile():
    """Your own receipts (data/receipts/*.truth.json, never committed) must add up exactly."""
    from pathlib import Path
    files = sorted((Path(__file__).resolve().parents[1] / "data" / "receipts").glob("*.truth.json"))
    if not files:
        pytest.skip("no local receipt transcriptions")
    for f in files:
        r = assemble(json.loads(f.read_text(encoding="utf-8")))
        assert not r.sum_mismatch and not r.count_mismatch, f.name


def test_garbage_is_harmless():
    for raw in ({}, {"rows": None}, {"rows": ["x", 3, None]}, {"rows": [{"text": ""}]}):
        r = assemble(raw)
        assert r.lines == [] and r.needs_second_look


def test_code_alias():
    assert code_alias("7290004131074", "Carrefour") == "code 7290004131074"     # barcode: global
    assert code_alias("1501", "Carrefour City") == "code carrefour city 1501"     # short code: per chain
    assert code_alias("1501", "") is None
    assert code_alias(None, "x") is None


# ---------------------------------------------------------------- images
def _receipt_png(rows=60, width=400, gap=14, line=20):
    from PIL import Image, ImageDraw
    h = rows * (line + gap) + 40
    img = Image.new("RGB", (width, h), "white")
    d = ImageDraw.Draw(img)
    for i in range(rows):
        y = 20 + i * (line + gap)
        d.rectangle((40, y, width - 40, y + line), fill="black")
    return img


def test_slicing_only_cuts_through_blank_rows():
    img = _receipt_png()
    pieces = slice_tall(img)
    assert len(pieces) > 1
    assert sum(p.height for p in pieces) == img.height
    for p in pieces[:-1]:
        assert _ink_profile(p)[-1] == 0              # the cut is in a white gap
        assert p.height <= p.width * 2.2 + 1
    assert slice_tall(_receipt_png(rows=5)) == [slice_tall(_receipt_png(rows=5))[0]]  # short ones untouched


def test_pdf_pages_are_rendered_and_blank_space_squeezed():
    from PIL import Image
    pages = [_receipt_png(rows=20), Image.new("RGB", (400, 900), "white")]
    pages[1].paste(_receipt_png(rows=2), (0, 0))      # a last page that's mostly empty
    buf = io.BytesIO()
    pages[0].save(buf, format="PDF", save_all=True, append_images=pages[1:])
    data = buf.getvalue()
    assert len(load_images(data, "r.pdf")) == 2
    imgs = prepare(data, "r.pdf")
    assert sum(i.height for i in imgs) < sum(p.height for p in pages) * 3 * 0.8   # rendered ×3, blank part removed


def test_bad_files():
    with pytest.raises(ReceiptError, match="empty"):
        load_images(b"", "x.jpg")
    with pytest.raises(ReceiptError, match="bad_file"):
        load_images(b"not an image", "x.jpg")


# ---------------------------------------------------------------- two passes
class FakeClient:
    """Stands in for openai.OpenAI: returns canned JSON answers in order."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []
        self.chat = self
        self.completions = self

    def create(self, **kw):
        self.prompts.append(kw)
        ans = self.answers.pop(0)
        if isinstance(ans, Exception):
            raise ans

        class M:  # minimal response shape
            pass
        m = M()
        m.choices = [M()]
        m.choices[0].message = M()
        m.choices[0].message.content = json.dumps(ans, ensure_ascii=False)
        return m


def _png_bytes():
    buf = io.BytesIO()
    _receipt_png(rows=10).save(buf, format="PNG")
    return buf.getvalue()


def test_second_look_when_a_line_is_missing(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    incomplete = json.loads(json.dumps(OSHER))
    del incomplete["rows"][5]
    reader = ReceiptReader()
    reader._client = FakeClient([incomplete, OSHER])
    r = reader.read(_png_bytes(), "r.png")
    assert r.rechecked and not r.sum_mismatch and r.calls == 2
    hint = reader._client.prompts[1]["messages"][1]["content"][0]["text"]
    assert "59.40" in hint and "units" in hint                       # told exactly what was off


def test_no_second_look_when_it_adds_up(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    reader = ReceiptReader()
    reader._client = FakeClient([OSHER])
    r = reader.read(_png_bytes(), "r.png")
    assert r.calls == 1 and not r.rechecked


def test_worse_second_look_is_ignored_and_model_falls_back(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    incomplete = json.loads(json.dumps(OSHER))
    del incomplete["rows"][5]
    worse = json.loads(json.dumps(incomplete))
    del worse["rows"][0]
    reader = ReceiptReader(model="no-such-model")
    reader._client = FakeClient([RuntimeError("model not found"), incomplete, worse])
    r = reader.read(_png_bytes(), "r.png")
    assert r.sum_mismatch and not r.rechecked and r.missing_amount == pytest.approx(59.40)
    assert reader._client.prompts[1]["model"] == "gpt-4o-mini"


def test_api_failure_and_missing_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ReceiptError, match="no_key"):
        ReceiptReader().read(_png_bytes(), "r.png")
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    reader = ReceiptReader()
    reader._client = FakeClient([RuntimeError("down"), RuntimeError("down")])
    with pytest.raises(ReceiptError, match="api"):
        reader.read(_png_bytes(), "r.png")


# ---------------------------------------------------------------- matching & saving
def test_item_codes_match_exactly_and_never_fuzzily():
    from matcher import ProductMatcher
    products = [{"id": 1, "name_en": "Tomato", "name_he": "עגבניה", "category": "Produce"},
                {"id": 2, "name_en": "Cucumber", "name_he": "מלפפון", "category": "Produce"}]
    aliases = {"code osher 9000122": 1, "code osher 9000207": 2}
    m = ProductMatcher(products, aliases)
    s = m.suggest([{"raw_name": "xyz", "code_key": "code osher 9000207"},
                   {"raw_name": "xyz", "code_key": "code osher 9000208"}])
    assert (s[0].product_id, s[0].reason) == (2, "alias")
    assert s[1].product_id is None                                   # a near-identical code is NOT a match


def test_saved_receipt_line_remembers_its_code(backend):
    from config import today
    from engine import PantryDepletionEngine
    from pantry_service import PantryService
    db = backend()
    ok, _ = db.create_user("r@b.com", "password1", "R", "S", household_name="Home")
    hh = db.authenticate("r@b.com", "password1")["household_id"]
    svc = PantryService(db, PantryDepletionEngine())
    svc.record_purchases(hh, [{"name_en": "Banana", "name_he": "בננה", "category": "Produce", "units": 1,
                               "raw_name": "בננה", "aliases": ["code אושר עד 9005417"]}],
                         today() - timedelta(days=3), "R")
    pid = svc.get_products(hh)[0]["id"]
    assert svc.get_aliases(hh)["code אושר עד 9005417"] == pid


# ---------------------------------------------------------------- app
@pytest.fixture
def app_env(backend, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    database = backend()
    ok, _ = database.create_user("a@b.com", "password1", "Avi", "S", household_name="Home")
    return database


def _logged_in(db):
    from pathlib import Path
    import streamlit as st
    from streamlit.testing.v1 import AppTest
    st.cache_resource.clear()
    at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "src" / "app.py"), default_timeout=30)
    user = db.authenticate("a@b.com", "password1")
    at.session_state["authenticated"] = True
    for k in ["household_id", "household_name", "household_code", "display_name", "email", "username"]:
        at.session_state[k] = user[k]
    at.session_state["user_id"] = user["id"]
    at.session_state["intro_dismissed"] = True
    return at


def test_receipt_tab_without_key_is_disabled(app_env):
    at = _logged_in(app_env).run()
    assert not at.exception, at.exception
    btn = next(b for b in at.button if b.label == "📷 Read receipt")
    assert btn.disabled


def test_receipt_review_shows_check_and_saves_codes(app_env):
    from config import today
    from engine import PantryDepletionEngine
    from pantry_service import PantryService
    svc = PantryService(app_env, PantryDepletionEngine())
    hh = app_env.authenticate("a@b.com", "password1")["household_id"]
    at = _logged_in(app_env)
    at.session_state["staged_items"] = [
        {"raw_name": "בננה", "name_en": "Banana", "name_he": "בננה", "category": "Produce", "units": 1,
         "size": "0.94 kg", "price": 8.37, "product_id": None, "match": "new", "code_key": "code אושר עד 9005417"},
        {"raw_name": "חלב 3% טרה 2 ליטר וס", "name_en": "3% Milk", "name_he": "חלב 3%", "category": "Dairy & Eggs",
         "units": 1, "size": "2 L", "price": 14.40, "product_id": None, "match": "new",
         "code_key": "code 7290102398065"},
    ]
    at.session_state["staged_date"] = today() - timedelta(days=2)
    at.session_state["staged_source"] = "receipt"
    at.session_state["receipt_summary"] = {
        "lines": 2, "store": "אושר עד", "total": 82.17, "rows_sum": 22.77, "missing": 59.40, "item_count": 4,
        "counted": 2, "sum_off": True, "count_off": True, "merged": 1, "deposits": 0.6}
    at.run()
    assert not at.exception, at.exception
    warnings = " ".join(w.value for w in at.warning)
    assert "₪59.40" in warnings and "receipt says 4" in warnings
    assert any("combined" in c.value for c in at.caption)
    next(b for b in at.button if b.label == "✅ Save to pantry").click().run()
    assert not at.exception, at.exception
    aliases = svc.get_aliases(hh)
    assert "code 7290102398065" in aliases and "code אושר עד 9005417" in aliases
    banana = next(i for i in svc.get_pantry(hh) if i["name_en"] == "Banana")
    assert banana["size"].amount == 940
    assert banana["days_elapsed"] == 2                               # the receipt's date, not today
    assert "staged_items" not in at.session_state and "receipt_summary" not in at.session_state


# ---------------------------------------------------------------- admin reliability stats
def test_scan_stats_store_no_personal_data(backend):
    db = backend()
    scan = db.log_receipt_scan(source="pdf", chain="Osher Ad", model="m", images=5, calls=1, seconds=12.5,
                               lines=40, merged=2, repaired=1, rechecked=False, has_total=True, sum_ok=True,
                               gap_pct=0.0, has_count=True, count_ok=True)
    db.finish_receipt_scan(scan, "saved", kept=39, unticked=1, rematched=2, edited=3)
    db.finish_receipt_scan(scan, "discarded")                     # first outcome wins
    rows = db.receipt_scans("2000-01-01")
    assert len(rows) == 1 and rows[0]["outcome"] == "saved" and rows[0]["rematched"] == 2
    assert set(rows[0]) == {"id", "scanned_on", "source", "chain", "model", "images", "calls", "seconds", "lines",
                            "merged", "repaired", "rechecked", "has_total", "sum_ok", "gap_pct", "has_count",
                            "count_ok", "outcome", "kept", "unticked", "rematched", "edited"}
    # nothing that identifies a person or what they bought: no user/household, items, prices or amounts


def test_chain_names_come_from_a_fixed_list():
    from receipt_reader import chain_name
    assert chain_name("אושר עד") == "Osher Ad" and chain_name("Carrefour city") == "Carrefour"
    assert chain_name("מכולת של יוסי, רחוב הרצל 5") == "Other"         # free text never stored
    assert chain_name("") == "Unknown"


def test_saved_receipt_review_records_corrections(app_env):
    from config import today
    at = _logged_in(app_env)
    scan = app_env.log_receipt_scan(source="photo", chain="Carrefour", lines=2, has_total=True, sum_ok=True,
                                    has_count=True, count_ok=False)
    items = [
        {"raw_name": "בננה", "name_en": "Banana", "name_he": "בננה", "category": "Produce", "units": 1,
         "size": "0.94 kg", "price": 8.37, "product_id": None, "match": "new", "code_key": None},
        {"raw_name": "חלב", "name_en": "Milk", "name_he": "חלב", "category": "Dairy & Eggs", "units": 2,
         "size": "", "price": 14.70, "product_id": None, "match": "new", "code_key": None},
    ]
    at.session_state["staged_items"] = items
    # what the scanner originally proposed: the milk was matched to product 999 and named differently
    at.session_state["staged_original"] = [
        {"product_id": None, "units": 1, "size": "0.94 kg", "name_en": "Banana", "name_he": "בננה", "category": "Produce"},
        {"product_id": 999, "units": 2, "size": "", "name_en": "Homogenized milk", "name_he": "חלב", "category": "Dairy & Eggs"},
    ]
    at.session_state["staged_date"] = today()
    at.session_state["staged_source"] = "receipt"
    at.session_state["receipt_scan_id"] = scan
    at.session_state["receipt_summary"] = {"lines": 2, "store": "", "total": 23.07, "rows_sum": 23.07, "missing": 0,
                                           "item_count": 3, "counted": 3, "sum_off": False, "count_off": False,
                                           "merged": 0, "deposits": 0}
    at.run()
    next(b for b in at.button if b.label == "✅ Save to pantry").click().run()
    assert not at.exception, at.exception
    row = app_env.receipt_scans("2000-01-01")[0]
    assert (row["outcome"], row["kept"], row["unticked"], row["rematched"], row["edited"]) == ("saved", 2, 0, 1, 1)


def test_admin_sees_scanner_reliability(app_env, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "a@b.com")
    for ok in (True, True, False):
        s = app_env.log_receipt_scan(source="pdf", chain="Osher Ad", lines=40, has_total=True, sum_ok=ok,
                                     gap_pct=0 if ok else 2.5, has_count=True, count_ok=ok, seconds=20, rechecked=not ok)
        app_env.finish_receipt_scan(s, "saved", kept=40, unticked=0, rematched=1, edited=1)
    app_env.log_receipt_scan(source="photo", lines=0, outcome="failed")
    at = _logged_in(app_env).run()
    assert not at.exception, at.exception
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Scans"] == "4" and metrics["Adds up exactly"] == "67%" and metrics["Lines users fixed"] == "5%"
    assert any("no receipt contents" in c.value for c in at.caption)
