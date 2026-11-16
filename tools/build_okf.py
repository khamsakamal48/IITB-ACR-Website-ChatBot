#!/usr/bin/env python3
"""Build the ACR chatbot knowledge bundle (OKF v0.2 layout) from local inputs.

Inputs  (never committed to git — knowledge-base data):
  --scrape  site-scrape/output/        one Markdown file per WP item (+ manifest.csv)
  --faq     "Chatbot FAQ ACR.xlsx"     team-authored FAQ sheet
Output:
  --out     knowledge/
    index.md                       root map (goes into the chat prompt)
    faq/<category>.md              one OKF file per FAQ category  (type: faq, verified)
    sources/<type>/<slug>.md       scraped pages, frontmatter normalised to OKF
    sources/<type>/index.md        per-type listing (agent browses these)
    concepts/                      compiled topic pages — never touched here

Re-runnable: regenerates faq/ and sources/ from scratch, leaves concepts/ alone.
File formats live in trafilatura-service/okf.py (shared with the live knowledge service).

  uv run --with pyyaml --with openpyxl python -I tools/build_okf.py \
      --scrape site-scrape/output --faq "Chatbot FAQ ACR.xlsx" --out knowledge
"""
from __future__ import annotations

import argparse
import collections
import csv
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "trafilatura-service"))
import okf  # noqa: E402

import openpyxl  # noqa: E402


def read_faq_xlsx(xlsx: Path) -> list[dict]:
    """Sheet columns (matched loosely): Category | Subcategory | User Question |
    Alternative User Query | Answer | Relevant Website Page."""
    ws = openpyxl.load_workbook(xlsx, data_only=True).active
    rows = list(ws.iter_rows(values_only=True))
    head = [str(h or "").strip().lower() for h in rows[0]]

    def col(*names):
        for n in names:
            for i, h in enumerate(head):
                if n in h:
                    return i
        raise SystemExit(f"FAQ sheet: no column like {names}; header = {head}")

    idx = dict(category=col("category"), subcategory=col("subcategory"),
               question=col("user question"), alternative=col("alternative"),
               answer=col("answer"), page=col("relevant", "page"))
    return [{k: r[i] for k, i in idx.items()} for r in rows[1:]]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scrape", type=Path, required=True)
    ap.add_argument("--faq", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()

    shutil.rmtree(a.out / "sources", ignore_errors=True)    # regenerate; keep concepts/
    a.out.mkdir(parents=True, exist_ok=True)

    counts: dict[str, int] = collections.Counter()
    no_desc = 0
    for src in sorted(a.scrape.glob("*/*.md")):
        folder = src.parent.name
        meta, body = okf.split_frontmatter(src.read_text(encoding="utf-8"))
        doc = okf.source_doc(meta, body, folder)
        no_desc += not doc.get("description")
        okf.write_atomic(a.out / "sources" / folder / src.name, okf.dump(doc, body))
        counts[folder] += 1
    for folder in counts:
        okf.write_folder_index(a.out / "sources" / folder)

    cats = okf.write_faq(read_faq_xlsx(a.faq), a.out / "faq", a.faq.name)
    okf.write_root_index(a.out)

    if (a.scrape / "manifest.csv").exists():
        rows = list(csv.DictReader(open(a.scrape / "manifest.csv", encoding="utf-8")))
        skipped = collections.Counter(r["type"] for r in rows if r["status"] == "empty")
        print("scrape items skipped as empty:", dict(skipped.most_common()))
    print(f"sources: {sum(counts.values())} files in {len(counts)} types ({no_desc} without description)")
    print(f"faq: {sum(cats.values())} Q&As in {len(cats)} categories")
    print(f"wrote {a.out.resolve()}")


if __name__ == "__main__":
    main()
