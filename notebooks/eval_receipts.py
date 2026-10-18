"""How accurately does the receipt reader read real receipts?

Compares the real vision model's reading of each receipt with a hand transcription
(`data/receipts/<name>.truth.json`, next to the receipt file it names). The `data/` folder is
gitignored, so your receipts never reach the public repo.

Run from the project folder, with your key in the environment (PowerShell):
    $env:OPENAI_API_KEY = "sk-..."          # paste it in the terminal only, never in chat
    python notebooks/eval_receipts.py                     # all receipts
    python notebooks/eval_receipts.py carrefour receipt3  # just these
Optional: $env:OPENAI_VISION_MODEL = "gpt-4o"  to compare models.

Per receipt it reports:
  found     share of printed product lines read with the right code AND price
  extra     lines the model invented or duplicated
  qty/kg    lines whose quantity or weight was misread
  sum       whether the reading adds up to the printed total (✓) and by how much it's off
  count     whether units match the printed item count
  2nd look  whether the second pass was needed
  repaired  rows fixed in code using the printed total (e.g. a price split across pages)
"""
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from normalize import match_key  # noqa: E402
from receipt_reader import ReceiptReader, _clean_rows  # noqa: E402

FOLDER = ROOT / "data" / "receipts"


def key(r):
    return (r["code"] or match_key(r["text"]), round(r["price"] or 0, 2))


def compare(truth_rows, read_rows):
    t_items = [r for r in truth_rows if r["kind"] in ("item", "deposit")]
    r_items = [r for r in read_rows if r["kind"] in ("item", "deposit")]
    t, r = Counter(map(key, t_items)), Counter(map(key, r_items))
    found = sum((t & r).values())
    qty_bad = 0
    by_key = {key(x): x for x in r_items}
    for x in t_items:
        y = by_key.get(key(x))
        if y and abs((y["qty"] or 0) - (x["qty"] or 0)) > 0.005:
            qty_bad += 1
    missed = list((t - r).elements())
    extra = list((r - t).elements())
    return found, len(t_items), len(extra), qty_bad, missed, extra


def main(names):
    reader_model = ReceiptReader().model
    print(f"Vision model: {reader_model}\n")
    files = sorted(FOLDER.glob("*.truth.json"))
    if names:
        files = [f for f in files if f.name.split(".")[0] in names]
    if not files:
        sys.exit(f"No *.truth.json files in {FOLDER}")
    totals = Counter()
    report = []
    print(f"{'receipt':<11}{'found':>12}{'extra':>7}{'qty/kg':>8}{'sum':>12}{'count':>8}{'2nd look':>10}"
          f"{'repaired':>10}{'secs':>7}")
    for f in files:
        truth = json.loads(f.read_text(encoding="utf-8"))
        path = FOLDER / truth["file"]
        if not path.exists():
            print(f"{f.name.split('.')[0]:<11} (missing {path.name}: copy the receipt file there)")
            continue
        reader = ReceiptReader()
        t0 = time.time()
        try:
            res = reader.read(path.read_bytes(), path.name)
        except Exception as e:  # report and carry on with the next receipt
            print(f"{f.name.split('.')[0]:<11} FAILED: {e!r}")
            continue
        secs = time.time() - t0
        found, n, extra, qty_bad, missed, extra_rows = compare(_clean_rows(truth), res.raw["rows"])
        sum_txt = "✓" if not res.sum_mismatch else f"off {res.missing_amount:+.2f}"
        cnt_txt = "✓" if not res.count_mismatch else f"{res.counted:g}/{res.item_count:g}"
        print(f"{f.name.split('.')[0]:<11}{found:>5}/{n:<3} {found / n:>4.0%}{extra:>5}{qty_bad:>8}{sum_txt:>12}"
              f"{cnt_txt:>8}{'yes' if res.rechecked else '-':>10}{res.fixed or '-':>10}{secs:>7.1f}")
        for m in missed[:8]:
            print(f"{'':11}  missed/misread: {m}")
        for x in extra_rows[:5]:
            print(f"{'':11}  extra: {x}")
        totals.update(found=found, n=n, extra=extra, qty=qty_bad, ok=int(not res.sum_mismatch), receipts=1)
        report.append({"receipt": f.name.split(".")[0], "found": found, "lines": n, "extra": extra,
                       "qty_wrong": qty_bad, "sum_ok": not res.sum_mismatch, "count_ok": not res.count_mismatch,
                       "rechecked": res.rechecked, "seconds": round(secs, 1), "calls": res.calls,
                       "repaired": res.fixed, "missed": [list(m) for m in missed],
                       "extra_rows": [list(x) for x in extra_rows], "model_answers": res.answers})
    if totals["n"]:
        print(f"\nOverall: {totals['found']}/{totals['n']} lines read exactly ({totals['found'] / totals['n']:.1%}), "
              f"{totals['extra']} extra, {totals['qty']} quantity errors; "
              f"{totals['ok']}/{totals['receipts']} receipts add up to their printed total.")
        out = FOLDER / f"eval_{reader_model}.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"Details saved to {out}")


if __name__ == "__main__":
    main(sys.argv[1:])
