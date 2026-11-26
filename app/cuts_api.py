"""Data cuts: up to 5 questions x 25 columns, a trend for one answer, saved cuts, and Copy to Excel.

The caps are enforced here, not just in the browser. Copy to Excel builds the rows on the server, counts
against the user's monthly allowance and is written to the activity log.
"""
from datetime import datetime, timezone
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.activity import log
from app.catalogue import BY_CODE
from app.client_api import FILTERS, Viewer, fail, pick_period, require_client, resolve_entity
from app.db import get_db
from app.engine import Engine, Scope
from app.models import ActivityLog, SavedCut, User
from app.rules import EXPORT_LIMIT, MAX_CUT_COLUMNS, MAX_CUT_QUESTIONS, MAX_TREND_LINES, MIN_SHOW, sample_status
from app.views import PROFILE_KINDS, cut_block, series

router = APIRouter(prefix="/api/client", tags=["cuts"])

INDIA = ZoneInfo("Asia/Kolkata")
MARKET_KEY, MARKET_LABEL = "all", "All new-car buyers"
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# ---------- export allowance ----------

def allowance(db: Session, user: User) -> dict:
    now = datetime.now(INDIA)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    used = db.scalar(select(func.count()).select_from(ActivityLog).where(
        ActivityLog.user_id == user.id, ActivityLog.action == "export",
        ActivityLog.at >= month_start.astimezone(timezone.utc)))
    next_month = MONTHS[now.month % 12]
    return {"used": used, "limit": EXPORT_LIMIT, "remaining": max(0, EXPORT_LIMIT - used), "resets": f"1 {next_month}"}


@router.get("/allowance")
def get_allowance(db: Session = Depends(get_db), viewer: Viewer = Depends(require_client)):
    return allowance(db, viewer.user)


# ---------- building a cut ----------

class CutRequest(BaseModel):
    quarter: Optional[str] = None
    compare: str = "off"
    questions: List[str] = Field(min_length=1, max_length=MAX_CUT_QUESTIONS)
    columns: List[str] = Field(min_length=1, max_length=MAX_CUT_COLUMNS)
    filters: Dict[str, int] = Field(default={}, max_length=10)


def check_questions(codes: list) -> list:
    codes = list(dict.fromkeys(codes))
    for code in codes:
        q = BY_CODE.get(code)
        if q is None or q.kind not in PROFILE_KINDS:
            fail(400, f"“{code}” can’t be used in a data cut.")
    return codes


def check_filters(engine: Engine, filters: dict) -> tuple:
    out = []
    for code, option_id in filters.items():
        allowed = {o.id for o in engine.options_for(code)} if code in dict(FILTERS) else set()
        if option_id not in allowed:
            fail(400, "Unknown filter.")
        out.append((option_id,))
    return tuple(out)


def build(db: Session, viewer: Viewer, body: CutRequest) -> dict:
    engine = Engine(db)
    period = pick_period(engine, viewer, body.quarter, body.compare)
    q, cmp = period.scope, period.compare
    codes = check_questions(body.questions)
    filters = check_filters(engine, body.filters)
    chosen = [resolve_entity(engine, k, viewer.brand_id) for k in dict.fromkeys(body.columns)]
    columns = [(MARKET_KEY, MARKET_LABEL, Scope(q, None, filters))] + \
              [(e["key"], e["label"], Scope(q, e["model_ids"], filters)) for e in chosen]

    def base(scope: Scope) -> dict:
        n = engine.buyers(scope)
        return {"status": "no_data" if n == 0 else sample_status(n), **({"n": n} if n >= MIN_SHOW else {})}

    info = [{"key": MARKET_KEY, "kind": "market", "label": MARKET_LABEL, "mine": False, "base": base(columns[0][2])}] + \
           [{**{k: e[k] for k in ("key", "kind", "label", "mine")}, "base": base(s)} for e, (_, _, s) in zip(chosen, columns[1:])]
    return {
        "quarter": period.info,
        "compare": period.compare_info,
        "filters": {code: next(o.label for o in engine.options_for(code) if o.id == oid) for code, oid in body.filters.items()},
        "columns": info,
        "blocks": [cut_block(engine, columns, code, cmp) for code in codes],
    }


@router.post("/cut")
def cut(body: CutRequest, request: Request, db: Session = Depends(get_db), viewer: Viewer = Depends(require_client)):
    out = build(db, viewer, body)
    log(db, request, "view_cut", viewer.user, quarter=out["quarter"]["label"], questions=body.questions,
        columns=body.columns, filters=body.filters, **viewer.log_detail())
    db.commit()
    return out


class TrendRequest(BaseModel):
    quarter: Optional[str] = None
    columns: List[str] = Field(min_length=1, max_length=MAX_TREND_LINES)
    question: str
    answer: str
    filters: Dict[str, int] = Field(default={}, max_length=10)


