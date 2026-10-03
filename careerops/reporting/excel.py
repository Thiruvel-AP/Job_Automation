"""Excel export — a read-only view of the SQLite state (keeps the 'companies.xlsx' habit without
making a spreadsheet the database)."""
from __future__ import annotations

import json
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from ..db import DB
from ..tailoring.queue import queue_key

COLS = [("Company", "company", 26), ("Title", "title", 38), ("Tier", "tier", 6), ("Family", "family", 15),
        ("Location", "location", 22), ("Status", "status", 13),
        ("Reason", "reason", 34), ("Flags", "flags", 40), ("Sponsor (register)", "sponsor_name", 30),
        ("Sponsor match", "sponsor_score", 9), ("ATS literal", "score", 9), ("ATS stemmed", "score_stemmed", 9),
        ("Ceiling (pre)", "ceiling_pre", 9), ("Honest ceiling", "ceiling", 9), ("Letter", "letter_score", 8), ("Attempts", "attempts", 8),
        ("Source", "source", 11), ("URL", "url", 40), ("Output folder", "out_dir", 40),
        ("Application folder", "application_dir", 44),
        ("Applied at", "applied_at", 20), ("Confirmation", "confirmation", 20),
        ("First seen", "first_seen", 20), ("Last seen", "last_seen", 20)]

FILL = {"applied": "C6EFCE", "approved": "DDEBF7", "needs_review": "FFF2CC", "rejected": "F8CBAD",
        "skipped": "EDEDED", "needs_jd": "FCE4D6", "error": "F4B084"}
PCT = {"sponsor_score", "score", "score_stemmed", "ceiling_pre", "ceiling", "letter_score"}
STATUS_COL = next(i for i, c in enumerate(COLS, 1) if c[1] == "status")


def export(db: DB, path: Path, floor: float = 0.70) -> int:
    """Rows sorted by the post-tailoring queue key (floor, tier, honest ceiling, freshness)."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Jobs"
    ws.append([c[0] for c in COLS])
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="14285A")
    rows = sorted(db.jobs(), key=lambda r: queue_key(r, floor, tailored=True))
    for r in rows:
        vals = []
        for _, key, _ in COLS:
            v = r[key]
            if key == "flags":
                v = " | ".join(json.loads(v or "[]"))
            elif key == "application_dir" and r["status"] != "applied":
                v = None                                   # only applied rows have an archive folder to show
            vals.append(v)
        ws.append(vals)
        fill = FILL.get(r["status"])
        if fill:
            ws.cell(ws.max_row, STATUS_COL).fill = PatternFill("solid", fgColor=fill)
    for i, (_, key, w) in enumerate(COLS, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
        if key in PCT:
            for row in ws.iter_rows(min_row=2, min_col=i, max_col=i):
                row[0].number_format = "0%"
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = ws.dimensions

    st = wb.create_sheet("Summary")
    st.append(["Status", "Count"])
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    for k, v in sorted(counts.items()):
        st.append([k, v])
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return len(rows)
