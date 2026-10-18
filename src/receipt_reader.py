"""Read a grocery receipt (phone photo or PDF) into purchase lines.

Two passes, kept deliberately separate:

  1. TRANSCRIBE (vision model): copy every printed row of the receipt, top to bottom, as
     structured JSON: code, text, quantity, kg or units, line price, and what kind of row it is.
     No interpretation, no merging, so nothing gets "tidied away".
  2. ASSEMBLE (plain Python, see `assemble`): drop deal notes, keep deposits out of the pantry,
     net credits into their item, merge repeated lines of the same product, turn weights into
     package sizes, and CHECK the result against the receipt:
        * line prices must add up to the printed total, and
        * units (each weighed item counts as one, deposits included) must equal the printed item count.
     Both held exactly on every sample receipt (Osher Ad digital receipts and a Carrefour photo).
     If either check fails, the model gets one more look with the numbers it missed by; whatever
     is still off is shown to the user as a warning before saving.

Images never leave the request: nothing is stored.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import re
from dataclasses import dataclass, field

from config import get_secret
from normalize import Size, extract_size, match_key

log = logging.getLogger(__name__)

try:  # iPhone photos (HEIC) when the optional package is installed
    from pillow_heif import register_heif_opener
    register_heif_opener()
except Exception:  # pragma: no cover - optional
    pass

DEFAULT_VISION_MODEL = "gpt-4.1-mini"
FALLBACK_VISION_MODEL = "gpt-4o-mini"
MAX_FILE_BYTES = 15 * 1024 * 1024
MAX_PDF_PAGES = 8
MAX_IMAGES = 16
SLICE_RATIO = 2.2          # slice images taller than this (height / width)
SUM_TOLERANCE = 0.05       # ₪; rounding on weighed lines
KINDS = {"item", "deposit", "discount", "promo", "other"}

DEPOSIT_RE = re.compile(r"מיחזור|פיקדון|פקדון|deposit", re.IGNORECASE)
# Payment / footer lines the model sometimes copies as rows (card amounts, change, totals).
PAYMENT_RE = re.compile(r'כרטיס|אשראי|מזומן|עודף|לתשלום|תשלום|סה"?כ|סה״כ|\bDTS\b|visa|ויזה|ישראכרט|mastercard',
                        re.IGNORECASE)
AMOUNT_ONLY_RE = re.compile(r"^\s*-?\d+[.,]\d{2}\s*-?\s*$")
PRICED_KINDS = ("item", "deposit", "discount")
# Deal notes: "2 ב 25", "4 ב 18 -15", "שימורי זנלכל 2ב16", "9.90 מוגבל 3"
PROMO_RE = re.compile(r"(?<![\d.])\d+\s*ב\s*-?\d+(?:\.\d+)?|מוגבל")
# Deal text the model sometimes glues onto a product's description (removed from the text only).
DEAL_TEXT_RE = re.compile(r"-?\d+(?:\.\d+)?\s*-?\s*מוגבל\s*\d*|מוגבל\s*\d+(?:\.\d+)?|"
                          r"(?<![\d.])-?\d+\s*ב\s*-?\d+(?:\.\d+)?-?")
# Tare / weight-only lines: "0.020 ק"ג x 14.90"
TARE_RE = re.compile(r'^\s*\d+[.,]\d+\s*(?:ק"?ג|ק״ג|kg)?\s*[x×*]\s*\d+[.,]?\d*\s*$', re.IGNORECASE)


class ReceiptError(RuntimeError):
    """User-facing problem (bad file, nothing readable, API trouble)."""


# ==========================================================================
# 1a. Images
# ==========================================================================
def load_images(data: bytes, filename: str = "") -> list:
    """File bytes → list of RGB PIL images in reading order (PDF pages or one photo)."""
    from PIL import Image, ImageOps

    if not data:
        raise ReceiptError("empty")
    if len(data) > MAX_FILE_BYTES:
        raise ReceiptError("too_big")
    if data[:5] == b"%PDF-" or filename.lower().endswith(".pdf"):
        try:
            import pypdfium2 as pdfium
            pdf = pdfium.PdfDocument(data)
        except Exception as e:
            raise ReceiptError("bad_file") from e
        if len(pdf) > MAX_PDF_PAGES:
            raise ReceiptError("too_many_pages")
        # Scale 3 ≈ 216 dpi: receipt text comes out ~25 px high, comfortably readable.
        return [pdf[i].render(scale=3).to_pil().convert("RGB") for i in range(len(pdf))]
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)   # phone photos store rotation in EXIF
        return [img.convert("RGB")]
    except Exception as e:
        raise ReceiptError("bad_file") from e


def trim_margins(img, threshold: int = 235, pad: int = 16):
    """Crop plain white margins (PDF pages). Photos with a background are left alone."""
    from PIL import ImageOps
    gray = ImageOps.invert(img.convert("L")).point(lambda v: 255 if v > 255 - threshold else 0)
    box = gray.getbbox()
    if not box:
        return img
    l, t, r, b = box
    return img.crop((max(0, l - pad), max(0, t - pad), min(img.width, r + pad), min(img.height, b + pad)))


def crop_to_paper(img, min_width: int = 900):
    """Photos: crop to the (bright) receipt paper and enlarge narrow receipts.

    A receipt photographed on a table often fills only a third of the frame; the model then sees
    tiny text. Bright rows/columns are found from brightness profiles; nothing happens when there's
    no clear background (receipt on a white table, scans, screenshots).
    """
    from PIL import Image
    g = img.convert("L")
    small = g.copy()
    small.thumbnail((400, 400))
    w, h = small.size
    px = small.load()
    bright = [[px[x, y] > 150 for x in range(w)] for y in range(h)]
    cols = [sum(bright[y][x] for y in range(h)) / h for x in range(w)]

    def span(profile):
        top = max(profile) or 1
        on = [v >= 0.4 * top for v in profile]
        best, cur, start = (0, 0), 0, 0
        for i, v in enumerate(on + [False]):
            if v and cur == 0:
                start = i
            cur = cur + 1 if v else 0
            if not v and i - start > best[1] - best[0] and i > start:
                best = (start, i)
        return best

    x0, x1 = span(cols)
    # Rows: brightness inside the paper's columns, smoothed over a few text lines, so dense
    # lines of print don't look like background.
    inner = [sum(bright[y][x] for x in range(x0, x1)) / max(1, x1 - x0) for y in range(h)]
    k = max(1, h // 40)
    rows = [sum(inner[max(0, y - k):y + k + 1]) / len(inner[max(0, y - k):y + k + 1]) for y in range(h)]
    y0, y1 = span(rows)
    if (x1 - x0) * (y1 - y0) > 0.8 * w * h or x1 - x0 < w * 0.1:
        return img            # no clear background, or nothing paper-like found
    sx, sy = img.width / w, img.height / h
    pad_x, pad_y = (x1 - x0) * 0.04, (y1 - y0) * 0.01
    box = (max(0, int((x0 - pad_x) * sx)), max(0, int((y0 - pad_y) * sy)),
           min(img.width, int((x1 + pad_x) * sx)), min(img.height, int((y1 + pad_y) * sy)))
    out = img.crop(box)
    if out.width < min_width:
        f = min_width / out.width
        out = out.resize((min_width, int(out.height * f)), Image.LANCZOS)
    return out


def _ink_profile(img) -> list[float]:
    """Share of dark pixels per row, measured on the middle 80% of the width."""
    g = img.convert("L")
    w, h = g.size
    x0, x1 = int(w * 0.1), int(w * 0.9)
    px = g.load()
    step = max(1, (x1 - x0) // 200)        # sample ~200 columns: fast enough on big photos
    cols = range(x0, x1, step)
    n = len(cols) or 1
    return [sum(1 for x in cols if px[x, y] < 140) / n for y in range(h)]


def squeeze_blank(img, max_gap: int = 48):
    """Shrink long blank stretches (e.g. the empty half of a PDF's last page) to `max_gap` rows."""
    from PIL import Image
    ink = _ink_profile(img)
    keep, run = [], 0
    for y, v in enumerate(ink):
        run = run + 1 if v < 0.002 else 0
        if run <= max_gap:
            keep.append(y)
    if len(keep) > img.height - 8:
        return img
    out = Image.new("RGB", (img.width, len(keep)), "white")
    # copy contiguous bands of kept rows
    start = prev = keep[0]
    dst = 0
    for y in keep[1:] + [None]:
        if y is None or y != prev + 1:
            band = img.crop((0, start, img.width, prev + 1))
            out.paste(band, (0, dst))
            dst += band.height
            if y is not None:
                start = y
        if y is not None:
            prev = y
    return out


# ---- multi-page PDFs: rebuild one continuous receipt, re-joining rows split by page breaks ----
INK = 140          # darker than this = printed text (light page headers/footers fall outside)


def _ink_rows(g) -> list[bool]:
    w, h = g.size
    px = g.load()
    return [any(px[x, y] < INK for x in range(0, w, 2)) for y in range(h)]


def _row_blocks(g) -> list[tuple[int, int]]:
    """Vertical runs of printed rows (a 2-line item is one block: its lines touch)."""
    dark = _ink_rows(g)
    blocks, y, h = [], 0, len(dark)
    while y < h:
        if dark[y]:
            s = y
            while y < h and (dark[y] or (y + 3 < h and any(dark[y:y + 4]))):
                y += 1
            blocks.append((s, y))
        y += 1
    return blocks


def _zones(g, block) -> set[str]:
    """Which columns a block has ink in: P(rice) Q(ty) D(escription) C(ode), by position across the width."""
    y0, y1 = block
    w = g.width
    px = g.load()
    bounds = {"P": (0.0, 0.18), "Q": (0.18, 0.33), "D": (0.33, 0.70), "C": (0.70, 1.0)}
    out = set()
    for z, (a, b) in bounds.items():
        xs = range(int(a * w), max(int(a * w) + 1, int(b * w)), 2)
        if any(px[x, y] < INK for y in range(y0, y1, 2) for x in xs):
            out.add(z)
    return out


def _stack(top, bottom, gap: int):
    from PIL import Image
    out = Image.new("L", (top.width, top.height + gap + bottom.height), 255)
    out.paste(top, (0, 0))
    out.paste(bottom, (0, top.height + gap))
    return out


def _collide(a, b) -> bool:
    """Would two strips' printed text overlap if bottom-aligned? (Then they're NOT one split row.)"""
    from PIL import Image, ImageChops
    h = max(a.height, b.height)
    ma = Image.new("L", (a.width, h), 0)
    mb = Image.new("L", (a.width, h), 0)
    ma.paste(a.point(lambda v: 255 if v < INK else 0), (0, h - a.height))
    mb.paste(b.point(lambda v: 255 if v < INK else 0), (0, h - b.height))
    both = ImageChops.multiply(ma, mb)
    return sum(both.histogram()[1:]) > 15


def _overlay(top, bottom):
    """Combine two row strips whose ink is in different columns, bottom-aligned (darker pixel wins)."""
    from PIL import Image, ImageChops
    h = max(top.height, bottom.height)
    a = Image.new("L", (top.width, h), 255)
    b = Image.new("L", (top.width, h), 255)
    a.paste(top, (0, h - top.height))
    b.paste(bottom, (0, h - bottom.height))
    return ImageChops.darker(a, b)


def stitch_pages(pages) -> "Image":
    """Pages → one tall receipt image, with every row whole.

    Digital receipts (e.g. Pairzon, used by Osher Ad and others) break pages mid-row: either the
    PRICE ends up alone at the bottom of a page (code/qty/description on the next page), or the
    code/qty/price end the page and the DESCRIPTION starts the next one. The model then shifts
    prices or glues descriptions onto the wrong row. Here such pieces are put back on one line.
    Light page headers/footers (lighter than INK) are dropped; repeated dark ones are dropped too.
    """
    from PIL import Image
    grays = [p.convert("L") for p in pages]
    boxes = [g.point(lambda v: 255 if v < INK else 0).getbbox() for g in grays]
    boxes = [b for b in boxes if b]
    if not boxes:
        return pages[0]
    x0 = max(0, min(b[0] for b in boxes) - 12)
    x1 = min(grays[0].width, max(b[2] for b in boxes) + 12)
    pages_blocks = []                                  # per page: list of (strip, gap_before)
    for g in grays:
        g = g.crop((x0, 0, x1, g.height))
        blocks = _row_blocks(g)
        strips, prev_end = [], None
        for (a, b) in blocks:
            strips.append([g.crop((0, max(0, a - 3), g.width, min(g.height, b + 3))),
                           0 if prev_end is None else a - prev_end])
            prev_end = b
        pages_blocks.append(strips)

    # Drop a dark footer/header that repeats identically on every page.
    if len(pages_blocks) > 1 and all(pages_blocks):
        for idx in (-1, 0):
            sigs = [p[idx][0].resize((64, 8)).tobytes() for p in pages_blocks]
            same = all(sum(abs(a - b) for a, b in zip(sigs[0], s)) / len(s) < 4 for s in sigs[1:])
            first = pages_blocks[0][idx][0]
            looks_like_item = {"P", "C"} <= _zones(first, (0, first.height))   # same product line twice
            if same and not looks_like_item:
                for p in pages_blocks:
                    p.pop(idx)

    # Re-join rows split across page breaks.
    for i in range(len(pages_blocks) - 1):
        cur, nxt = pages_blocks[i], pages_blocks[i + 1]
        if not cur or not nxt:
            continue
        e_img = cur[-1][0]
        ze = _zones(e_img, (0, e_img.height))
        # the first block(s) of the next page; consecutive description-only lines belong together
        take = 1
        s_img = nxt[0][0]
        zs = _zones(s_img, (0, s_img.height))
        if zs <= {"D"}:
            while take < len(nxt) and nxt[take][1] < 20 and \
                    _zones(nxt[take][0], (0, nxt[take][0].height)) <= {"D"}:
                s_img = _stack(s_img, nxt[take][0], nxt[take][1])
                take += 1
        lone_price = ze == {"P"} and "P" not in zs and bool(zs & {"Q", "C"})
        lone_desc = {"P", "C"} <= ze and zs <= {"D"} and bool(zs)
        if (lone_price or lone_desc) and not _collide(s_img, e_img):
            merged = _overlay(s_img, e_img)
            if lone_price:
                nxt[0][0] = merged       # the price joins its row on the next page
                cur.pop()
            else:
                cur[-1][0] = merged      # the description joins its row on this page
                del nxt[:take]
                if nxt:
                    nxt[0][1] = 0

    strips = []
    for p_idx, p in enumerate(pages_blocks):
        for j, (img, gap) in enumerate(p):
            strips.append((img, 28 if j == 0 and p_idx > 0 else min(max(gap, 6), 60)))
    total_h = sum(im.height + gap for im, gap in strips) + 24
    out = Image.new("L", (x1 - x0, total_h), 255)
    y = 12
    for im, gap in strips:
        y += gap if y > 12 else 0
        out.paste(im, (0, y))
        y += im.height
    return out.crop((0, 0, out.width, y + 12)).convert("RGB")


def slice_tall(img, max_ratio: float = SLICE_RATIO) -> list:
    """Cut a long receipt into readable pieces, always through a blank gap between rows.

    Vision models shrink each image to a fixed size; a 3-page receipt as one strip would come
    out unreadably small. Cutting only through white space means no row is ever split by us.
    """
    w, h = img.size
    if h <= w * max_ratio:
        return [img]
    target = int(w * (max_ratio - 0.3))
    ink = _ink_profile(img)
    pieces, top = [], 0
    while h - top > w * max_ratio:
        lo, hi = top + int(target * 0.75), min(h - 1, top + int(target * 1.15))
        # the blankest row in the window, preferring the one nearest the target
        cut = min(range(lo, hi), key=lambda y: (round(ink[y], 3), abs(y - (top + target))))
        pieces.append(img.crop((0, top, w, cut)))
        top = cut
    pieces.append(img.crop((0, top, w, h)))
    return pieces


def prepare(data: bytes, filename: str = "") -> list:
    """File → the list of images sent to the model, in order."""
    out = []
    is_pdf = data[:5] == b"%PDF-" or filename.lower().endswith(".pdf")
    pages = load_images(data, filename)
    if is_pdf:
        # One continuous receipt with whole rows, then cut through the gaps between rows.
        out.extend(slice_tall(stitch_pages(pages)))
    else:
        for page in pages:
            page = crop_to_paper(trim_margins(page))
            if max(page.size) > 3000:
                page.thumbnail((3000, 3000))
            out.extend(slice_tall(page))
    if len(out) > MAX_IMAGES:
        raise ReceiptError("too_many_pages")
    return out


def _data_url(img) -> str:
    buf = io.BytesIO()
    im = img.copy()
    im.thumbnail((2048, 2048))
    im.save(buf, format="JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


# ==========================================================================
# 1b. Transcription (vision model)
# ==========================================================================
TRANSCRIBE_PROMPT = """You transcribe Israeli supermarket receipts (Hebrew, right-to-left) into JSON.
The images are consecutive pieces of ONE receipt, in order (pages or slices of a long photo).

Copy EVERY printed row between the header and the total, top to bottom, across all images.
- Never skip a row. Never merge rows. The same product often appears on several lines: list each one.
- A row can be split between two images. Typically its PRICE stands alone at the very bottom of one
  image and its code, description and quantity are at the top of the next image (printed there with
  no price). That lone amount IS that row's price: output the row ONCE, combined. Never output a
  lone amount as a row of its own, and never drop it.
- Stop at the total. Do not copy payment lines (credit card, cash, change, VAT, subtotal, totals)
  as rows.
- "text": the description exactly as printed (Hebrew, truncated words and sizes included). Do not translate or fix.
- "code": the item code/barcode as printed (digits only), or null.
- "qty": the quantity column. For weighed items it is the weight in kg (e.g. 1.23 ק"ג → 1.23, "qty_unit": "kg");
  otherwise a count ("qty_unit": "unit").
- "price": the LINE TOTAL as printed (not the price per unit or per kg). Negative for credits/discounts. null if none.
- "unit_price": the price per kg / per unit when the receipt prints it (e.g. "0.440 x 14.90" → 14.90), else null.
  Examples: "0.675 ק"ג x 12.90 → 8.71" gives qty 0.675 kg, price 8.71. "2 יחידה x 7.35 → 14.70" gives qty 2, price 14.70.
- "kind":
    "item"     a product line
    "deposit"  bottle/can/packaging deposit (מיחזור אריזה, פיקדון, פקדון)
    "discount" a line with a negative amount (הנחה, זיכוי, זיכוי אריזה)
    "promo"    a deal description with no amount of its own, e.g. "2 ב 25", "3 מוגבל 9.90", "4 ב 18 -15"
    "other"    any other row without an amount, e.g. a tare weight "0.020 ק"ג x 14.90"
From the footer:
- "total": the amount to pay for the purchase (next to לתשלום / סה"כ לתשלום / סה"כ). NOT an amount paid by a
  particular card or in cash (a purchase can be split over two cards), and not VAT or a subtotal.
- "footer_amounts": every amount printed below the last item (totals, payments, VAT, change), as numbers.
- "item_count": the printed number of items (סה"כ פריטים / מספר פריטים), or null.
- "date": the purchase date EXACTLY as printed (e.g. "17/09/26" or "27/08/2026"). Israeli dates are day/month/year.
- "store": the chain name (e.g. "אושר עד", "שופרסל", "Carrefour"), not the company's legal name.

Return ONLY JSON:
{"store": "...", "date": "17/09/26", "total": 0.0, "footer_amounts": [0.0], "item_count": 0,
 "rows": [{"kind": "item", "code": "...", "text": "...", "qty": 1, "qty_unit": "unit", "price": 0.0, "unit_price": null}]}"""


class ReceiptReader:
    def __init__(self, model: str | None = None):
        self.model = model or get_secret("OPENAI_VISION_MODEL") or DEFAULT_VISION_MODEL
        self._client = None
        self.calls = 0

    @property
    def available(self) -> bool:
        return bool(get_secret("OPENAI_API_KEY"))

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(api_key=get_secret("OPENAI_API_KEY"), timeout=180)
        return self._client

    def _ask(self, images, extra: str = "") -> dict:
        content = [{"type": "text", "text": f"Receipt in {len(images)} image(s), in order." + extra}]
        content += [{"type": "image_url", "image_url": {"url": _data_url(im), "detail": "high"}} for im in images]
        messages = [{"role": "system", "content": TRANSCRIBE_PROMPT}, {"role": "user", "content": content}]
        last = None
        for model in dict.fromkeys([self.model, FALLBACK_VISION_MODEL]):
            try:
                self.calls += 1
                resp = self._get_client().chat.completions.create(
                    model=model, temperature=0, response_format={"type": "json_object"},
                    messages=messages, max_tokens=8000)
                return json.loads(resp.choices[0].message.content)
            except Exception as e:  # unknown model, overload, bad JSON…
                last = e
                log.warning("Receipt transcription with %s failed: %s", model, e)
        raise ReceiptError("api") from last

    def read(self, data: bytes, filename: str = "") -> "ReceiptResult":
        if not self.available:
            raise ReceiptError("no_key")
        images = prepare(data, filename)
        answers = [self._ask(images)]
        result = assemble(answers[0])
        if result.needs_second_look:
            # Pass 2: same images, told exactly how far off the first transcription was.
            hint = ["\nA first transcription of this receipt is below. It is incomplete or misread:"]
            if result.sum_mismatch:
                hint.append(f"its line prices add up to {result.rows_sum:.2f} but the receipt total is "
                            f"{result.total:.2f} (difference {result.total - result.rows_sum:+.2f}).")
            if result.count_mismatch:
                hint.append(f"It has {result.counted:g} units but the receipt says {result.item_count:g} items.")
            hint.append("Look again at every image, especially at the edges between images, for rows that were "
                        "missed, duplicated or misread. Return the COMPLETE corrected JSON.\n"
                        + json.dumps(result.raw, ensure_ascii=False))
            try:
                answers.append(self._ask(images, " ".join(hint)))
                second = assemble(answers[-1])
                if second.score <= result.score:
                    second.rechecked = True
                    result = second
            except ReceiptError:
                pass  # keep the first reading; the warning tells the user what to check
        result.images = len(images)
        result.calls = self.calls
        result.answers = answers
        return result


# ==========================================================================
# 2. Assembly (deterministic)
# ==========================================================================
@dataclass
class ReceiptLine:
    text: str                 # as printed (first line, if merged)
    code: str | None
    units: int                # packages; 1 for weighed items
    size: Size | None         # package size; total weight for weighed items
    price: float | None       # net of credits, summed over merged lines
    weighed: bool = False
    lines: int = 1            # how many receipt rows were merged into this one


@dataclass
class ReceiptResult:
    lines: list[ReceiptLine]
    store: str = ""
    date: str | None = None
    total: float | None = None
    item_count: float | None = None
    rows_sum: float = 0.0
    counted: float = 0.0
    deposits: float = 0.0
    merged: int = 0           # receipt rows folded into another line
    fixed: int = 0            # rows repaired using the receipt's total (see _repair)
    raw: dict = field(default_factory=dict)
    rechecked: bool = False
    images: int = 0
    calls: int = 0
    answers: list = field(default_factory=list)   # the model's raw JSON answers (for debugging)

    @property
    def sum_mismatch(self) -> bool:
        return self.total is not None and abs(self.total - self.rows_sum) > SUM_TOLERANCE

    @property
    def count_mismatch(self) -> bool:
        return self.item_count is not None and abs(self.item_count - self.counted) > 0.01

    @property
    def needs_second_look(self) -> bool:
        return self.sum_mismatch or self.count_mismatch or not self.lines

    @property
    def missing_amount(self) -> float:
        return round((self.total or 0) - self.rows_sum, 2) if self.total is not None else 0.0

    @property
    def score(self) -> float:
        """Lower is better: how far the reading is from the receipt's own totals."""
        s = abs(self.missing_amount) if self.total is not None else 0.0
        if self.item_count is not None:
            s += 2 * abs(self.item_count - self.counted)
        return s + (1000 if not self.lines else 0)


def _num(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace("₪", "").replace(",", ".").replace("−", "-").strip()
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    n = float(m.group())
    return -abs(n) if s.endswith("-") and n > 0 else n   # "0.30-" is how some printers show credits


def _clean_rows(raw: dict) -> list[dict]:
    rows = []
    for r in raw.get("rows") or []:
        if not isinstance(r, dict):
            continue
        text = str(r.get("text") or "").strip()
        code = re.sub(r"\D", "", str(r.get("code") or "")) or None
        price = _num(r.get("price"))
        qty = _num(r.get("qty"))
        unit = "kg" if str(r.get("qty_unit") or "").lower() in ("kg", 'ק"ג', "קג") else "unit"
        kind = str(r.get("kind") or "item").lower()
        kind = kind if kind in KINDS else "item"
        if not text and price is None:
            continue
        if not code and PAYMENT_RE.search(text):
            continue                                   # payment section, not a purchase
        # Safety nets for common model slips:
        if kind == "item" and DEPOSIT_RE.search(text):
            kind = "deposit"
        if kind == "item" and price is None and not code and PROMO_RE.search(text):
            kind = "promo"
        if kind == "item" and price is not None and price < 0:
            kind = "discount"
        if kind == "discount" and price is not None and price > 0:
            kind = "item"                              # a "discount" with a positive price is a product
        unit_price = _num(r.get("unit_price"))
        if unit == "kg" and unit_price and price and price > 0 and qty and abs(qty * unit_price - price) > 0.02:
            qty = round(price / unit_price, 3)         # weight misread: weight × price per kg must match
        if kind == "item" and not code and TARE_RE.match(text):
            kind, price = "other", None                # bag/tray weight, not a product
        if kind in ("item", "deposit") and code:
            stripped = DEAL_TEXT_RE.sub(" ", text)
            stripped = re.sub(r'^\s*\d+[.,]\d+\s*(?:ק"?ג|ק״ג|kg)\s+', "", stripped)          # "0.59 ק"ג עגבניה"
            if qty and float(qty).is_integer():
                stripped = re.sub(rf"^\s*{int(qty)}\s+(?=[א-ת])", "", stripped)          # "2 חלב 3%…" (qty glued)
            stripped = re.sub(r"\s+", " ", stripped).strip(" -")
            text = stripped or text
        rows.append({"kind": kind, "code": code, "text": text, "qty": qty, "qty_unit": unit, "price": price})
    return rows


def _priced_sum(rows) -> float:
    return round(sum(r["price"] for r in rows if r["price"] is not None and r["kind"] in PRICED_KINDS), 2)


def _repair(rows: list[dict], total: float | None, infer: bool = True) -> list[dict]:
    """Fix the slips the receipt's own total lets us fix safely. Returns new rows; each fix is marked.

    1. Orphan price: a row that is just an amount ("18.90") with no code. On multi-page receipts
       a row's price can end up alone at the bottom of a page, its description on the next page.
       The amount goes to the nearest item that has no price (next one first).
    2. Deal lines with an amount ("4 ב 15 -15.00"): kept as a discount only if that is what
       makes the lines add up to the total; otherwise they're just deal notes.
    3. One price still missing: if exactly one item has no price, the gap to the total is its price.
    """
    rows = [dict(r) for r in rows]
    # 1. orphan prices
    out, pending = [], []
    for r in rows:
        if not r["code"] and r["kind"] != "discount" and (AMOUNT_ONLY_RE.match(r["text"]) or not r["text"]):
            value = r["price"] if r["price"] is not None else _num(r["text"])
            if value is not None:
                pending.append(value)
                continue
        if r["kind"] == "item" and r["price"] is None and pending:
            r["price"], r["fixed"] = pending.pop(0), "orphan_price"
        out.append(r)
    for value in pending:                     # price came after its item: give it to an earlier one
        target = next((r for r in reversed(out) if r["kind"] == "item" and r["price"] is None), None)
        if target is not None:
            target["price"], target["fixed"] = value, "orphan_price"
    rows = out

    if total is None:
        return rows
    # 2. ambiguous negative lines: a deal note read as a discount, or a product read as a credit
    #    because deal text was glued to it. Try the readings; keep one only if it hits the total.
    if abs(_priced_sum(rows) - total) > SUM_TOLERANCE:
        options = []
        for idx, r in enumerate(rows):
            if r["kind"] == "discount" and r["price"] is not None:
                alts = []
                if PROMO_RE.search(r["text"]):
                    alts.append(dict(r, kind="promo", price=None, fixed="deal_note"))
                if r["code"] and barcode_ok(r["code"]) is not False:
                    text = re.sub(r"\s+", " ", DEAL_TEXT_RE.sub(" ", r["text"])).strip(" -") or r["text"]
                    alts.append(dict(r, kind="item", price=abs(r["price"]), text=text, fixed="not_a_credit"))
                if alts:
                    options.append((idx, alts))
        best = None
        for mask in range(1, 1 << min(len(options), 8)):
            trial = list(rows)
            changes = 0
            for bit, (idx, alts) in enumerate(options[:8]):
                if mask >> bit & 1:
                    trial[idx] = alts[-1] if len(alts) == 1 or trial[idx]["code"] else alts[0]
                    changes += 1
            if abs(_priced_sum(trial) - total) <= SUM_TOLERANCE and (best is None or changes < best[0]):
                best = (changes, trial)
        if best:
            rows = best[1]
    # 3. a single missing price
    missing = [r for r in rows if r["kind"] == "item" and r["price"] is None]
    gap = round(total - _priced_sum(rows), 2)
    if infer and len(missing) == 1 and gap > 0:
        missing[0]["price"], missing[0]["fixed"] = gap, "from_total"
    return rows


def barcode_ok(code: str) -> bool | None:
    """EAN-8/UPC-A/EAN-13 check digit. None = not a standard barcode length (can't tell)."""
    if len(code) == 11:
        code = "0" + code                     # UPC-A printed without its leading zero
    if len(code) not in (8, 12, 13) or not code.isdigit():
        return None
    body, check = [int(c) for c in code[:-1]], int(code[-1])
    s = sum(v * (3 if (len(body) - 1 - i) % 2 == 0 else 1) for i, v in enumerate(body))
    return (10 - s % 10) % 10 == check


CHAINS = [  # (display name, keywords). Only these names are ever stored in scan statistics.
    ("Osher Ad", ["אושר עד", "osher"]), ("Shufersal", ["שופרסל", "shufersal"]),
    ("Rami Levy", ["רמי לוי", "rami levy"]), ("Victory", ["ויקטורי", "victory"]),
    ("Yochananof", ["יוחננוף", "yochananof"]), ("Carrefour", ["קרפור", "carrefour"]),
    ("Yeinot Bitan", ["יינות ביתן", "bitan"]), ("Tiv Taam", ["טיב טעם", "tiv taam"]),
    ("Hatzi Hinam", ["חצי חינם", "hatzi"]), ("Mega", ["מגה", "mega"]), ("Super-Pharm", ["סופר-פארם", "סופר פארם", "super-pharm"]),
    ("AM:PM", ["am:pm", "am pm", "איי אם"]), ("Tiv Taam", ["tiv-taam"]), ("Machsanei HaShuk", ["מחסני השוק"]),
]


def chain_name(store: str | None) -> str:
    s = (store or "").lower()
    for name, keys in CHAINS:
        if any(k in s for k in keys):
            return name
    return "Other" if s else "Unknown"


def parse_receipt_date(text) -> str | None:
    """'17/09/26', '27.08.2026', '2026-08-27' → '2026-08-27'. Israeli receipts are day/month/year."""
    from datetime import date as _date
    t = str(text or "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.search(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})", t)
        if not m:
            return None
        d, mo, y = map(int, m.groups())
        y += 2000 if y < 100 else 0
    from config import today
    now = today()

    def plausible(yy, mm, dd):
        try:
            dt = _date(yy, mm, dd)
        except ValueError:
            return None
        return dt if (now - dt).days <= 400 and dt <= now else None

    dt = plausible(y, mo, d)
    if dt is None and d <= 99:
        # "17/09/26" misread as 2017-09-26: the "year" was really the day
        dt = plausible(2000 + d if d < 100 else d, mo, y % 100)
    return dt.isoformat() if dt else None


def code_alias(code: str | None, store: str | None) -> str | None:
    """A stable alias for an item code. Barcodes (8+ digits) are global; short codes are per chain.

    A barcode with a wrong check digit was misread, so it's never remembered.
    """
    if not code:
        return None
    if len(code) >= 8:
        return f"code {code}" if barcode_ok(code) is not False else None
    store_key = match_key(store or "")
    return f"code {store_key} {code}" if store_key else None


def assemble(raw: dict) -> ReceiptResult:
    raw = raw if isinstance(raw, dict) else {}
    total = _num(raw.get("total"))
    item_count = _num(raw.get("item_count"))
    clean = _clean_rows(raw)
    rows = _repair(clean, total)
    if total is not None and abs(_priced_sum(rows) - total) > SUM_TOLERANCE:
        # The model may have picked a payment line (e.g. one of two cards) as the total.
        # If the lines add up exactly to another amount printed in the footer, that's the total.
        footer = [a for a in (_num(x) for x in (raw.get("footer_amounts") or [])) if a and a > 0]
        for cand in sorted(set(footer), reverse=True):
            alt = _repair(clean, cand, infer=False)
            if abs(_priced_sum(alt) - cand) <= SUM_TOLERANCE:
                total, rows = cand, alt
                break
    rows_sum = _priced_sum(rows)
    fixed = sum(1 for r in rows if r.get("fixed"))

    counted = 0.0
    deposits = 0.0
    products: list[dict] = []          # in receipt order, before merging
    for r in rows:
        if r["kind"] == "item":
            weighed = r["qty_unit"] == "kg"
            counted += 1 if weighed else (r["qty"] or 1)
            products.append({**r, "weighed": weighed, "lines": 1})
        elif r["kind"] == "deposit":
            counted += r["qty"] or 1
            deposits += r["price"] or 0
        elif r["kind"] == "discount" and products and r["price"] is not None:
            last = products[-1]                        # a credit belongs to the item above it
            last["price"] = round((last["price"] or 0) + r["price"], 2)
        # promo / other rows carry no product of their own

    merged: dict[str, dict] = {}
    order: list[str] = []
    for p in products:
        key = (p["code"] or "") + "|" + (match_key(p["text"]) if not p["code"] else "")
        key += "|kg" if p["weighed"] else ""
        if key in merged:
            m = merged[key]
            m["qty"] = (m["qty"] or (0 if p["weighed"] else 1)) + (p["qty"] or (0 if p["weighed"] else 1))
            if p["price"] is not None:
                m["price"] = round((m["price"] or 0) + p["price"], 2)
            m["lines"] += 1
        else:
            merged[key] = dict(p)
            order.append(key)

    lines = []
    for key in order:
        p = merged[key]
        if p["weighed"]:
            kg = p["qty"] or 0
            size = Size(round(kg * 1000), "g") if kg > 0 else None
            lines.append(ReceiptLine(p["text"], p["code"], 1, size, p["price"], True, p["lines"]))
        else:
            _, size = extract_size(p["text"])
            units = max(1, int(round(p["qty"] or 1)))
            lines.append(ReceiptLine(p["text"], p["code"], units, size, p["price"], False, p["lines"]))

    date = parse_receipt_date(raw.get("date"))
    return ReceiptResult(
        lines=lines, store=str(raw.get("store") or "").strip(), date=date, total=total,
        item_count=item_count, rows_sum=rows_sum, counted=round(counted, 3), deposits=round(deposits, 2),
        merged=len(products) - len(lines), fixed=fixed, raw={"store": raw.get("store"), "date": date,
                                                "total": total, "item_count": item_count, "rows": rows})
