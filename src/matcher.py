"""Suggest which existing product each incoming line belongs to.

Pipeline (cheapest first):
  1. Known alias   – this item code (receipts) or exact cleaned text was confirmed before → match
  2. Fuzzy         – string similarity against product names and aliases  → match if clearly best
  3. AI            – one batched gpt-4o-mini call for the unclear lines   → match or "new"
The user always confirms in the review table; confirmed lines become aliases (see PantryService).
"""
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from normalize import match_key

AUTO_MATCH = 0.90      # fuzzy score that is trusted without asking the AI
AUTO_MARGIN = 0.10     # ...as long as the runner-up is clearly worse
AI_FLOOR = 0.40        # below this, a candidate isn't worth showing to the AI
NO_AI_MATCH = 0.80     # without an API key, match only on strong similarity
CODE_PREFIX = "code "  # aliases made from receipt item codes (see receipt_reader.code_alias)


@dataclass
class Suggestion:
    product_id: int | None              # None = new product
    reason: str                          # 'alias' | 'similar' | 'ai' | 'new'
    score: float = 0.0
    candidates: list[int] = field(default_factory=list)   # best first, for the dropdown


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    # "חלב 3%" vs "חלב טרה 3%": all words of the shorter name appear in the longer one
    if ta and tb and (ta <= tb or tb <= ta):
        ratio = max(ratio, 0.75 + 0.2 * min(len(ta), len(tb)) / max(len(ta), len(tb)))
    return ratio


class ProductMatcher:
    def __init__(self, products: list[dict], aliases: dict[str, int], parser=None):
        self.products = {p["id"]: p for p in products}
        self.aliases = aliases
        self.parser = parser
        # every known spelling per product
        self.keys: dict[int, set[str]] = {pid: set() for pid in self.products}
        for p in products:
            for t in (p.get("name_en"), p.get("name_he")):
                k = match_key(t)
                if k:
                    self.keys[p["id"]].add(k)
        for alias, pid in aliases.items():
            # Item-code aliases only ever match exactly: "code 9000122" vs "code 9000123" are
            # nearly identical strings but completely different products.
            if pid in self.keys and not alias.startswith(CODE_PREFIX):
                self.keys[pid].add(alias)

    def _texts(self, item) -> list[str]:
        return [t for t in (item.get("raw_name"), item.get("name_he"), item.get("name_en")) if t]

    def _ranked(self, item) -> list[tuple[int, float]]:
        mine = {match_key(t) for t in self._texts(item)} - {""}
        scored = []
        for pid, keys in self.keys.items():
            s = max((_similarity(a, b) for a in mine for b in keys), default=0.0)
            if s > 0:
                scored.append((pid, s))
        scored.sort(key=lambda x: -x[1])
        return scored

    def suggest(self, items: list[dict]) -> list[Suggestion]:
        out: list[Suggestion | None] = []
        unsure: list[tuple[int, dict, list[tuple[int, float]]]] = []
        for i, item in enumerate(items):
            # 1. known alias: the item's code first (most reliable), then its texts
            keys = [match_key(item.get("code_key"))] if item.get("code_key") else []
            keys += [match_key(t) for t in self._texts(item)]
            hit = next((self.aliases[k] for k in keys if k in self.aliases and self.aliases[k] in self.products), None)
            ranked = self._ranked(item)
            cands = [pid for pid, _ in ranked[:5]]
            if hit:
                out.append(Suggestion(hit, "alias", 1.0, [hit] + [c for c in cands if c != hit]))
                continue
            # 2. clearly similar
            if ranked:
                best, s = ranked[0]
                second = ranked[1][1] if len(ranked) > 1 else 0.0
                if s >= AUTO_MATCH and s - second >= AUTO_MARGIN:
                    out.append(Suggestion(best, "similar", s, cands))
                    continue
            plausible = [(pid, s) for pid, s in ranked[:5] if s >= AI_FLOOR]
            if not plausible:
                out.append(Suggestion(None, "new", 0.0, cands))
                continue
            out.append(None)
            unsure.append((i, item, plausible))

        # 3. the AI decides the unclear ones in one call
        decisions = {}
        if unsure and self.parser is not None and getattr(self.parser, "available", False):
            requests = [{
                "i": i,
                "line": item.get("raw_name") or "",
                "en": item.get("name_en") or "",
                "he": item.get("name_he") or "",
                "candidates": [{"id": pid, "en": self.products[pid].get("name_en"),
                                "he": self.products[pid].get("name_he"),
                                "category": self.products[pid].get("category")} for pid, _ in plausible],
            } for i, item, plausible in unsure]
            decisions = self.parser.choose_products(requests)

        for i, item, plausible in unsure:
            cands = [pid for pid, _ in plausible]
            if i in decisions:
                pid = decisions[i]
                if pid in self.products:
                    out[i] = Suggestion(pid, "ai", dict(plausible).get(pid, 0.0), [pid] + [c for c in cands if c != pid])
                else:
                    out[i] = Suggestion(None, "new", 0.0, cands)
            else:
                best, s = plausible[0]
                out[i] = Suggestion(best, "similar", s, cands) if s >= NO_AI_MATCH else Suggestion(None, "new", s, cands)
        return out
