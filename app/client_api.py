"""Client screens. A client only ever sees published data, for the quarters their company has paid for,
with their own brand marked "Yours". Novus staff can "View as client" for any brand (all quarters).
"""
from dataclasses import dataclass
from typing import Dict, List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.activity import log
from app.auth import current_session, require_user
from app.catalogue import BY_CODE, QUESTIONS, SATISFIED_LABEL
from app.db import get_db
from app.engine import Engine, Scope
from app.layout import home_of, profile_of
from app.models import Brand, CarModel, LoginSession, User, UserLayout
from app.rules import FLAT, MIN_SHOW, MIN_SOLID, sample_status
from app.views import THEMES, PROFILE_KINDS, amount_tile, bars, movers, profile_card, standouts

router = APIRouter(prefix="/api/client", tags=["client"])

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def fail(status: int, message: str):
    raise HTTPException(status, detail={"code": "refused", "message": message})


@dataclass(frozen=True)
class Viewer:
    """Who is looking at the client screens, and what they may see."""
    user: User
    brand_id: int
    company: str  # shown on stamps and the watermark
    removed: frozenset = frozenset()  # quarters taken away from this client; Novus staff see all
    staff: bool = False
    view_as: Optional[str] = None  # the brand Novus staff are viewing as

    def log_detail(self) -> dict:
        """Extra activity-log detail, so staff views are never mistaken for a client's."""
        return {"view_as": self.view_as} if self.staff else {}


def require_client(s: LoginSession = Depends(current_session), user: User = Depends(require_user)) -> Viewer:
    if user.role == "admin":
        if s.view_as_brand_id is None:
            fail(403, "Choose a brand under “View as client” on the admin Overview first.")
        return Viewer(user, s.view_as_brand_id, f"Novus Insights, viewing as {s.view_as_brand.name}", staff=True,
                      view_as=s.view_as_brand.name)
    if user.company.brand_id is None:
        fail(403, "Your company account has no brand set up yet. Please contact Novus Insights.")
    return Viewer(user, user.company.brand_id, user.company.name, frozenset(user.company.removed_quarters or []))


def visible_quarters(engine: Engine, viewer: Viewer) -> list:
    """Published quarters this viewer may see, oldest first: all of them, minus any removed."""
    return [quarter_info(r) for r in engine.published_quarters() if r[0] not in viewer.removed]


def quarter_info(row) -> dict:
    q, buyers, first, last = row
    year, k = int(q[:4]), int(q[5])
    covered = (f"{MONTHS[first.month - 1]} {year} only" if first.month == last.month
               else f"{MONTHS[first.month - 1]}–{MONTHS[last.month - 1]} {year}")
    return {"id": q, "label": f"Q{k} {year}", "months": f"{MONTHS[3 * k - 3]}–{MONTHS[3 * k - 1]} {year}",
            "covered": covered, "partial": last.month - first.month < 2, "buyers": buyers,
            "from": f"{MONTHS[first.month - 1]} {first.year}", "to": f"{MONTHS[last.month - 1]} {last.year}"}


YEAR_QUARTERS = 4  # the default view: the trailing four quarters together
TREND_QUARTERS = 4  # trends: one point per quarter for the trailing four


def year_info(quarters: list) -> dict:
    """The trailing year as one period, e.g. 'Q3 2025 – Q2 2026', covering Sep 2025 – May 2026."""
    first, last = quarters[0], quarters[-1]
    n = len(quarters)
    return {"id": "year", "kind": "year", "label": first["label"] if n == 1 else f"{first['label']} – {last['label']}",
            "months": f"{first['months'].split('–')[0]} {first['id'][:4]} – {last['months'].split('–')[1]}" if n > 1 else first["months"],
            "covered": first["covered"] if n == 1 else f"{first['from']} – {last['to']}", "partial": n < YEAR_QUARTERS,
            "quarters": n, "buyers": sum(q["buyers"] for q in quarters), "from": first["from"], "to": last["to"]}


@dataclass(frozen=True)
class Period:
    """What a screen shows: the trailing year (default) or one quarter, with an optional comparison quarter."""
    scope: Union[str, tuple]  # passed to Scope: one quarter, or the year's quarters
    compare: Optional[str]  # comparison quarter (quarter-on-quarter only)
    info: dict  # label, months covered, partial...
    compare_info: Optional[dict]
    quarters: list  # every quarter this viewer may see
    trend: tuple  # the trailing four quarters, for trends


