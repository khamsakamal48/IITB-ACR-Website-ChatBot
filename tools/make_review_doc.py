#!/usr/bin/env python3
"""Render knowledge/concepts/*.md into ONE Word document for team review.

  uv run --with pyyaml --with python-docx python -I tools/make_review_doc.py \
      --concepts ~/acr-chatbot/knowledge/concepts --out "ACR Chatbot - Topic Pages for Review.docx"

Reviewers read each page, use Word/Google Docs comments or suggested edits for
corrections, and tick Approve / Needs changes in the box after each page.
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
from pathlib import Path

import yaml
from docx import Document
from docx.enum.text import WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

# Display order = master list order in CONCEPT-SPEC.md
GROUPS = {
    "Giving": "ways-of-giving recurring-giving pledges tax-benefits-and-receipts "
              "international-giving gift-types-in-kind-shares-property donation-troubleshooting "
              "donor-privacy-and-transparency causes-to-support institute-development-fund "
              "matching-gifts legacy-giving",
    "Areas you can support": "scholarships-and-student-aid wings-and-womens-scholarships "
              "student-housing-and-hostels faculty-support-and-chair-professorships "
              "young-faculty-fellowship alumni-centre student-wellbeing entrepreneurship "
              "research-and-centres-of-excellence campus-infrastructure go-iitb-and-current-campaign "
              "behind-every-great-mind-campaign growth-india-telescope",
    "Alumni engagement": "reunions batch-legacy class-legacy-projects alumni-events "
              "faculty-alumni-network-fan alumni-association alumni-chapters "
              "alumni-portal-and-directory alumni-communications-and-newsletters asmp-mentorship "
              "volunteering-and-timebank sarc-student-alumni-relations alumni-awards alumni-benefits",
    "Corporate & CSR": "csr-and-corporate-partnerships csr-conclave csr-projects "
              "corporate-partnership-case-studies technology-for-development",
    "About & recognition": "donor-recognition-circles donor-wall about-acr contacts-directory "
              "iit-bombay-facts-and-rankings annual-reports-and-publications careers-at-acr "
              "policies-privacy-and-refunds",
}
INLINE = re.compile(r"(\*\*[^*]+\*\*|\*[^*\s][^*]*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))")
GREY, AMBER = RGBColor(0x66, 0x66, 0x66), RGBColor(0x9A, 0x5B, 0x00)


def split_fm(text: str) -> tuple[dict, str]:
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        return yaml.safe_load(text[4:end]) or {}, text[end + 5:]
    return {}, text


def add_inline(par, text: str, color=None) -> None:
    """Bold / italic / code / [link](url) → runs. Relative links show title only;
    absolute URLs are kept visible so reviewers can check them."""
    for part in INLINE.split(text):
        if not part:
            continue
        if part.startswith("**"):
            r = par.add_run(part[2:-2]); r.bold = True
        elif part.startswith("`"):
            r = par.add_run(part[1:-1]); r.font.name = "Consolas"
        elif part.startswith("["):
            label, url = re.match(r"\[([^\]]+)\]\(([^)]+)\)", part).groups()
            r = par.add_run(label if not url.startswith("http") else f"{label} ({url})")
            r.underline = url.startswith("http")
        elif part.startswith("*"):
            r = par.add_run(part[1:-1]); r.italic = True
        else:
            r = par.add_run(part)
        if color:
            r.font.color.rgb = color


def shade(cell, hex_fill: str) -> None:
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear"); shd.set(qn("w:color"), "auto"); shd.set(qn("w:fill"), hex_fill)
    tcPr.append(shd)


def render_body(doc: Document, body: str) -> None:
    lines = body.strip().splitlines()
    i, in_review = 0, False
    while i < len(lines):
        ln = lines[i].rstrip()
        if not ln.strip():
            i += 1; continue
        if ln.startswith("# "):                          # page H1 already printed as heading
            i += 1; continue
        m = re.match(r"^(#{2,6})\s+(.*)", ln)
        if m:
            title = m.group(2).strip()
            in_review = title.lower().startswith("review notes")
            h = doc.add_heading(("⚠ " if in_review else "") + title, level=min(len(m.group(1)), 4))
            i += 1; continue
        if ln.lstrip().startswith("|"):                  # markdown table
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                    rows.append(cells)
                i += 1
            if rows:
                ncol = max(map(len, rows))
                t = doc.add_table(rows=len(rows), cols=ncol); t.style = "Table Grid"
                for r, cells in enumerate(rows):
                    for c, txt in enumerate(cells):
                        p = t.cell(r, c).paragraphs[0]; add_inline(p, txt)
                        if r == 0:
                            for run in p.runs: run.bold = True
                            shade(t.cell(r, c), "EDEDED")
            continue
        bm = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)", ln)
        if bm:
            lvl = min(len(bm.group(1)) // 2, 2)
            style = ("List Number" if bm.group(2)[0].isdigit() else "List Bullet") + \
                    ("" if lvl == 0 else f" {lvl + 1}")
            p = doc.add_paragraph(style=style)
            add_inline(p, bm.group(3), AMBER if in_review else None)
            i += 1; continue
        para = [ln.strip()]                               # paragraph: join wrapped lines
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(r"^(#|\s*[-*] |\s*\d+\. |\s*\|)", lines[i]):
            para.append(lines[i].strip()); i += 1
        add_inline(doc.add_paragraph(), " ".join(para), AMBER if in_review else None)


def review_box(doc: Document) -> None:
    t = doc.add_table(rows=3, cols=2); t.style = "Table Grid"
    for r, (k, v) in enumerate([("Reviewer / date", ""),
                                ("Decision", "☐ Approve as is     ☐ Approve with my edits     ☐ Needs rework"),
                                ("Missing or wrong (if not marked inline)", "")]):
        t.cell(r, 0).paragraphs[0].add_run(k).bold = True
        shade(t.cell(r, 0), "F3F6FA")
        t.cell(r, 1).paragraphs[0].add_run(v)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--concepts", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()

    pages = {p.stem: split_fm(p.read_text(encoding="utf-8")) for p in a.concepts.glob("*.md")
             if p.name != "index.md"}
    order = [s for g in GROUPS.values() for s in g.split()]
    missing = [s for s in order if s not in pages]
    extra = sorted(set(pages) - set(order))

    doc = Document()
    st = doc.styles["Normal"]; st.font.name = "Calibri"; st.font.size = Pt(10.5)

    doc.add_heading("ACR Website Chatbot — Topic Pages for Review", 0)
    p = doc.add_paragraph(); p.add_run(f"Draft of {dt.date.today():%d %B %Y} · "
                                       f"{len(pages)} pages").italic = True
    doc.add_heading("Why you are reading this", 1)
    for t in [
        "The new chatbot answers visitors from three layers of knowledge: (1) your FAQ sheet, "
        "(2) these topic pages, and (3) the raw website pages. Topic pages pull the website and "
        "the FAQ together so the bot can give one complete, consistent answer on a subject.",
        "These pages were drafted by AI from the website and your FAQ only. The bot will not "
        "treat a page as trustworthy until a team member approves it.",
    ]:
        doc.add_paragraph(t)
    doc.add_heading("How to review (about 5 minutes per page)", 1)
    for t in [
        "Check facts, amounts, links and email addresses. Correct anything wrong directly "
        "(suggested edits / track changes) or add a comment.",
        "Look at the amber ⚠ Review notes at the end of each page: these are conflicts or gaps "
        "the AI found. Please answer them in a comment.",
        "Add anything a visitor would commonly ask that is missing.",
        "Tick a decision in the box after each page. Pages you approve go live in the bot.",
        "Tone does not matter much here — the bot rewrites answers in our voice. Focus on facts.",
    ]:
        doc.add_paragraph(t, style="List Number")

    doc.add_heading("Contents", 1)
    for g, slugs in GROUPS.items():
        present = [s for s in slugs.split() if s in pages]
        if present:
            p = doc.add_paragraph(); p.add_run(g + ": ").bold = True
            p.add_run(", ".join(str(pages[s][0].get("title", s)) for s in present))

    for g, slugs in GROUPS.items():
        present = [s for s in slugs.split() if s in pages] + ([s for s in extra] if g == "About & recognition" else [])
        if not present:
            continue
        doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
        doc.add_heading(g, 0)
        for n, s in enumerate(present):
            meta, body = pages[s]
            if n:
                doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
            doc.add_heading(str(meta.get("title", s)), 1)
            sp = doc.add_paragraph()
            add_inline(sp, f"*{meta.get('description', '')}*", GREY)
            render_body(doc, body)
            srcs = [x for x in (meta.get("sources") or [])]
            if srcs:
                doc.add_heading("Sources used", 4)
                for x in srcs:
                    r = doc.add_paragraph(style="List Bullet").add_run(str(x))
                    r.font.size = Pt(8.5); r.font.color.rgb = GREY
            doc.add_paragraph()
            review_box(doc)

    a.out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(a.out)
    print(f"wrote {a.out} — {len(pages)} pages")
    if missing:
        print("missing (not yet written):", ", ".join(missing))
    if extra:
        print("extra (not in master list):", ", ".join(extra))


if __name__ == "__main__":
    main()