@router.post("/cut/trend")
def cut_trend(body: TrendRequest, request: Request, db: Session = Depends(get_db),
              viewer: Viewer = Depends(require_client)):
    engine = Engine(db)
    period = pick_period(engine, viewer, body.quarter)
    q = period.scope
    check_questions([body.question])
    filters = check_filters(engine, body.filters)
    span = list(period.trend)  # one point per quarter, the trailing four
    lines = []
    for key in dict.fromkeys(body.columns):
        e = resolve_entity(engine, key, viewer.brand_id)
        lines.append({"key": key, "label": e["label"], "mine": e["mine"],
                      "points": series(engine, Scope(q, e["model_ids"], filters), body.question, body.answer, span)})
    market = series(engine, Scope(q, None, filters), body.question, body.answer, span)
    log(db, request, "view_cut_trend", viewer.user, question=body.question, answer=body.answer, columns=body.columns,
        **viewer.log_detail())
    db.commit()
    return {"quarters": [next(x for x in period.quarters if x["id"] == t) for t in span], "lines": lines,
            "market": {"key": MARKET_KEY, "label": MARKET_LABEL, "points": market}}


# ---------- Copy to Excel ----------

class ExportRequest(CutRequest):
    preview: bool = False  # True: show what would be copied, without counting it


FLAGS = {"caution": "small base", "hidden": f"hidden (fewer than {MIN_SHOW})", "no_data": "no data", "not_asked": "not asked"}


def safe_cell(value):
    """Text that Excel would read as a formula (starting =, +, -, @) gets a leading apostrophe, so a label
    in an uploaded file can never run as a formula on a client's computer. Tabs and line breaks become
    spaces so the pasted table keeps its shape."""
    if not isinstance(value, str):
        return value
    value = value.replace("\t", " ").replace("\r", " ").replace("\n", " ")
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value


def excel_rows(viewer: Viewer, cut_data: dict) -> list:
    """Real numbers, base sizes and flags on their own rows, stamped with user, company and date on top."""
    q, cols = cut_data["quarter"], cut_data["columns"]
    today = datetime.now(INDIA)
    filters = "".join(f" · {v}" for v in cut_data["filters"].values())
    rows = [
        ["BuyerLenz data cut · buyers in this survey, not sales"],
        [f"{viewer.user.name} · {viewer.company} · copied {today.day} {MONTHS[today.month - 1]} {today.year}"],
        [f"{q['label']} ({q['covered']}){' · filtered:' + filters if filters else ''} · values are % of each column’s buyers"],
        ["Question", "Answer", *[c["label"] for c in cols]],
    ]
    for b in cut_data["blocks"]:
        cells = [b["cells"][c["key"]] for c in cols]
        rows.append([f"{b['code']} {b['title']}", "Base (n)", *[c.get("n", "") for c in cells]])
        for a in b["answers"]:
            rows.append(["", a, *[round(c["values"][a]["pct"]) if a in c.get("values", {})
                                  else "not offered" if a in c.get("not_offered", []) else "" for c in cells]])
        rows.append(["", "Flag", *[FLAGS.get(c["status"], "") for c in cells]])
    return [[safe_cell(v) for v in row] for row in rows]


@router.post("/cut/export")
def export(body: ExportRequest, request: Request, db: Session = Depends(get_db),
           viewer: Viewer = Depends(require_client)):
    user = viewer.user
    left = allowance(db, user)
    if not body.preview and left["remaining"] <= 0:
        fail(429, f"Your monthly export allowance is used. It resets on {left['resets']}. "
                  "Viewing is unlimited; your Novus account manager can raise the allowance.")
    out = build(db, viewer, body)
    rows = excel_rows(viewer, out)
    if not body.preview:
        log(db, request, "export", user, quarter=out["quarter"]["label"], questions=body.questions, columns=body.columns,
            filters=body.filters, rows=len(rows), **viewer.log_detail())
        db.commit()
        left = allowance(db, user)
    return {"rows": rows, "allowance": left}


# ---------- saved cuts ----------

class SaveRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    questions: List[str] = Field(min_length=1, max_length=MAX_CUT_QUESTIONS)
    columns: List[str] = Field(min_length=1, max_length=MAX_CUT_COLUMNS)
    filters: Dict[str, int] = Field(default={}, max_length=10)


def describe(s: SavedCut) -> dict:
    return {"id": s.id, "name": s.name, "questions": s.questions, "columns": s.columns, "filters": s.filters}


@router.get("/saved-cuts")
def saved_cuts(db: Session = Depends(get_db), viewer: Viewer = Depends(require_client)):
    return [describe(s) for s in db.scalars(select(SavedCut).where(SavedCut.user_id == viewer.user.id).order_by(SavedCut.id.desc()))]


@router.post("/saved-cuts")
def save_cut(body: SaveRequest, request: Request, db: Session = Depends(get_db),
             viewer: Viewer = Depends(require_client)):
    engine, user = Engine(db), viewer.user
    check_questions(body.questions)
    check_filters(engine, body.filters)
    for k in body.columns:
        resolve_entity(engine, k, viewer.brand_id)
    s = SavedCut(user_id=user.id, name=body.name.strip(), questions=body.questions, columns=body.columns, filters=body.filters)
    db.add(s)
    log(db, request, "save_cut", user, name=s.name)
    db.commit()
    return describe(s)


@router.delete("/saved-cuts/{cut_id}")
def delete_cut(cut_id: int, request: Request, db: Session = Depends(get_db),
               viewer: Viewer = Depends(require_client)):
    user = viewer.user
    s = db.get(SavedCut, cut_id)
    if s is None or s.user_id != user.id:
        fail(404, "That saved cut doesn’t exist.")
    db.delete(s)
    log(db, request, "delete_cut", user, name=s.name)
    db.commit()
    return {"ok": True}