def pick_period(engine: Engine, viewer: Viewer, quarter: Optional[str], compare: str = "off") -> Period:
    """quarter empty or "year": the trailing four quarters, no changes shown. A quarter id: that quarter,
    compared with `compare` (another quarter id, or "prev" / "yoy")."""
    quarters = visible_quarters(engine, viewer)
    ids = [q["id"] for q in quarters]
    if not ids:
        fail(404, "No survey results have been published yet.")
    trend = tuple(ids[-TREND_QUARTERS:])
    if not quarter or quarter == "year":
        year = quarters[-YEAR_QUARTERS:]
        return Period(tuple(q["id"] for q in year), None, year_info(year), None, quarters, trend)
    if quarter not in ids:
        fail(404, "That quarter isn’t available.")
    cmp = compare if compare in ids else comparison_quarter(quarter, compare)
    cmp = cmp if cmp in ids and cmp != quarter else None
    info = {**next(q for q in quarters if q["id"] == quarter), "kind": "quarter"}
    return Period(quarter, cmp, info, next((q for q in quarters if q["id"] == cmp), None), quarters, trend)


def comparison_quarter(q: str, compare: str) -> str:
    year, k = int(q[:4]), int(q[5])
    if compare == "prev":
        return f"{year - 1}Q4" if k == 1 else f"{year}Q{k - 1}"
    if compare == "yoy":
        return f"{year - 1}Q{k}"
    return ""


def brand_scope(db: Session, viewer: Viewer, quarter: str) -> tuple:
    brand = db.get(Brand, viewer.brand_id)
    model_ids = tuple(db.scalars(select(CarModel.id).where(CarModel.brand_id == brand.id)))
    return brand, Scope(quarter, model_ids)


FILTERS = (("E10", "Fuel"), ("D2", "Buyer type"), ("B1", "Age"))  # city and region join when files include them


@router.get("/context")
def context(db: Session = Depends(get_db), viewer: Viewer = Depends(require_client)):
    engine = Engine(db)
    brand = db.get(Brand, viewer.brand_id)
    quarters = visible_quarters(engine, viewer)
    filters = [{"code": code, "label": label, "options": [{"id": o.id, "label": o.label} for o in engine.options_for(code)]}
               for code, label in FILTERS if code in engine.questions and engine.options_for(code)]
    questions = [{"code": q.code, "title": engine.questions[q.code].title, "wording": engine.questions[q.code].wording,
                  "kind": q.kind, "theme": q.theme}
                 for q in QUESTIONS if q.kind in PROFILE_KINDS and q.code in engine.questions]
    brand_key = f"b:{brand.id}"
    return {"brand": brand.name, "brand_key": brand_key, "company": viewer.company, "user": viewer.user.name,
            "staff_view": viewer.staff, "quarters": quarters, "filters": filters, "questions": questions}


def kpi(engine: Engine, scope: Scope, code: str, headline: str, compare: Optional[str]) -> dict:
    """A headline tile: one answer's share for the brand, vs all buyers, with change."""
    b = bars(engine, scope, code, compare)
    out = {k: v for k, v in b.items() if k != "options"}
    if "options" in b:
        out["headline"] = next((o for o in b["options"] if o["label"] == headline), None)
        if BY_CODE[code].rating:
            out["distribution"] = [o for o in b["options"] if o["label"] != SATISFIED_LABEL]
    return out


def tile(engine: Engine, scope: Scope, spec: dict, compare: Optional[str]) -> dict:
    """One Home tile from a layout entry: share of an answer, satisfaction, or typical amount."""
    if spec["kind"] == "amount":
        data = amount_tile(engine, scope, spec["question"], compare)
    elif spec["kind"] == "satisfaction":
        data = kpi(engine, scope, spec["question"], SATISFIED_LABEL, compare)
    else:
        data = kpi(engine, scope, spec["question"], spec["answer"], compare)
        if data["status"] == "ok" or data["status"] == "caution":
            if data.get("headline") is None:  # that answer isn't given (or offered) this quarter
                data = {k: data[k] for k in ("code", "title", "wording")} | {"status": "no_data"}
    return {**spec, "data": data}


def chart(engine: Engine, scope: Scope, code: str, compare: Optional[str]) -> dict:
    """One Home chart: the question's answers as bars. Long lists show their top 5."""
    q = BY_CODE[code]
    return {**bars(engine, scope, code, compare, top=5 if q.kind in ("multi", "slots", "reject") else None),
            "kind": q.kind}


