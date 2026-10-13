"""All pantry business logic. The UI and background jobs call these; nobody else writes SQL
for inventory, history or the shopping list.

Learning happens in exactly three places:
  * record_purchase  – a rebuy closes the previous stock row's cycle ('repurchase')
  * mark_depleted    – "ran out" closes the cycle ('depleted') and adds the item to the list
  * mark_still_going – records a censoring point (confirmed_until); purchase_date is NOT touched
"""
from dataclasses import dataclass
from datetime import date, datetime

from config import DATE_FMT, RESTOCK_THRESHOLD, today as app_today
from db import Database, normalize_key
from engine import PantryDepletionEngine


def _to_date(d) -> date:
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return datetime.strptime(str(d), DATE_FMT).date()


def _s(d: date) -> str:
    return d.strftime(DATE_FMT)


@dataclass
class PurchaseResult:
    status: str          # 'new' | 'restocked' | 'added_units' | 'backdated'
    item_key: str
    learned_interval: float | None = None


class PantryService:
    def __init__(self, db: Database, engine: PantryDepletionEngine):
        self.db = db
        self.engine = engine

    # ======================================================================
    # Internal helpers (take an open connection so callers can batch)
    # ======================================================================
    @staticmethod
    def _find_stock(conn, household_id, key, name_he):
        return conn.execute(
            """SELECT * FROM inventory
               WHERE household_id = ?
                 AND (item_key = ? OR (TRIM(COALESCE(?, '')) != '' AND LOWER(item_name_he) = LOWER(TRIM(?))))
               ORDER BY purchase_date DESC, id DESC LIMIT 1""",
            (household_id, key, name_he, name_he),
        ).fetchone()

    @staticmethod
    def _log_interval(conn, household_id, key, category, start, end, units, kind, user):
        days = (end - start).days
        if kind == "depleted":
            days = max(1, days)  # ran out the same day still counts as ~1 day
        if days <= 0:
            return None
        per_unit = days / max(1, int(units or 1))
        conn.execute(
            """INSERT INTO purchase_history
               (household_id, item_key, category, interval_days, units, kind, recorded_at, recorded_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (household_id, key, category, per_unit, units, kind, _s(end), user),
        )
        return per_unit

    def _learned_scale(self, conn, household_id, key, category):
        item = [r[0] for r in conn.execute(
            "SELECT interval_days FROM purchase_history WHERE household_id = ? AND item_key = ?",
            (household_id, key))]
        cat = [r[0] for r in conn.execute(
            "SELECT interval_days FROM purchase_history WHERE household_id = ? AND category = ?",
            (household_id, category))]
        return self.engine.estimate_item_scale(category, item, cat)

    @staticmethod
    def _shopping_keys(conn, household_id) -> set[str]:
        return {r[0] for r in conn.execute(
            "SELECT item_key FROM shopping_list WHERE household_id = ?", (household_id,))}

    def _add_to_list(self, conn, household_id, items, source, user, on_date) -> int:
        existing = self._shopping_keys(conn, household_id)
        next_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) FROM shopping_list WHERE household_id = ?", (household_id,)
        ).fetchone()[0]
        added = 0
        for it in items:
            key = normalize_key(it.get("name_en"), it.get("name_he"))
            if not key or key in existing:
                continue
            next_order += 1
            conn.execute(
                """INSERT INTO shopping_list
                   (household_id, item_key, item_name_en, item_name_he, category, source, sort_order, added_at, added_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (household_id, key, it.get("name_en"), it.get("name_he"), it.get("category"),
                 source, next_order, _s(on_date), user),
            )
            existing.add(key)
            added += 1
        return added

    # ======================================================================
    # Purchases & consumption events
    # ======================================================================
    def record_purchase(self, household_id, name_en, name_he, category, units, purchase_date, user,
                        conn=None) -> PurchaseResult:
        name_en = (name_en or "").strip() or (name_he or "").strip()
        name_he = (name_he or "").strip() or name_en
        if not name_en:
            raise ValueError("Item needs a name.")
        units = max(1, int(units or 1))
        p_date = _to_date(purchase_date)
        if p_date > app_today():
            raise ValueError("Purchase date can't be in the future.")
        key = normalize_key(name_en, name_he)

        if conn is None:
            with self.db.connect() as own:
                return self.record_purchase(household_id, name_en, name_he, category, units, p_date, user, own)

        conn.execute(
            """INSERT INTO purchases
               (household_id, item_key, category, item_name_en, item_name_he, units, purchase_date, recorded_by, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (household_id, key, category, name_en, name_he, units, _s(p_date), user, datetime.now().isoformat(timespec="seconds")),
        )

        existing = self._find_stock(conn, household_id, key, name_he)
        learned = None
        if existing:
            prev_date = _to_date(existing["purchase_date"])
            delta = (p_date - prev_date).days
            if delta < 0:
                # Logging an older receipt: keep it in the ledger, don't disturb current stock.
                return PurchaseResult("backdated", key)
            if delta == 0:
                conn.execute("UPDATE inventory SET units = units + ? WHERE id = ?", (units, existing["id"]))
                conn.execute("DELETE FROM shopping_list WHERE household_id = ? AND item_key = ?", (household_id, key))
                return PurchaseResult("added_units", key)
            learned = self._log_interval(conn, household_id, key, existing["category"] or category,
                                         prev_date, p_date, existing["units"], "repurchase", user)
            conn.execute("DELETE FROM inventory WHERE id = ?", (existing["id"],))

        scale = self._learned_scale(conn, household_id, key, category)
        conn.execute(
            """INSERT INTO inventory
               (household_id, item_key, category, item_name_en, item_name_he, units, purchase_date, user_lambda)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (household_id, key, category, name_en, name_he, units, _s(p_date), scale),
        )
        # Bought it → it no longer belongs on the shopping list.
        conn.execute("DELETE FROM shopping_list WHERE household_id = ? AND item_key = ?", (household_id, key))
        return PurchaseResult("restocked" if existing else "new", key, learned)

    def record_purchases(self, household_id, items, purchase_date, user) -> list[PurchaseResult]:
        """Batch version for the Quick Add list; all-or-nothing."""
        results = []
        with self.db.connect() as conn:
            for it in items:
                results.append(self.record_purchase(
                    household_id, it.get("name_en"), it.get("name_he"), it.get("category"),
                    it.get("units", 1), purchase_date, user, conn))
        return results

    def mark_depleted(self, household_id, inventory_ids, on_date, user, add_to_list=True) -> int:
        on_date = _to_date(on_date)
        done = 0
        with self.db.connect() as conn:
            for inv_id in inventory_ids:
                row = conn.execute("SELECT * FROM inventory WHERE id = ? AND household_id = ?",
                                   (inv_id, household_id)).fetchone()
                if not row:
                    continue
                self._log_interval(conn, household_id, row["item_key"], row["category"],
                                   _to_date(row["purchase_date"]), on_date, row["units"], "depleted", user)
                if add_to_list:
                    self._add_to_list(conn, household_id, [{
                        "name_en": row["item_name_en"], "name_he": row["item_name_he"], "category": row["category"],
                    }], "ran_out", user, on_date)
                conn.execute("DELETE FROM inventory WHERE id = ?", (inv_id,))
                done += 1
        return done

    def mark_still_going(self, household_id, inventory_ids, on_date) -> int:
        on = _s(_to_date(on_date))
        with self.db.connect() as conn:
            n = 0
            for inv_id in inventory_ids:
                n += conn.execute(
                    "UPDATE inventory SET confirmed_until = ?, last_alerted = NULL WHERE id = ? AND household_id = ?",
                    (on, inv_id, household_id)).rowcount
        return n

    # ======================================================================
    # Plain inventory edits (corrections – no learning)
    # ======================================================================
    def delete_items(self, household_id, inventory_ids):
        with self.db.connect() as conn:
            conn.executemany("DELETE FROM inventory WHERE id = ? AND household_id = ?",
                             [(i, household_id) for i in inventory_ids])

    def update_item(self, household_id, inventory_id, name_en, name_he, category, units):
        name_en = (name_en or "").strip() or (name_he or "").strip()
        name_he = (name_he or "").strip() or name_en
        if not name_en:
            raise ValueError("Item needs a name.")
        with self.db.connect() as conn:
            conn.execute(
                """UPDATE inventory SET item_name_en = ?, item_name_he = ?, item_key = ?, category = ?, units = ?
                   WHERE id = ? AND household_id = ?""",
                (name_en, name_he, normalize_key(name_en, name_he), category, max(1, int(units)),
                 inventory_id, household_id))

    def merge_items(self, household_id, father_id, child_ids):
        child_ids = [c for c in child_ids if c != father_id]
        if not child_ids:
            return
        with self.db.connect() as conn:
            marks = ",".join("?" * len(child_ids))
            extra = conn.execute(
                f"SELECT COALESCE(SUM(units), 0) FROM inventory WHERE household_id = ? AND id IN ({marks})",
                (household_id, *child_ids)).fetchone()[0]
            conn.execute("UPDATE inventory SET units = units + ? WHERE id = ? AND household_id = ?",
                         (extra, father_id, household_id))
            conn.execute(f"DELETE FROM inventory WHERE household_id = ? AND id IN ({marks})",
                         (household_id, *child_ids))

    # ======================================================================
    # Reads
    # ======================================================================
    def get_pantry(self, household_id, as_of=None) -> list[dict]:
        as_of = _to_date(as_of or app_today())
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM inventory WHERE household_id = ?", (household_id,)).fetchall()
            on_list = self._shopping_keys(conn, household_id)

        items = []
        for r in rows:
            p_date = _to_date(r["purchase_date"])
            days = max(0, (as_of - p_date).days)
            confirmed = None
            if r["confirmed_until"]:
                confirmed = max(0, (_to_date(r["confirmed_until"]) - p_date).days)
            units = max(1, r["units"] or 1)
            per_unit = r["user_lambda"] if r["user_lambda"] else self.engine.prior_scale(r["category"])
            items.append({
                "id": r["id"],
                "item_key": r["item_key"],
                "name_en": r["item_name_en"],
                "name_he": r["item_name_he"],
                "category": r["category"],
                "units": units,
                "purchase_date": r["purchase_date"],
                "confirmed_until": r["confirmed_until"],
                "days_elapsed": days,
                "days_confirmed": confirmed,
                "per_unit_scale": per_unit,
                "effective_scale": self.engine.effective_scale(r["category"], per_unit, units),
                "prob_depleted": self.engine.depletion_probability(r["category"], days, per_unit, units, confirmed),
                "on_shopping_list": r["item_key"] in on_list,
                "last_alerted": r["last_alerted"],
            })
        return items

    @staticmethod
    def restock_candidates(pantry_items, threshold=RESTOCK_THRESHOLD) -> list[dict]:
        """High-risk items that aren't already on the shopping list."""
        return [i for i in pantry_items if i["prob_depleted"] >= threshold and not i["on_shopping_list"]]

    def item_learning_stats(self, household_id, item_key, category) -> dict:
        with self.db.connect() as conn:
            item = [r[0] for r in conn.execute(
                "SELECT interval_days FROM purchase_history WHERE household_id = ? AND item_key = ?",
                (household_id, item_key))]
            cat = [r[0] for r in conn.execute(
                "SELECT interval_days FROM purchase_history WHERE household_id = ? AND category = ?",
                (household_id, category))]
        return {
            "item_cycles": len(item),
            "item_mean_per_unit": sum(item) / len(item) if item else None,
            "category_cycles": len(cat),
            "prior_scale": self.engine.prior_scale(category),
            "prior_key": self.engine.category_params(category)["prior_key"],
            "rho": self.engine.rho(category),
            "learned_scale": self.engine.estimate_item_scale(category, item, cat),
        }

    def recent_purchases(self, household_id, days=60, as_of=None) -> list[dict]:
        as_of = _to_date(as_of or app_today())
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT purchase_date, item_name_en, item_name_he, category, units, recorded_by
                   FROM purchases WHERE household_id = ? AND purchase_date >= date(?, ?)
                   ORDER BY purchase_date DESC, id DESC""",
                (household_id, _s(as_of), f"-{int(days)} days")).fetchall()
        return [dict(r) for r in rows]

    # ======================================================================
    # Shopping list
    # ======================================================================
    def get_shopping_list(self, household_id) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM shopping_list WHERE household_id = ? ORDER BY sort_order, id",
                                (household_id,)).fetchall()
        return [dict(r) for r in rows]

    def add_to_shopping_list(self, household_id, items, source, user, on_date=None) -> int:
        """items: dicts with name_en, name_he, category. Skips anything already on the list."""
        with self.db.connect() as conn:
            return self._add_to_list(conn, household_id, items, source, user, _to_date(on_date or app_today()))

    def remove_from_shopping_list(self, household_id, shop_id):
        with self.db.connect() as conn:
            conn.execute("DELETE FROM shopping_list WHERE id = ? AND household_id = ?", (shop_id, household_id))

    def update_shopping_item(self, household_id, shop_id, name_en, name_he, category):
        name_en = (name_en or "").strip() or (name_he or "").strip()
        name_he = (name_he or "").strip() or name_en
        with self.db.connect() as conn:
            conn.execute(
                """UPDATE shopping_list SET item_name_en = ?, item_name_he = ?, item_key = ?, category = ?
                   WHERE id = ? AND household_id = ?""",
                (name_en, name_he, normalize_key(name_en, name_he), category, shop_id, household_id))

    def reorder_shopping_list(self, household_id, ordered_ids):
        """Must be given the FULL list (the UI disables reordering while filtered)."""
        with self.db.connect() as conn:
            conn.executemany("UPDATE shopping_list SET sort_order = ? WHERE id = ? AND household_id = ?",
                             [(i, sid, household_id) for i, sid in enumerate(ordered_ids)])

    def bought_from_list(self, household_id, shop_id, units, user, on_date=None) -> PurchaseResult | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM shopping_list WHERE id = ? AND household_id = ?",
                               (shop_id, household_id)).fetchone()
            if not row:
                return None
            result = self.record_purchase(household_id, row["item_name_en"], row["item_name_he"], row["category"],
                                          units, on_date or app_today(), user, conn)
            conn.execute("DELETE FROM shopping_list WHERE id = ?", (shop_id,))
            return result

    # ======================================================================
    # Alerts
    # ======================================================================
    def mark_alerted(self, household_id, inventory_ids, on_date=None):
        on = _s(_to_date(on_date or app_today()))
        with self.db.connect() as conn:
            conn.executemany("UPDATE inventory SET last_alerted = ? WHERE id = ? AND household_id = ?",
                             [(on, i, household_id) for i in inventory_ids])
