"""Turns a pasted grocery list into structured items with an LLM.

The OpenAI client is created lazily, so the rest of the app still runs when no key is set.
"""
import json
import logging
import re

from config import get_secret

log = logging.getLogger(__name__)
MAX_INPUT_CHARS = 4000
MODEL = "gpt-4o-mini"


HEBREW = re.compile(r"[\u0590-\u05FF]")


def has_hebrew(text: str | None) -> bool:
    return bool(text and HEBREW.search(text))


def normalize_name_pair(name_en: str | None, name_he: str | None) -> tuple[str, str]:
    """Put each name in its right field and blank out a name that's just a copy of the other.

    ('חלב', '')      -> ('', 'חלב')     Hebrew typed into the English box
    ('Milk', 'Milk') -> ('Milk', '')    the old "copy the other name" fallback
    ('חלב', 'חלב')   -> ('', 'חלב')
    """
    en, he = (name_en or "").strip(), (name_he or "").strip()
    if en and he and en.lower() == he.lower():
        if has_hebrew(en):
            en = ""
        else:
            he = ""
    if has_hebrew(en) and not he:
        en, he = "", en
    if he and not has_hebrew(he) and not en:
        en, he = he, ""
    return en, he


class TextListParser:
    def __init__(self):
        self._client = None

    @property
    def available(self) -> bool:
        return bool(get_secret("OPENAI_API_KEY"))

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(api_key=get_secret("OPENAI_API_KEY"))
        return self._client

    def translate_pairs(self, pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """Fill in whichever grocery name is missing (English <-> Hebrew) in one API call.

        Pairs that already have both names are returned unchanged. Without an API key,
        or if the call fails, the missing name falls back to a copy of the other one.
        """
        pairs = [normalize_name_pair(en, he) for en, he in pairs]
        todo = [i for i, (en, he) in enumerate(pairs) if bool(en) != bool(he)]
        result = list(pairs)
        if todo and self.available:
            request = [{"i": i, "en": pairs[i][0], "he": pairs[i][1]} for i in todo]
            prompt = (
                "These are grocery item names from an Israeli household. For each entry, fill in the "
                "missing name: 'en' is the natural English product name, 'he' is the natural Hebrew name "
                "as it would appear on an Israeli shopping list. Keep brand names and percentages. "
                'Return JSON {"items": [{"i": <same i>, "en": "...", "he": "..."}]}.\n'
                + json.dumps(request, ensure_ascii=False))
            try:
                resp = self._get_client().chat.completions.create(
                    model=MODEL, response_format={"type": "json_object"},
                    messages=[{"role": "user", "content": prompt}], temperature=0.0)
                for item in json.loads(resp.choices[0].message.content).get("items", []):
                    i = item.get("i")
                    if i in todo:
                        en = pairs[i][0] or str(item.get("en") or "").strip()
                        he = pairs[i][1] or str(item.get("he") or "").strip()
                        result[i] = (en, he)
            except Exception:
                log.exception("Name translation failed; falling back to copies")
        # Anything still missing: copy the other name so nothing is ever blank.
        return [(en or he, he or en) for en, he in result]

    def parse(self, raw_text: str, categories: list[dict]) -> list[dict]:
        """Returns items with name_en, name_he, category (a valid household category or None), units.

        Raises RuntimeError with a user-facing message on failure.
        """
        raw_text = raw_text.strip()[:MAX_INPUT_CHARS]
        if not raw_text:
            return []
        if not self.available:
            raise RuntimeError("Quick Add needs an OPENAI_API_KEY secret.")

        valid = [c["en"] for c in categories]
        cats_formatted = "\n".join(f'- "{c["en"]}" (Hebrew: {c["he"]})' for c in categories)
        prompt = f"""Convert this grocery list (Hebrew and/or English) into structured items.
Each line is usually one item.
- "units" is the number of packages ("2 milk" → 2). A package SIZE like "1 ליטר", "500 גרם", "1.5L" is NOT units.
- "line" is the item's original text exactly as written, including any size.
- Names are generic product names without brand or package size (e.g. "חלב תנובה 3% 1 ל" → "3% Milk" / "חלב 3%").
  Keep meaningful variants such as fat %, flavor or type.

LIST:
{raw_text}

Use ONLY these category keys (copy the English key exactly):
{cats_formatted}

Return JSON: {{"items": [{{"line": "original text", "item_name_en": "English name", "item_name_he": "Hebrew name",
"category": "Category key", "units": 1}}]}}"""

        try:
            resp = self._get_client().chat.completions.create(
                model=MODEL,
                response_format={"type": "json_object"},
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
            )
            data = json.loads(resp.choices[0].message.content)
        except Exception as e:
            log.exception("Quick Add parse failed")
            raise RuntimeError(f"Couldn't read the list ({type(e).__name__}). Please try again.") from e

        items = []
        for it in data.get("items", []):
            en = str(it.get("item_name_en") or "").strip()
            he = str(it.get("item_name_he") or "").strip()
            if not (en or he):
                continue
            cat = it.get("category")
            try:
                units = max(1, int(it.get("units") or 1))
            except (TypeError, ValueError):
                units = 1
            items.append({
                "raw_name": str(it.get("line") or "").strip() or (he or en),
                "name_en": en or he,
                "name_he": he or en,
                "category": cat if cat in valid else None,  # user must pick one before committing
                "units": units,
            })
        return items

    def describe_lines(self, lines: list[str], categories: list[dict]) -> list[dict]:
        """Name and categorize receipt lines, strictly one answer per line (by index).

        Unlike `parse`, the model can't merge, split or drop lines here: anything it skips falls
        back to the printed text with no category, so the user still sees every line.
        Returns [{"name_en", "name_he", "category"}] aligned with `lines`.
        """
        out = [{"name_en": "", "name_he": t, "category": None} for t in lines]
        if not lines or not self.available:
            return out
        valid = [c["en"] for c in categories]
        cats_formatted = "\n".join(f'- "{c["en"]}" (Hebrew: {c["he"]})' for c in categories)
        prompt = (
            "These lines come from an Israeli supermarket receipt. Receipt printers cut descriptions at "
            "~20 characters, so words may be truncated (e.g. 'גבינה גאודה מגוררת ח' = grated gouda cheese).\n"
            "For each line give a short generic product name in English and in Hebrew, WITHOUT brand or "
            "package size, keeping meaningful variants (fat %, flavor, type): "
            "'חלב 3% רמת הגולן 1 ל' → '3% Milk' / 'חלב 3%'. 'חזה עוף טרי פרימה' → 'Chicken breast' / 'חזה עוף'.\n"
            f"Use ONLY these category keys (copy the English key exactly):\n{cats_formatted}\n"
            'Return JSON {"items": [{"i": <same i>, "en": "...", "he": "...", "category": "..."}]} '
            "with exactly one entry per line.\n"
            + json.dumps([{"i": i, "line": t} for i, t in enumerate(lines)], ensure_ascii=False))
        try:
            resp = self._get_client().chat.completions.create(
                model=MODEL, response_format={"type": "json_object"},
                messages=[{"role": "user", "content": prompt}], temperature=0.0)
            data = json.loads(resp.choices[0].message.content)
        except Exception:
            log.exception("Naming receipt lines failed; showing them as printed")
            return out
        for it in data.get("items", []):
            i = it.get("i")
            if not isinstance(i, int) or not 0 <= i < len(lines):
                continue
            en, he = normalize_name_pair(str(it.get("en") or ""), str(it.get("he") or ""))
            cat = it.get("category")
            out[i] = {"name_en": en, "name_he": he or lines[i], "category": cat if cat in valid else None}
        return out

    def choose_products(self, requests: list[dict]) -> dict[int, int | None]:
        """For each unclear line, pick one of its candidate products or None (= a new product).

        requests: [{"i", "line", "en", "he", "candidates": [{"id","en","he","category"}]}]
        Returns {i: product_id or None}. Empty dict on any failure (callers fall back to similarity).
        """
        if not requests or not self.available:
            return {}
        prompt = (
            "You match grocery lines to a household's existing products.\n"
            "Rules: the SAME product regardless of brand or package size (Tnuva vs Tara milk, 1 L vs 2 L). "
            "DIFFERENT products when a meaningful variant differs (3% vs 1% milk, plain vs strawberry yogurt, "
            "white vs whole-wheat bread). If none of the candidates is the same product, answer null.\n"
            'Return JSON {"matches": [{"i": <i>, "product_id": <candidate id or null>}]}.\n'
            + json.dumps(requests, ensure_ascii=False))
        try:
            resp = self._get_client().chat.completions.create(
                model=MODEL, response_format={"type": "json_object"},
                messages=[{"role": "user", "content": prompt}], temperature=0.0)
            data = json.loads(resp.choices[0].message.content)
        except Exception:
            log.exception("Product matching failed; falling back to similarity")
            return {}
        allowed = {r["i"]: {c["id"] for c in r["candidates"]} for r in requests}
        out = {}
        for m in data.get("matches", []):
            i, pid = m.get("i"), m.get("product_id")
            if i in allowed:
                out[i] = pid if pid in allowed[i] else None
        return out