def buyers_tile(engine: Engine, scope: Scope, compare: Optional[str]) -> dict:
    n, market = engine.buyers(scope), engine.buyers(scope.market())
    status = "no_data" if n == 0 else sample_status(n)
    if status in ("hidden", "no_data"):
        return {"status": status}
    out = {"status": status, "n": n, "share_pct": round(n / market * 100, 1)}
    if compare:
        before, before_market = engine.buyers(scope.in_quarter(compare)), engine.buyers(scope.market().in_quarter(compare))
        if before == 0:
            out["change"] = {"kind": "new"}
        elif n < MIN_SOLID or before < MIN_SOLID:
            out["change"] = {"kind": "few"}
        else:
            points = round(n / market * 100 - before / before_market * 100, 1)
            out["change"] = {"kind": "change", "points": points, "flat": abs(points) <= FLAT}
    return out


def top_models(engine: Engine, db: Session, scope: Scope, compare: Optional[str], limit: int = 5) -> list:
    now = engine.buyers_by_model(scope.quarter, scope.model_ids)
    before = engine.buyers_by_model(compare, scope.model_ids) if compare else {}
    names = {m.id: m for m in db.scalars(select(CarModel).where(CarModel.id.in_(now.keys())))}
    out = []
    for model_id, n in sorted(now.items(), key=lambda x: -x[1])[:limit]:
        m = names[model_id]
        row = {"id": model_id, "name": m.name, "segment": m.segment, "status": sample_status(n)}
        if n >= MIN_SHOW:
            row["n"] = n
        if compare:
            p = before.get(model_id, 0)
            if p == 0:
                row["change"] = {"kind": "new"}
            elif n >= MIN_SHOW and p >= MIN_SHOW:  # never let a change reveal a hidden count
                row["change"] = {"kind": "count", "buyers": n - p, "flat": abs(n - p) < max(3, p * 0.05)}
        out.append(row)
    return out


@router.get("/home")
def home(request: Request, quarter: Optional[str] = None, compare: str = "off",
         db: Session = Depends(get_db), viewer: Viewer = Depends(require_client)):
    engine = Engine(db)
    if not visible_quarters(engine, viewer):
        return {"quarters": [], "empty": True}
    period = pick_period(engine, viewer, quarter, compare)
    cmp = period.compare

    brand, scope = brand_scope(db, viewer, period.scope)
    saved = db.get(UserLayout, viewer.user.id)
    layout = home_of(saved.home if saved else None)
    log(db, request, "view_home", viewer.user, quarter=period.info["label"], compare=cmp, **viewer.log_detail())
    db.commit()
    return {
        "brand": brand.name,
        "quarter": period.info,
        "compare": period.compare_info,
        "market_buyers": engine.buyers(scope.market()),
        "customised": bool(saved and saved.home),
        "buyers": buyers_tile(engine, scope, cmp),
        "tiles": [tile(engine, scope, t, cmp) for t in layout["tiles"] if t["question"] in engine.questions],
        "charts": [chart(engine, scope, c, cmp) for c in layout["charts"] if c in engine.questions],
        "top_models": top_models(engine, db, scope, cmp) if layout["top_models"] else None,
        "movers": (movers(engine, scope, cmp) if cmp else None) if layout["movers"] else None,
        "show_movers": layout["movers"],
    }


# ---------- Buyer Profile ----------

MAX_PROFILE = 8


def resolve_entity(engine: Engine, key: str, own_brand_id: int) -> dict:
    """'m:12' (a model) or 'b:3' (a brand) -> label, description and the model ids it covers."""
    kind, _, raw = key.partition(":")
    if not raw.isdigit():
        fail(400, f"Unknown selection “{key}”.")
    if kind == "m" and int(raw) in engine.models:
        m = engine.models[int(raw)]
        return {"key": key, "kind": "model", "label": m.name, "sub": m.brand.name + (f" · {m.segment}" if m.segment else ""),
                "mine": m.brand_id == own_brand_id, "model_ids": (m.id,)}
    if kind == "b" and int(raw) in engine.brands:
        b = engine.brands[int(raw)]
        ids = tuple(sorted(mid for mid, m in engine.models.items() if m.brand_id == b.id))
        return {"key": key, "kind": "brand", "label": b.name, "sub": "Brand · all models",
                "mine": b.id == own_brand_id, "model_ids": ids}
    fail(400, f"Unknown selection “{key}”.")


