import base64
import difflib
import io
import json
import os
import re
import traceback
import concurrent.futures
from datetime import datetime
import numpy as np
import pypdfium2 as pdfium
from PIL import Image, ImageOps, ImageFilter
from openai import OpenAI

from database import get_connection, init_db, get_household_categories
from engine import PantryDepletionEngine

class ReceiptIngestor:
    def __init__(self):
        init_db()
        self.engine = PantryDepletionEngine()
        self.client = OpenAI()

    def process_receipt_item(self, item_name_en: str, item_name_he: str, category: str, purchase_date_str: str, household_id: int, user_name: str = "Household", units: int = 1, conn=None):
        owns_conn = False
        if conn is None:
            conn = get_connection()
            owns_conn = True

        cursor = conn.cursor()
        purchase_date = datetime.strptime(purchase_date_str, "%Y-%m-%d")

        cursor.execute(
            """
            SELECT id, purchase_date, units 
            FROM inventory 
            WHERE household_id = ? 
              AND category = ?
              AND (
                  (TRIM(COALESCE(?, '')) != '' AND LOWER(item_name_en) = LOWER(TRIM(?))) OR 
                  (TRIM(COALESCE(?, '')) != '' AND LOWER(item_name_he) = LOWER(TRIM(?)))
              )
            ORDER BY purchase_date DESC LIMIT 1
            """,
            (household_id, category, item_name_en, item_name_en, item_name_he, item_name_he)
        )
        existing_item = cursor.fetchone()

        if existing_item:
            prev_date = datetime.strptime(existing_item["purchase_date"], "%Y-%m-%d")
            prev_units = existing_item["units"] if existing_item["units"] and existing_item["units"] > 0 else 1
            delta_days = (purchase_date - prev_date).days

            if delta_days > 0:
                interval_per_unit = delta_days / prev_units
                cursor.execute(
                    """
                    INSERT INTO purchase_history (household_id, category, interval_days, recorded_at, recorded_by)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (household_id, category, interval_per_unit, purchase_date_str, user_name)
                )
                
            cursor.execute("DELETE FROM inventory WHERE id = ?", (existing_item["id"],))

        cursor.execute(
            "SELECT interval_days FROM purchase_history WHERE household_id = ? AND category = ?",
            (household_id, category)
        )
        intervals = [row["interval_days"] for row in cursor.fetchall()]
        updated_lambda = self.engine.update_user_scale(category, intervals)

        safe_units = max(1, units)

        # Permanent purchases ledger logging
        cursor.execute(
            """
            INSERT INTO purchases (household_id, item_name_en, item_name_he, category, units, purchase_date, recorded_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (household_id, item_name_en, item_name_he, category, safe_units, purchase_date_str, user_name)
        )

        cursor.execute(
            """
            INSERT INTO inventory (household_id, item_name_en, item_name_he, category, units, purchase_date, user_lambda)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (household_id, item_name_en, item_name_he, category, safe_units, purchase_date_str, updated_lambda)
        )

        if owns_conn:
            conn.commit()
            conn.close()

    def mark_item_depleted_manually(self, inventory_id: int, household_id: int, current_date_str: str, user_name: str = "Household"):
        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT item_name_en, item_name_he, category, purchase_date, units FROM inventory WHERE id = ? AND household_id = ?",
            (inventory_id, household_id)
        )
        item = cursor.fetchone()

        if item:
            p_date = datetime.strptime(item["purchase_date"], "%Y-%m-%d")
            c_date = datetime.strptime(current_date_str, "%Y-%m-%d")
            prev_units = item["units"] if item["units"] and item["units"] > 0 else 1
            delta_days = max(1, (c_date - p_date).days)
            interval_per_unit = delta_days / prev_units

            cursor.execute(
                """
                INSERT INTO purchase_history (household_id, category, interval_days, recorded_at, recorded_by)
                VALUES (?, ?, ?, ?, ?)
                """,
                (household_id, item["category"], interval_per_unit, current_date_str, user_name)
            )

            cursor.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 AS next_order FROM shopping_list WHERE household_id = ?", (household_id,))
            next_order = cursor.fetchone()["next_order"]

            cursor.execute(
                """
                INSERT INTO shopping_list (household_id, item_name_en, item_name_he, category, source, sort_order, added_at, added_by)
                VALUES (?, ?, ?, ?, 'auto_depleted', ?, ?, ?)
                """,
                (household_id, item["item_name_en"], item["item_name_he"], item["category"], next_order, current_date_str, user_name)
            )

            cursor.execute("DELETE FROM inventory WHERE id = ?", (inventory_id,))

        conn.commit()
        conn.close()

    def _assess_image_quality(self, img: "Image.Image") -> dict:
        gray = img.convert('L')
        if max(gray.size) > 1600:
            scale = 1600 / max(gray.size)
            gray = gray.resize((max(1, int(gray.width * scale)), max(1, int(gray.height * scale))))

        laplacian_kernel = ImageFilter.Kernel((3, 3), [0, 1, 0, 1, -4, 1, 0, 1, 0], scale=1)
        edges = gray.filter(laplacian_kernel)
        sharpness = float(np.array(edges, dtype=np.float64).var())
        brightness = float(np.array(gray, dtype=np.float64).mean())

        reasons = []
        if sharpness < 80:
            reasons.append("the image looks blurry or out of focus")
        if brightness < 40:
            reasons.append("the image looks too dark")
        elif brightness > 235:
            reasons.append("the image looks overexposed / washed out")
        if min(img.size) < 600:
            reasons.append("the image resolution is too low")

        return {
            "acceptable": not reasons,
            "sharpness": sharpness,
            "brightness": brightness,
            "reasons": reasons,
        }

    def _prepare_images(self, file_bytes: bytes, filename: str) -> tuple[list[str], list[dict]]:
        ext = filename.lower().split('.')[-1]
        pil_images = []
        is_pdf = ext == 'pdf'

        if is_pdf:
            pdf = pdfium.PdfDocument(file_bytes)
            for page in pdf:
                pil_images.append(page.render(scale=3.0).to_pil())
        else:
            raw_img = Image.open(io.BytesIO(file_bytes))
            transposed = ImageOps.exif_transpose(raw_img)
            gray = transposed.convert('L')
            arr = np.array(gray)

            bounds = None
            for threshold in (180, 140, 100):
                col_max = arr.max(axis=0)
                row_max = arr.max(axis=1)
                paper_cols = np.where(col_max > threshold)[0]
                paper_rows = np.where(row_max > threshold)[0]

                if len(paper_cols) == 0 or len(paper_rows) == 0:
                    continue

                x0, x1 = paper_cols[0], paper_cols[-1]
                y0, y1 = paper_rows[0], paper_rows[-1]
                width_frac = (x1 - x0) / transposed.width
                height_frac = (y1 - y0) / transposed.height

                if width_frac >= 0.3 and height_frac >= 0.3:
                    bounds = (x0, x1, y0, y1, width_frac, height_frac)
                    break

            if bounds is not None:
                x0, x1, y0, y1, width_frac, height_frac = bounds
                if width_frac < 0.92 or height_frac < 0.92:
                    pad_x = max(int(transposed.width * 0.06), 25)
                    pad_y = max(int(transposed.height * 0.06), 25)
                    cropped = transposed.crop((
                        max(0, x0 - pad_x),
                        max(0, y0 - pad_y),
                        min(transposed.width, x1 + pad_x),
                        min(transposed.height, y1 + pad_y)
                    ))
                    pil_images.append(cropped)
                else:
                    pil_images.append(transposed)
            else:
                pil_images.append(transposed)

        quality_warnings = []
        accepted_images = []
        for idx, img in enumerate(pil_images):
            if not is_pdf:
                quality = self._assess_image_quality(img)
                if not quality["acceptable"]:
                    quality_warnings.append({
                        "page": idx + 1,
                        "reasons": quality["reasons"],
                        "sharpness": quality["sharpness"],
                        "brightness": quality["brightness"],
                    })
                    continue
            accepted_images.append(img)

        base64_images = []
        for img in accepted_images:
            buffered = io.BytesIO()
            img.save(buffered, format="JPEG", quality=95)
            base64_images.append(base64.b64encode(buffered.getvalue()).decode('utf-8'))

        return base64_images, quality_warnings

    def _extract_page_items(self, b64_img: str, household_id: int) -> list[dict]:
        categories = get_household_categories(household_id)
        cats_formatted = "\n".join([f'- "{c["en"]}" (Hebrew: {c["he"]})' for c in categories])

        prompt = f"""
You are an expert grocery data parser. Analyze this high-resolution receipt image directly.

INSTRUCTIONS:
1. EXHAUSTIVE EXTRACTION: Extract EVERY single edible grocery item printed on the receipt. Read row by row carefully.
2. COMBINE MULTI-LINE PRODUCTS: Israeli receipts frequently split long product names across two consecutive lines. Combine them into a single item.
3. STRICT EXACT TRANSCRIPTION: Transcribe the primary Hebrew product name EXACTLY as printed.
4. QUANTITIES:
   - Weighed produce counts as units = 1.
   - Look for unit multipliers (e.g., '2 יחידה x 7.35' -> units = 2).
5. EDIBILITY CLASSIFICATION:
   - "is_food": true for all groceries, produce, baked goods, dairy, meat, snacks.
   - "is_food": false for bottle/bag packaging discounts, totals, fees, and cleaning supplies.
6. CATEGORIZE: Map each valid food item strictly to one of these:
{cats_formatted}

Return valid JSON:
{{
  "items": [
    {{
      "item_name_en": "Clear English translation",
      "item_name_he": "Combined verbatim Hebrew name",
      "category": "Exact Category Key from list",
      "units": <Integer quantity, default 1>,
      "is_food": <true or false>
    }}
  ]
}}
"""
        try:
            response = self.client.chat.completions.create(
                model="gpt-4o",
                response_format={"type": "json_object"},
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_img}", "detail": "high"}}
                    ]
                }],
                temperature=0.0
            )
            parsed = json.loads(response.choices[0].message.content)
            all_items = parsed.get("items", [])
            return [it for it in all_items if it.get("is_food") is True]
        except Exception:
            return []

    def parse_preview(self, file_bytes: bytes, filename: str, household_id: int, status_callback=None) -> dict:
        if status_callback:
            status_callback("📸 Preparing high-resolution image...")

        b64_images, quality_warnings = self._prepare_images(file_bytes, filename)

        for w in quality_warnings:
            reason_text = "; ".join(w["reasons"])
            if status_callback:
                status_callback(f"⚠️ Page {w['page']} skipped - {reason_text}. Please retake and re-upload.")

        if not b64_images:
            return {"items": [], "quality_warnings": quality_warnings}

        all_items = []
        num_pages = len(b64_images)

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(num_pages, 4)) as executor:
            future_to_page = {
                executor.submit(self._extract_page_items, b64, household_id): idx + 1
                for idx, b64 in enumerate(b64_images)
            }

            for future in concurrent.futures.as_completed(future_to_page):
                page_num = future_to_page[future]
                try:
                    res = future.result()
                    all_items.extend(res)
                    if status_callback:
                        status_callback(f"✅ Extracted page {page_num}/{num_pages}...")
                except Exception:
                    pass

        merged = {}
        order = []
        for it in all_items:
            key = (it.get("item_name_en", "").strip().lower(), it.get("item_name_he", "").strip().lower())
            if not key[0]:
                continue
            if key not in merged:
                merged[key] = dict(it)
                merged[key]["units"] = merged[key].get("units") or 1
                order.append(key)
            else:
                merged[key]["units"] = (merged[key].get("units") or 1) + (it.get("units") or 1)

        return {"items": [merged[k] for k in order], "quality_warnings": quality_warnings}

    def parse_raw_text(self, raw_text: str, household_id: int) -> list[dict]:
        categories = get_household_categories(household_id)
        cats_formatted = "\n".join([f'- "{c["en"]}" (Hebrew: {c["he"]})' for c in categories])

        prompt = f"""
Convert this pasted grocery list into structured items.
{raw_text}

Map strictly to categories:
{cats_formatted}

JSON Schema:
{{
  "items": [
    {{
      "item_name_en": "English Translation",
      "item_name_he": "Hebrew Name",
      "category": "Category Key",
      "units": 1
    }}
  ]
}}
"""
        try:
            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                response_format={"type": "json_object"},
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0
            )
            return json.loads(response.choices[0].message.content).get("items", [])
        except Exception:
            return []