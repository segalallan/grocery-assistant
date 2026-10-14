"""All pantry business logic. The UI and background jobs call these; nobody else writes SQL
for inventory, history or the shopping list.

Items are *products* (a household's "3% Milk", whatever brand or package size). Every row in
inventory / purchases / history / shopping list points at a product through item_key "prod:<id>".
Raw names ("חלב תנובה 3% 1 ל") are remembered as aliases so the same text maps to the same product.

Amounts: each purchase is converted to "reference units" (eff_units) using the product's reference
package size, so 2 x 2 L milk with a 1 L reference counts as 4 units. Learning is per reference unit.

Learning happens in exactly three places:
  * record_purchase  – a rebuy closes the previous stock row's cycle ('repurchase')
  * mark_depleted    – "ran out" closes the cycle ('depleted') and adds the item to the list
  * mark_still_going – records a censoring point (confirmed_until); purchase_date is NOT touched
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from config import DATE_FMT, RESTOCK_THRESHOLD, today as app_today
from db import Database, product_id_of, product_key
from engine import PantryDepletionEngine
from normalize import Size, extract_size, match_key, to_ref_units


def _to_date(d) -> date:
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return datetime.strptime(str(d), DATE_FMT).date()


def _s(d: date) -> str:
    return d.strftime(DATE_FMT)


def _as_size(size) -> Size | None:
    if size is None or isinstance(size, Size):
        return size
    if isinstance(size, dict) and size.get("amount") and size.get("unit"):
        return Size(float(size["amount"]), size["unit"])
    return None


def _row_units(row) -> float:
    """Amount in reference units for a stock/ledger row (old rows only have a package count)."""
    eff = row["eff_units"] if "eff_units" in row.keys() else None
    return float(eff) if eff else float(max(1, row["units"] or 1))


@dataclass
class PurchaseResult:
    status: str          # 'new' | 'restocked' | 'added_units' | 'backdated'
    item_key: str
    learned_interval: float | None = None
    product_id: int | None = None


class PantryService:
    def __init__(self, db: Database, engine: PantryDepletionEngine):
        self.db = db
        self.engine = engine

    # ======================================================================
    # Products & aliases
    # ======================================================================
    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _product(conn, household_id, product_id):
        return conn.execute("SELECT * FROM products WHERE id = ? AND household_id = ?",
                            (product_id, household_id)).fetchone()

    @staticmethod
    def _add_aliases(conn, household_id, product_id, texts):
        for t in texts:
            k = match_key(t)
            if k:
                # Latest confirmation wins: if this text used to mean another product, re-point it.
                conn.execute("INSERT INTO product_aliases (household_id, alias, product_id) VALUES (?, ?, ?) "
                             "ON CONFLICT (household_id, alias) DO UPDATE SET product_id = excluded.product_id",
                             (household_id, k, product_id))

    @staticmethod
    def _by_alias(conn, household_id, texts):
        for t in texts:
            k = match_key(t)
            if not k:
                continue
            row = conn.execute("SELECT product_id FROM product_aliases WHERE household_id = ? AND alias = ?",
                               (household_id, k)).fetchone()
            if row:
                return row[0]
        return None

    def _create_product(self, conn, household_id, name_en, name_he, category, size: Size | None):
        return conn.execute(
            """INSERT INTO products (household_id, name_en, name_he, category, ref_amount, ref_unit, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id""",
            (household_id, name_en, name_he, category,
             size.amount if size else None, size.unit if size else None, self._now())).fetchone()[0]

    def resolve_product(self, conn, household_id, name_en, name_he, category, size=None,
                        product_id=None, create=True, extra_aliases=()):
        """Product id for these names: the given id, else a known alias, else a new product."""
        pid = product_id
        if pid is None:
            pid = self._by_alias(conn, household_id, [name_en, name_he, *extra_aliases])
        if pid is None:
            if not create:
                return None
            pid = self._create_product(conn, household_id, name_en, name_he, category, size)
        self._add_aliases(conn, household_id, pid, [name_en, name_he, *extra_aliases])
        return pid

    def get_products(self, household_id) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT p.*, (SELECT COUNT(*) FROM product_aliases a WHERE a.product_id = p.id) AS alias_count
                   FROM products p WHERE household_id = ? ORDER BY LOWER(COALESCE(name_en, name_he))""",
                (household_id,)).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]

    def get_aliases(self, household_id) -> dict[str, int]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT alias, product_id FROM product_aliases WHERE household_id = ?",
                                (household_id,)).fetchall()
        return {r[0]: r[1] for r in rows}

    def learn_aliases(self, household_id, product_id, texts):
        with self.db.connect() as conn:
            self._add_aliases(conn, household_id, product_id, texts)

    def update_product(self, household_id, product_id, name_en, name_he, category, conn=None):
        """Rename/recategorize a product everywhere it appears (and remember the new names)."""
        if conn is None:
            with self.db.connect() as own:
                return self.update_product(household_id, product_id, name_en, name_he, category, own)
        name_en = (name_en or "").strip() or (name_he or "").strip()
        name_he = (name_he or "").strip() or name_en
        key = product_key(product_id)
        conn.execute("UPDATE products SET name_en = ?, name_he = ?, category = ? WHERE id = ? AND household_id = ?",
                     (name_en, name_he, category, product_id, household_id))
        for table in ("inventory", "shopping_list"):
            conn.execute(f"UPDATE {table} SET item_name_en = ?, item_name_he = ?, category = ? "
                         f"WHERE household_id = ? AND item_key = ?", (name_en, name_he, category, household_id, key))
        self._add_aliases(conn, household_id, product_id, [name_en, name_he])

    def set_reference_size(self, household_id, product_id, amount: float, unit: str):
        """User override of a product's reference package size.

        Everything already stored in reference units is rescaled, so predictions don't jump:
        amounts shrink by old/new and learned per-unit durations grow by new/old.
        """
        if not amount or amount <= 0 or unit not in ("ml", "g", "pc"):
            raise ValueError("Invalid size.")
        key = product_key(product_id)
        with self.db.connect() as conn:
            p = self._product(conn, household_id, product_id)
            if not p:
                return
            old_amount, old_unit = p["ref_amount"], p["ref_unit"]
            conn.execute("UPDATE products SET ref_amount = ?, ref_unit = ? WHERE id = ?", (amount, unit, product_id))
            if old_amount and old_unit == unit:
                f = old_amount / amount            # e.g. 1 L → 2 L reference: amounts halve
                conn.execute("UPDATE inventory SET eff_units = COALESCE(eff_units, units) * ?, "
                             "user_lambda = user_lambda / ? WHERE household_id = ? AND item_key = ?",
                             (f, f, household_id, key))
                conn.execute("UPDATE purchases SET eff_units = COALESCE(eff_units, units) * ? "
                             "WHERE household_id = ? AND item_key = ?", (f, household_id, key))
                conn.execute("UPDATE purchase_history SET eff_units = COALESCE(eff_units, units) * ?, "
                             "interval_days = interval_days / ? WHERE household_id = ? AND item_key = ?",
                             (f, f, household_id, key))
            else:
                # First reference (or a different kind of measure): recompute amounts from stored sizes.
                for table in ("inventory", "purchases"):
                    rows = conn.execute(f"SELECT id, units, size_amount, size_unit FROM {table} "
                                        f"WHERE household_id = ? AND item_key = ?", (household_id, key)).fetchall()
                    for r in rows:
                        size = Size(r["size_amount"], r["size_unit"]) if r["size_amount"] else None
                        conn.execute(f"UPDATE {table} SET eff_units = ? WHERE id = ?",
                                     (to_ref_units(r["units"], size, amount, unit), r["id"]))

    def merge_products(self, household_id, keep_id, drop_id):
        """Fold one product into another: names, aliases, purchase history, stock and list entries."""
        if keep_id == drop_id:
            return
        keep_key, drop_key = product_key(keep_id), product_key(drop_id)
        with self.db.connect() as conn:
            keep, drop = self._product(conn, household_id, keep_id), self._product(conn, household_id, drop_id)
            if not keep or not drop:
                return
            # aliases (skip ones the kept product already has)
            for (alias,) in conn.execute("SELECT alias FROM product_aliases WHERE household_id = ? AND product_id = ?",
                                         (household_id, drop_id)).fetchall():
                conn.execute("DELETE FROM product_aliases WHERE household_id = ? AND alias = ? AND product_id = ?",
                             (household_id, alias, drop_id))
                conn.execute("INSERT INTO product_aliases (household_id, alias, product_id) VALUES (?, ?, ?) "
                             "ON CONFLICT DO NOTHING", (household_id, alias, keep_id))
            for table in ("purchases", "purchase_history"):
                conn.execute(f"UPDATE {table} SET item_key = ? WHERE household_id = ? AND item_key = ?",
                             (keep_key, household_id, drop_key))
            # stock: combine into the kept product's newest row
            rows = conn.execute("SELECT * FROM inventory WHERE household_id = ? AND item_key IN (?, ?) "
                                "ORDER BY purchase_date DESC, id DESC", (household_id, keep_key, drop_key)).fetchall()
            if rows:
                target = rows[0]
                total_units = sum(max(1, r["units"] or 1) for r in rows)
                total_eff = sum(_row_units(r) for r in rows)
                conn.execute("UPDATE inventory SET item_key = ?, item_name_en = ?, item_name_he = ?, category = ?, "
                             "units = ?, eff_units = ? WHERE id = ?",
                             (keep_key, keep["name_en"], keep["name_he"], keep["category"],
                              total_units, total_eff, target["id"]))
                for r in rows[1:]:
                    conn.execute("DELETE FROM inventory WHERE id = ?", (r["id"],))
            # shopping list: one entry at most
            on_list = conn.execute("SELECT id FROM shopping_list WHERE household_id = ? AND item_key = ?",
                                   (household_id, keep_key)).fetchone()
            if on_list:
                conn.execute("DELETE FROM shopping_list WHERE household_id = ? AND item_key = ?",
                             (household_id, drop_key))
            else:
                conn.execute("UPDATE shopping_list SET item_key = ?, item_name_en = ?, item_name_he = ? "
                             "WHERE household_id = ? AND item_key = ?",
                             (keep_key, keep["name_en"], keep["name_he"], household_id, drop_key))
            conn.execute("DELETE FROM products WHERE id = ?", (drop_id,))

    # ======================================================================
    # Internal helpers (take an open connection so callers can batch)
    # ======================================================================
    @staticmethod
    def _find_stock(conn, household_id, key):
        return conn.execute(
            "SELECT * FROM inventory WHERE household_id = ? AND item_key = ? ORDER BY purchase_date DESC, id DESC LIMIT 1",
            (household_id, key)).fetchone()

    @staticmethod
    def _log_interval(conn, household_id, key, category, start, end, eff_units, kind, user):
        days = (end - start).days
        if kind == "depleted":
            days = max(1, days)  # ran out the same day still counts as ~1 day
        if days <= 0:
            return None
        eff_units = max(0.05, float(eff_units or 1))
        per_unit = days / eff_units
        conn.execute(
            """INSERT INTO purchase_history
               (household_id, item_key, category, interval_days, units, eff_units, kind, recorded_at, recorded_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (household_id, key, category, per_unit, max(1, round(eff_units)), eff_units, kind, _s(end), user),
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
            key = it.get("item_key")
            if not key:
                en = (it.get("name_en") or "").strip() or (it.get("name_he") or "").strip()
                if not en:
                    continue
                pid = self.resolve_product(conn, household_id, en, it.get("name_he") or en, it.get("category"),
                                           product_id=it.get("product_id"))
                key = product_key(pid)
            if key in existing:
                continue
            pid = product_id_of(key)
            p = self._product(conn, household_id, pid) if pid else None
            name_en = p["name_en"] if p else it.get("name_en")
            name_he = p["name_he"] if p else it.get("name_he")
            category = (p["category"] if p else None) or it.get("category")
            next_order += 1
            conn.execute(
                """INSERT INTO shopping_list
                   (household_id, item_key, item_name_en, item_name_he, category, source, sort_order, added_at, added_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (household_id, key, name_en, name_he, category, source, next_order, _s(on_date), user),
            )
            existing.add(key)
            added += 1
        return added

    # ======================================================================
    # Purchases & consumption events
    # ======================================================================
    def record_purchase(self, household_id, name_en, name_he, category, units, purchase_date, user,
                        conn=None, product_id=None, size=None, raw_name=None) -> PurchaseResult:
        """Log a purchase of `units` packages. Size is parsed from the names unless given."""
        clean_en, size_en = extract_size((name_en or "").strip())
        clean_he, size_he = extract_size((name_he or "").strip())
        size = _as_size(size) or size_en or size_he
        clean_en = clean_en or clean_he
        clean_he = clean_he or clean_en
        if not clean_en and product_id is None:
            raise ValueError("Item needs a name.")
        units = max(1, int(units or 1))
        p_date = _to_date(purchase_date)
        if p_date > app_today():
            raise ValueError("Purchase date can't be in the future.")

        if conn is None:
            with self.db.connect() as own:
                return self.record_purchase(household_id, name_en, name_he, category, units, p_date, user,
                                            own, product_id, size, raw_name)

        extra = [raw_name] if raw_name else []
        pid = self.resolve_product(conn, household_id, clean_en, clean_he, category, size,
                                   product_id=product_id, extra_aliases=extra)
        p = self._product(conn, household_id, pid)
        if size and not p["ref_amount"]:
            # First known size becomes the reference package (the user can change it later).
            conn.execute("UPDATE products SET ref_amount = ?, ref_unit = ? WHERE id = ?", (size.amount, size.unit, pid))
            p = self._product(conn, household_id, pid)
        category = p["category"] or category
        if not p["category"] and category:
            conn.execute("UPDATE products SET category = ? WHERE id = ?", (category, pid))
        eff = to_ref_units(units, size, p["ref_amount"], p["ref_unit"])
        key = product_key(pid)

        conn.execute(
            """INSERT INTO purchases
               (household_id, item_key, category, item_name_en, item_name_he, units, eff_units, size_amount, size_unit,
                raw_name, purchase_date, recorded_by, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (household_id, key, category, p["name_en"], p["name_he"], units, eff,
             size.amount if size else None, size.unit if size else None, raw_name,
             _s(p_date), user, self._now()),
        )

        existing = self._find_stock(conn, household_id, key)
        learned = None
        if existing:
            prev_date = _to_date(existing["purchase_date"])
            delta = (p_date - prev_date).days
            if delta < 0:
                # Logging an older receipt: keep it in the ledger, don't disturb current stock.
                return PurchaseResult("backdated", key, product_id=pid)
            if delta == 0:
                conn.execute("UPDATE inventory SET units = units + ?, eff_units = ? WHERE id = ?",
                             (units, _row_units(existing) + eff, existing["id"]))
                conn.execute("DELETE FROM shopping_list WHERE household_id = ? AND item_key = ?", (household_id, key))
                return PurchaseResult("added_units", key, product_id=pid)
            learned = self._log_interval(conn, household_id, key, existing["category"] or category,
                                         prev_date, p_date, _row_units(existing), "repurchase", user)
            conn.execute("DELETE FROM inventory WHERE id = ?", (existing["id"],))

        scale = self._learned_scale(conn, household_id, key, category)
        conn.execute(
            """INSERT INTO inventory
               (household_id, item_key, category, item_name_en, item_name_he, units, eff_units,
                size_amount, size_unit, purchase_date, user_lambda)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (household_id, key, category, p["name_en"], p["name_he"], units, eff,
             size.amount if size else None, size.unit if size else None, _s(p_date), scale),
        )
        # Bought it → it no longer belongs on the shopping list.
        conn.execute("DELETE FROM shopping_list WHERE household_id = ? AND item_key = ?", (household_id, key))
        return PurchaseResult("restocked" if existing else "new", key, learned, pid)

    def record_purchases(self, household_id, items, purchase_date, user) -> list[PurchaseResult]:
        """Batch version for Quick Add / receipts; all-or-nothing.

        Each item: name_en, name_he, category, units, and optionally product_id, size, raw_name.
        """
        results = []
        with self.db.connect() as conn:
            for it in items:
                results.append(self.record_purchase(
                    household_id, it.get("name_en"), it.get("name_he"), it.get("category"),
                    it.get("units", 1), purchase_date, user, conn,
                    product_id=it.get("product_id"), size=it.get("size"), raw_name=it.get("raw_name")))
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
                                   _to_date(row["purchase_date"]), on_date, _row_units(row), "depleted", user)
                if add_to_list:
                    self._add_to_list(conn, household_id, [{"item_key": row["item_key"]}], "ran_out", user, on_date)
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
        """Edit a stock row: renames the product everywhere, and corrects the package count."""
        name_en = (name_en or "").strip() or (name_he or "").strip()
        name_he = (name_he or "").strip() or name_en
        if not name_en:
            raise ValueError("Item needs a name.")
        units = max(1, int(units))
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM inventory WHERE id = ? AND household_id = ?",
                               (inventory_id, household_id)).fetchone()
            if not row:
                return
            pid = product_id_of(row["item_key"])
            if pid:
                self.update_product(household_id, pid, name_en, name_he, category, conn)
                p = self._product(conn, household_id, pid)
                size = Size(row["size_amount"], row["size_unit"]) if row["size_amount"] else None
                eff = to_ref_units(units, size, p["ref_amount"], p["ref_unit"])
            else:
                eff = units
            conn.execute("UPDATE inventory SET units = ?, eff_units = ?, category = ? WHERE id = ?",
                         (units, eff, category, inventory_id))

    def merge_items(self, household_id, father_id, child_ids):
        """Merge pantry rows. Rows of different products merge the products themselves (history included)."""
        child_ids = [c for c in child_ids if c != father_id]
        if not child_ids:
            return
        with self.db.connect() as conn:
            father = conn.execute("SELECT item_key FROM inventory WHERE id = ? AND household_id = ?",
                                  (father_id, household_id)).fetchone()
            marks = ",".join("?" * len(child_ids))
            children = conn.execute(f"SELECT item_key FROM inventory WHERE household_id = ? AND id IN ({marks})",
                                    (household_id, *child_ids)).fetchall()
        if not father:
            return
        keep = product_id_of(father["item_key"])
        for c in children:
            drop = product_id_of(c["item_key"])
            if keep and drop and drop != keep:
                self.merge_products(household_id, keep, drop)
        # Same-product leftovers (e.g. two stock rows of one product): fold into the father row.
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM inventory WHERE household_id = ? AND item_key = ? ORDER BY id",
                                (household_id, father["item_key"])).fetchall()
            if len(rows) > 1:
                target = next((r for r in rows if r["id"] == father_id), rows[0])
                conn.execute("UPDATE inventory SET units = ?, eff_units = ? WHERE id = ?",
                             (sum(max(1, r["units"] or 1) for r in rows), sum(_row_units(r) for r in rows),
                              target["id"]))
                for r in rows:
                    if r["id"] != target["id"]:
                        conn.execute("DELETE FROM inventory WHERE id = ?", (r["id"],))

    # ======================================================================
    # Reads
    # ======================================================================
    def get_pantry(self, household_id, as_of=None) -> list[dict]:
        as_of = _to_date(as_of or app_today())
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM inventory WHERE household_id = ?", (household_id,)).fetchall()
            on_list = self._shopping_keys(conn, household_id)
            products = {r["id"]: r for r in conn.execute(
                "SELECT id, ref_amount, ref_unit FROM products WHERE household_id = ?", (household_id,)).fetchall()}

        items = []
        for r in rows:
            p_date = _to_date(r["purchase_date"])
            days = max(0, (as_of - p_date).days)
            confirmed = None
            if r["confirmed_until"]:
                confirmed = max(0, (_to_date(r["confirmed_until"]) - p_date).days)
            units = max(1, r["units"] or 1)
            eff = _row_units(r)
            per_unit = r["user_lambda"] if r["user_lambda"] else self.engine.prior_scale(r["category"])
            pid = product_id_of(r["item_key"])
            prod = products.get(pid)
            size = Size(r["size_amount"], r["size_unit"]) if r["size_amount"] else None
            items.append({
                "id": r["id"],
                "item_key": r["item_key"],
                "product_id": pid,
                "name_en": r["item_name_en"],
                "name_he": r["item_name_he"],
                "category": r["category"],
                "units": units,
                "eff_units": eff,
                "size": size,
                "ref_size": Size(prod["ref_amount"], prod["ref_unit"]) if prod and prod["ref_amount"] else None,
                "purchase_date": r["purchase_date"],
                "confirmed_until": r["confirmed_until"],
                "days_elapsed": days,
                "days_confirmed": confirmed,
                "per_unit_scale": per_unit,
                "effective_scale": self.engine.effective_scale(r["category"], per_unit, eff),
                "prob_depleted": self.engine.depletion_probability(r["category"], days, per_unit, eff, confirmed),
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
        cutoff = _to_date(as_of or app_today()) - timedelta(days=int(days))
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT purchase_date, item_name_en, item_name_he, category, units, recorded_by
                   FROM purchases WHERE household_id = ? AND purchase_date >= ?
                   ORDER BY purchase_date DESC, id DESC""",
                (household_id, _s(cutoff))).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]

    # ======================================================================
    # Shopping list
    # ======================================================================
    def get_shopping_list(self, household_id) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM shopping_list WHERE household_id = ? ORDER BY sort_order, id",
                                (household_id,)).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]

    def add_to_shopping_list(self, household_id, items, source, user, on_date=None) -> int:
        """items: dicts with item_key (from the pantry) or name_en/name_he/category. Skips duplicates."""
        with self.db.connect() as conn:
            return self._add_to_list(conn, household_id, items, source, user, _to_date(on_date or app_today()))

    def remove_from_shopping_list(self, household_id, shop_id):
        with self.db.connect() as conn:
            conn.execute("DELETE FROM shopping_list WHERE id = ? AND household_id = ?", (shop_id, household_id))

    def update_shopping_item(self, household_id, shop_id, name_en, name_he, category):
        """Editing a list entry renames its product everywhere."""
        with self.db.connect() as conn:
            row = conn.execute("SELECT item_key FROM shopping_list WHERE id = ? AND household_id = ?",
                               (shop_id, household_id)).fetchone()
            if not row:
                return
            pid = product_id_of(row["item_key"])
            if pid:
                self.update_product(household_id, pid, name_en, name_he, category, conn)

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
                                          units, on_date or app_today(), user, conn,
                                          product_id=product_id_of(row["item_key"]))
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