@router.get("/entities")
def entities(quarter: Optional[str] = None, db: Session = Depends(get_db), viewer: Viewer = Depends(require_client)):
    """Everything that can be added to a profile, with the period's buyers (counts under 10 are not sent)."""
    engine = Engine(db)
    counts = engine.buyers_by_model(pick_period(engine, viewer, quarter).scope, tuple(engine.models))
    out = []
    for key in [f"m:{i}" for i in engine.models] + [f"b:{i}" for i in engine.brands]:
        e = resolve_entity(engine, key, viewer.brand_id)
        n = sum(counts.get(i, 0) for i in e["model_ids"])
        item = {k: e[k] for k in ("key", "kind", "label", "sub", "mine")}
        item["status"] = "no_data" if n == 0 else sample_status(n)
        if n >= MIN_SHOW:
            item["n"] = n
        out.append((n, item))
    out.sort(key=lambda x: (x[1]["kind"] != "model", -x[0], x[1]["label"]))
    return [item for _, item in out]


class ProfileRequest(BaseModel):
    quarter: Optional[str] = None
    compare: str = "off"
    entities: List[str] = Field(min_length=1, max_length=MAX_PROFILE)
    filters: Dict[str, int] = Field(default={}, max_length=10)  # question code -> option id
    trend: bool = False


def group_base(engine: Engine, scope: Scope, compare: Optional[str]) -> dict:
    n, market = engine.buyers(scope), engine.buyers(scope.market())
    status = "no_data" if n == 0 else sample_status(n)
    out = {"status": status}
    if n >= MIN_SHOW:
        out.update(n=n, share_pct=round(n / market * 100, 1))
        if compare:
            before = engine.buyers(scope.in_quarter(compare))
            if before == 0:
                out["change"] = {"kind": "new"}
            elif before >= MIN_SHOW:  # never let a change reveal a hidden count
                out["change"] = {"kind": "count", "buyers": n - before, "flat": abs(n - before) <= max(3, before * 0.05)}
    return out


@router.post("/profile")
def profile(body: ProfileRequest, request: Request, db: Session = Depends(get_db),
            viewer: Viewer = Depends(require_client)):
    engine = Engine(db)
    period = pick_period(engine, viewer, body.quarter, body.compare)
    q, cmp = period.scope, period.compare

    filters = []
    for code, option_id in body.filters.items():
        allowed = {o.id for o in engine.options_for(code)} if code in dict(FILTERS) else set()
        if option_id not in allowed:
            fail(400, "Unknown filter.")
        filters.append((option_id,))
    keys = list(dict.fromkeys(body.entities))  # no duplicates, order kept
    chosen = [resolve_entity(engine, k, viewer.brand_id) for k in keys]
    groups = [(e["key"], e["label"], Scope(q, e["model_ids"], tuple(filters))) for e in chosen]
    trend_quarters = list(period.trend) if body.trend else []

    saved = db.get(UserLayout, viewer.user.id)
    chosen_cards = profile_of(saved.profile if saved else None)
    sections = []
    for theme, title, sub in THEMES:
        codes = [c for c in chosen_cards.get(theme, []) if c in engine.questions]
        sections.append({"theme": theme, "title": title, "sub": sub,
                         "cards": [profile_card(engine, groups, code, cmp, trend_quarters) for code in codes]})

    market_n = engine.buyers(groups[0][2].market())
    log(db, request, "view_profile", viewer.user, quarter=period.info["label"], entities=keys, filters=body.filters, trend=body.trend,
        **viewer.log_detail())
    db.commit()
    return {
        "quarter": period.info,
        "compare": period.compare_info,
        "trend_quarters": [next(x for x in period.quarters if x["id"] == t) for t in trend_quarters],
        "filtered": bool(filters),
        "customised": bool(saved and saved.profile),
        "entities": [{**{k: e[k] for k in ("key", "kind", "label", "sub", "mine")}, "base": group_base(engine, s, cmp)}
                     for e, (_, _, s) in zip(chosen, groups)],
        "market": {"status": sample_status(market_n), **({"n": market_n} if market_n >= MIN_SHOW else {})},
        "standouts": [{"key": key, "items": standouts(engine, s)} for key, _, s in groups[:3]],
        "sections": sections,
    }
