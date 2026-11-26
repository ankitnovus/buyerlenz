"""The Summary report: view it for up to 5 models, or download it as a formatted Excel file (one export)."""
import io
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.activity import log
from app.client_api import Viewer, fail, pick_period, require_client, resolve_entity
from app.cuts_api import MONTHS, allowance, safe_cell
from app.db import get_db
from app.engine import Engine, Scope
from app.report import Report

router = APIRouter(prefix="/api/client", tags=["report"])

MAX_REPORT_MODELS = 5
INDIA = ZoneInfo("Asia/Kolkata")


class ReportRequest(BaseModel):
    quarter: Optional[str] = None
    entities: list[str] = Field(min_length=1, max_length=MAX_REPORT_MODELS)


def build(db: Session, viewer: Viewer, body: ReportRequest) -> dict:
    engine = Engine(db)
    period = pick_period(engine, viewer, body.quarter)
    chosen = [resolve_entity(engine, k, viewer.brand_id) for k in dict.fromkeys(body.entities)]
    columns = [(e["key"], e["label"], Scope(period.scope, e["model_ids"])) for e in chosen]
    report = Report(engine, columns)
    return {
        "quarter": period.info,
        "columns": [{"key": e["key"], "label": e["label"], "mine": e["mine"], "status": report.column_status[e["key"]],
                     **({"n": engine.buyers(s)} if report.visible(e["key"]) else {})}
                    for e, (_, _, s) in zip(chosen, columns)],
        "sections": report.build(),
    }


@router.post("/report")
def view_report(body: ReportRequest, request: Request, db: Session = Depends(get_db),
                viewer: Viewer = Depends(require_client)):
    out = build(db, viewer, body)
    log(db, request, "view_report", viewer.user, quarter=out["quarter"]["label"], entities=body.entities,
        **viewer.log_detail())
    db.commit()
    return out


# ---------- Excel ----------

FILL = {"title": "FF92CDDC", "base": "FFFFFF99", "section": "FFE2CFBC", "average": "FFF4F9C8"}
STATE = {"hidden": f"hidden (fewer than 10)", "no_data": "no data", "not_asked": "not asked",
         "not_offered": "not offered", "none": "", "caution": "small base"}


def cell_value(c: dict):
    """What an Excel cell holds: a real percentage, an average, "Label-41.7%" for top-5 lists, "-" for a true zero."""
    if "text" in c:  # a top-5 answer with its share, or a model's name heading a rejection section
        return safe_cell(f"{c['text']}-{c['pct']:.1f}%" if "pct" in c else c["text"])
    if "pct" in c:
        return "-" if c["pct"] == 0 else c["pct"] / 100
    if "value" in c:
        return c["value"]
    return STATE.get(c.get("status", ""), "")


def workbook(viewer: Viewer, data: dict) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary report"
    cols = data["columns"]
    today = datetime.now(INDIA)
    q = data["quarter"]
    stamp = [
        "BuyerLenz summary report · buyers in this survey, not sales",
        f"{viewer.user.name} · {viewer.company} · downloaded {today.day} {MONTHS[today.month - 1]} {today.year}",
        f"{q['label']} ({q['covered']}) · values are % of each model’s buyers asked the question",
    ]
    for line in stamp:
        ws.append([safe_cell(line)])
    ws.append([])

    def fill(row: int, kind: str, bold: bool = False):
        for col in range(1, len(cols) + 2):
            cell = ws.cell(row=row, column=col)
            cell.fill = PatternFill("solid", fgColor=FILL[kind])
            if bold:
                cell.font = Font(bold=True)

    ws.append([""] + [safe_cell(c["label"]) for c in cols])
    fill(ws.max_row, "title", bold=True)
    ws.append(["Base: All Respondents"] + [c.get("n", STATE.get(c["status"], "")) for c in cols])
    fill(ws.max_row, "base", bold=True)

    for sec in data["sections"]:
        ws.append([safe_cell(sec["title"])] + [""] * len(cols))
        fill(ws.max_row, "section")
        if sec["kind"] == "rejected":
            ws.append(["Model"] + [cell_value(sec["subject"][c["key"]]) for c in cols])
            for col in range(1, len(cols) + 2):
                ws.cell(row=ws.max_row, column=col).font = Font(bold=True)
        for r in sec["rows"]:
            ws.append([safe_cell(r["label"])] + [cell_value(r["cells"][c["key"]]) for c in cols])
            row = ws.max_row
            if r.get("pinned") or r.get("average"):
                fill(row, "average")
            for i, c in enumerate(cols, start=2):
                cell = ws.cell(row=row, column=i)
                if isinstance(cell.value, float) and not r.get("average"):
                    cell.number_format = "0.0%"
                elif r.get("average"):
                    cell.number_format = "#,##0.0"
                cell.alignment = Alignment(horizontal="right" if not isinstance(cell.value, str) else "left")

    ws.column_dimensions["A"].width = 62
    for i in range(len(cols)):
        ws.column_dimensions[get_column_letter(i + 2)].width = 34
    ws.freeze_panes = "B6"
    ws.oddHeader.center.text = stamp[1]  # printed pages carry the stamp too
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


@router.post("/report/download")
def download_report(body: ReportRequest, request: Request, db: Session = Depends(get_db),
                    viewer: Viewer = Depends(require_client)):
    left = allowance(db, viewer.user)
    if left["remaining"] <= 0:
        fail(429, f"Your monthly export allowance is used. It resets on {left['resets']}. "
                  "Viewing is unlimited; your Novus account manager can raise the allowance.")
    data = build(db, viewer, body)
    content = workbook(viewer, data)
    log(db, request, "export", viewer.user, kind="summary_report", quarter=data["quarter"]["label"],
        columns=body.entities, **viewer.log_detail())
    db.commit()
    name = f"BuyerLenz summary report {data['quarter']['label']}.xlsx".replace("–", "to")
    return StreamingResponse(io.BytesIO(content), headers={"Content-Disposition": f'attachment; filename="{name}"'},
                             media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
