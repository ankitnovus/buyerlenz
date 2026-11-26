"""A person's own Home and Buyer Profile layout: read it (with the menu of what can be chosen), save it,
reset it to the standard layout, and preview one Home tile with real numbers."""
from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.activity import log
from app.catalogue import BY_CODE, QUESTIONS
from app.client_api import Viewer, brand_scope, fail, pick_period, require_client, tile
from app.db import get_db
from app.engine import Engine
from app.layout import (CHART_KINDS, DEFAULT_HOME, MAX_CHARTS, MAX_TILES, LayoutProblem, check_home,
                        check_profile, default_profile, home_of, profile_of, profile_questions, tile_kind_allowed)
from app.models import UserLayout
from app.views import THEMES

router = APIRouter(prefix="/api/client", tags=["layout"])


def answers_for(engine: Engine):
    return lambda code: [o.label for o in engine.options_for(code)] if code in engine.questions else []


def menu(engine: Engine) -> dict:
    themes = {t: title for t, title, _ in THEMES}
    tiles, charts = [], []
    for q in QUESTIONS:
        if q.code not in engine.questions:
            continue
        kinds = [k for k in ("share", "satisfaction", "amount") if tile_kind_allowed(k, q.code)]
        if kinds:
            entry = {"question": q.code, "title": q.title, "theme": themes.get(q.theme, ""), "kind": kinds[0]}
            if kinds[0] == "share":
                entry["answers"] = answers_for(engine)(q.code)
                if not entry["answers"]:
                    continue  # no answers uploaded yet
            tiles.append(entry)
        if q.kind in CHART_KINDS:
            charts.append({"code": q.code, "title": q.title, "theme": themes.get(q.theme, "")})
    return {
        "tiles": tiles,
        "charts": charts,
        "profile": {t: [{"code": c, "title": BY_CODE[c].title} for c in profile_questions(t) if c in engine.questions]
                    for t, _, _ in THEMES},
        "themes": [{"theme": t, "title": title} for t, title, _ in THEMES],
    }


@router.get("/layout")
def get_layout(db: Session = Depends(get_db), viewer: Viewer = Depends(require_client)):
    saved = db.get(UserLayout, viewer.user.id)
    return {
        "home": home_of(saved.home if saved else None),
        "profile": profile_of(saved.profile if saved else None),
        "customised": {"home": bool(saved and saved.home), "profile": bool(saved and saved.profile)},
        "defaults": {"home": DEFAULT_HOME, "profile": default_profile()},
        "menu": menu(Engine(db)),
        "limits": {"tiles": MAX_TILES, "charts": MAX_CHARTS},
    }


class SaveLayout(BaseModel):
    layout: Optional[dict] = None  # None: back to the standard layout


def save(db: Session, request: Request, viewer: Viewer, page: str, layout: Optional[dict]) -> dict:
    saved = db.get(UserLayout, viewer.user.id) or UserLayout(user_id=viewer.user.id)
    db.add(saved)
    setattr(saved, page, layout)
    log(db, request, "layout_reset" if layout is None else "layout_changed", viewer.user, page=page,
        **viewer.log_detail())
    db.commit()
    return get_layout(db, viewer)


@router.put("/layout/home")
def save_home(body: SaveLayout, request: Request, db: Session = Depends(get_db),
              viewer: Viewer = Depends(require_client)):
    try:
        layout = None if body.layout is None else check_home(body.layout, answers_for(Engine(db)))
    except LayoutProblem as e:
        fail(400, str(e))
    return save(db, request, viewer, "home", layout)


@router.put("/layout/profile")
def save_profile(body: SaveLayout, request: Request, db: Session = Depends(get_db),
                 viewer: Viewer = Depends(require_client)):
    try:
        layout = None if body.layout is None else check_profile(body.layout)
    except LayoutProblem as e:
        fail(400, str(e))
    return save(db, request, viewer, "profile", layout)


class TilePreview(BaseModel):
    tile: dict
    quarter: Optional[str] = None
    compare: str = "off"


@router.post("/tile")
def preview_tile(body: TilePreview, request: Request, db: Session = Depends(get_db),
                 viewer: Viewer = Depends(require_client)):
    """One tile with this quarter's numbers, so a person sees it before adding it to Home."""
    engine = Engine(db)
    try:
        spec = check_home({"tiles": [body.tile], "charts": []}, answers_for(engine))["tiles"][0]
    except LayoutProblem as e:
        fail(400, str(e))
    period = pick_period(engine, viewer, body.quarter, body.compare)
    _, scope = brand_scope(db, viewer, period.scope)
    log(db, request, "view_tile_preview", viewer.user, quarter=period.info["label"], tile=spec, **viewer.log_detail())
    db.commit()
    return tile(engine, scope, spec, period.compare)
