"""Turning messy item names into (a) a stable matching key and (b) a structured package size.

    "חלב תנובה 3% 1 ל"   -> key "חלב תנובה 3%",  size 1000 ml
    "Bananas 1.236 kg"   -> key "banana",        size 1236 g
    "ביצים L מארז 12"    -> key "ביצים l",        size 12 pieces

The key ignores package size (a 1 L and a 2 L carton are the same product);
the size is kept separately so the model can count a 2 L carton as two litres.
"""
import re
from dataclasses import dataclass

NIQQUD = re.compile(r"[֑-ׇ]")
FINAL_LETTERS = str.maketrans({"ך": "כ", "ם": "מ", "ן": "נ", "ף": "פ", "ץ": "צ"})
HEBREW = re.compile(r"[֐-׿]")

NUM = r"(\d+(?:[.,]\d+)?)"
# Unit words → (dimension, multiplier to base unit). Base units: ml, g, pc.
_UNITS = [
    (r'מ"ל|מ״ל|מל|ml|מיליליטר', "ml", 1),
    (r"ליטר|ליטרים|ל'|ל׳|ל|lt|ltr|liter|litre|l", "ml", 1000),
    (r'ק"ג|ק״ג|קג|קילו|kg|kilo', "g", 1000),
    (r"גרם|גר'|גר׳|גר|ג'|ג׳|gr|g|grams?", "g", 1),
    (r"יחידות|יח'|יח׳|יח|pcs|pc|units?|pieces?", "pc", 1),
]
# Letters on either side mean the "unit" is really part of a word (e.g. the ל in "לחם").
_EDGE = r"(?<![\w֐-׿])"
_TAIL = r"(?![\w֐-׿])"
_SIZE_RE = [(re.compile(_EDGE + NUM + r"\s*(?:" + pat + r")" + _TAIL, re.IGNORECASE), dim, mult)
            for pat, dim, mult in _UNITS]
_PACK_RE = [
    re.compile(_EDGE + r"(?:מארז|מגש|חבילה|pack(?:\s+of)?)\s*(\d+)" + _TAIL, re.IGNORECASE),
    re.compile(_EDGE + r"(\d+)\s*(?:pack|-pack|ב?מארז)" + _TAIL, re.IGNORECASE),
    re.compile(_EDGE + r"[x×*]\s*(\d+)" + _TAIL, re.IGNORECASE),
    re.compile(_EDGE + r"(\d+)\s*[x×*]" + r"(?=\s|$)", re.IGNORECASE),
]


@dataclass
class Size:
    amount: float      # in base units
    unit: str          # 'ml' | 'g' | 'pc'

    def label(self, lang: str = "en") -> str:
        a, u = self.amount, self.unit
        if u == "ml":
            return (f"{a / 1000:g} L" if a >= 1000 else f"{a:g} ml") if lang == "en" else \
                   (f'{a / 1000:g} ל\'' if a >= 1000 else f'{a:g} מ"ל')
        if u == "g":
            return (f"{a / 1000:g} kg" if a >= 1000 else f"{a:g} g") if lang == "en" else \
                   (f'{a / 1000:g} ק"ג' if a >= 1000 else f"{a:g} גרם")
        return f"{a:g} pcs" if lang == "en" else f"{a:g} יח'"


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def extract_size(text: str | None) -> tuple[str, Size | None]:
    """Remove the package size from a name and return (name_without_size, Size or None).

    A pack count multiplies a unit size ("6 x 1 L" → 6000 ml); a pack count alone is pieces.
    """
    if not text:
        return "", None
    s = str(text)
    size = None
    for rx, dim, mult in _SIZE_RE:
        m = rx.search(s)
        if m:
            size = Size(_num(m.group(1)) * mult, dim)
            s = s[:m.start()] + " " + s[m.end():]
            break
    for rx in _PACK_RE:
        m = rx.search(s)
        if m:
            count = _num(m.group(1))
            if size and size.unit in ("ml", "g"):
                size = Size(size.amount * count, size.unit)
            elif size is None and count > 0:
                size = Size(count, "pc")
            s = s[:m.start()] + " " + s[m.end():]
            break
    return re.sub(r"\s+", " ", s).strip(" -,.;"), size


def _singular(word: str) -> str:
    if not word.isascii() or len(word) <= 3:
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("oes", "ches", "shes", "xes", "sses")):
        return word[:-2]
    if word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def match_key(text: str | None) -> str:
    """Canonical key for matching: no size, no punctuation/niqqud, final letters unified, singular English."""
    name, _ = extract_size(text)
    t = NIQQUD.sub("", name.lower()).translate(FINAL_LETTERS)
    t = re.sub(r"[\"'׳״`.,;:!?()\[\]{}/\\_\-–—+*#]", " ", t)   # keep % (3% vs 1% milk matter)
    words = [_singular(w) for w in t.split()]
    return " ".join(words)


def has_hebrew(text: str | None) -> bool:
    return bool(text and HEBREW.search(text))


def to_ref_units(packages: float, size: Size | None, ref_amount: float | None, ref_unit: str | None) -> float:
    """How many 'reference packages' a purchase is worth.

    2 × 2 L with a 1 L reference → 4.0.  Unknown size, or a different dimension than the
    reference (weight vs volume), falls back to counting packages.
    """
    packages = max(0.0, float(packages or 0)) or 1.0
    if size and ref_amount and ref_unit == size.unit and ref_amount > 0:
        return packages * size.amount / ref_amount
    return packages


def parse_size_text(text: str | None) -> Size | None:
    """A size typed on its own, e.g. "1 L", "500 גרם", "12 יח'" (a bare number means pieces)."""
    text = (text or "").strip()
    if not text:
        return None
    _, size = extract_size("x " + text)
    if size is None:
        try:
            n = float(text.replace(",", "."))
            return Size(n, "pc") if n > 0 else None
        except ValueError:
            return None
    return size
