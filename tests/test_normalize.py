import pytest

from normalize import Size, extract_size, match_key, to_ref_units


@pytest.mark.parametrize("text,name,amount,unit", [
    ("חלב תנובה 3% 1 ל", "חלב תנובה 3%", 1000, "ml"),
    ("חלב טרה 3% 1 ליטר", "חלב טרה 3%", 1000, "ml"),
    ('יוגורט 500 מ"ל', "יוגורט", 500, "ml"),
    ("Milk 1.5L", "Milk", 1500, "ml"),
    ("Cola 6 x 1.5 L", "Cola", 9000, "ml"),
    ("אורז פרסי 1 ק\"ג", "אורז פרסי", 1000, "g"),
    ("גבינה צהובה 200 גרם", "גבינה צהובה", 200, "g"),
    ("Bananas 1,236 kg", "Bananas", 1236, "g"),
    ("ביצים L מארז 12", "ביצים L", 12, "pc"),
    ("Eggs 12 pcs", "Eggs", 12, "pc"),
    ("לחם אחיד פרוס", "לחם אחיד פרוס", None, None),   # the ל in לחם is not a litre
    ("חלב 3%", "חלב 3%", None, None),
])
def test_extract_size(text, name, amount, unit):
    n, size = extract_size(text)
    assert n == name
    if amount is None:
        assert size is None
    else:
        assert size == Size(amount, unit)


def test_match_key_ignores_size_and_noise():
    assert match_key("חלב תנובה 3% 1 ל") == match_key("חלב  תנובה 3%  2 ליטר") == "חלב תנובה 3%"
    assert match_key("Bananas") == match_key("banana") == "banana"
    assert match_key("Tomatoes 1kg") == "tomato"
    assert match_key("Cherries") == "cherry"
    assert match_key("קוטג'") == match_key("קוטג") == "קוטג"
    assert match_key("שמן קנולה") == match_key("שמנ קנולה")        # final letters unified
    assert match_key("1% milk") != match_key("3% milk")             # variants stay separate


def test_reference_units():
    one_litre = (1000, "ml")
    assert to_ref_units(2, Size(2000, "ml"), *one_litre) == 4.0
    assert to_ref_units(1, Size(500, "ml"), *one_litre) == 0.5
    assert to_ref_units(3, None, *one_litre) == 3.0                   # unknown size → packages
    assert to_ref_units(1, Size(1000, "g"), *one_litre) == 1.0        # different dimension → packages
    assert to_ref_units(1, Size(1236, "g"), 1000, "g") == pytest.approx(1.236)


def test_size_labels():
    assert Size(1500, "ml").label() == "1.5 L"
    assert Size(500, "g").label() == "500 g"
    assert Size(12, "pc").label("he") == "12 יח'"
