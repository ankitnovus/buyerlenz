"""Customisable Home and Buyer Profile: the standard layout, what a person may choose, and the checks.
A layout only says WHICH questions appear and in what order; app/views.py still decides HOW (every rule applies)."""
from typing import Optional

from app.catalogue import BY_CODE, E5_NONE_LABEL, QUESTIONS
from app.views import PROFILE_KINDS, THEMES

MAX_TILES = 7  # plus the Buyers tile, which is always first: 8 on Home
MAX_CHARTS = 9  # three rows of three

SHARE_KINDS = {"single", "multi", "slots", "banded", "consider"}  # "% of buyers who gave answer X" (E5: no alternative)
CHART_KINDS = {"single", "multi", "slots", "banded", "reject"}  # bar charts on Home

DEFAULT_HOME = {
    "tiles": [
        {"kind": "satisfaction", "question": "F1"},
        {"kind": "satisfaction", "question": "F4"},
        {"kind": "share", "question": "E3", "answer": "Yes"},
        {"kind": "share", "question": "E5", "answer": E5_NONE_LABEL},
    ],
    "charts": ["E10", "D2", "B6", "E15", "E7"],
    "movers": True,
    "top_models": True,
}


class LayoutProblem(Exception):
    """A layout that can't be saved; the message says why in plain words."""


def profile_questions(theme: str) -> list:
    return [q.code for q in QUESTIONS if q.theme == theme and q.kind in PROFILE_KINDS]


def default_profile() -> dict:
    return {theme: profile_questions(theme) for theme, _, _ in THEMES}


def tile_kind_allowed(kind: str, code: str) -> bool:
    q = BY_CODE.get(code)
    if q is None:
        return False
    if kind == "satisfaction":
        return q.rating
    if kind == "amount":
        return q.kind == "number"
    if kind == "share":
        return q.kind in SHARE_KINDS and not q.rating
    return False


def check_home(layout: dict, answers_for) -> dict:
    """A clean copy of a Home layout, or LayoutProblem. answers_for(code) gives a question's answer labels."""
    tiles = layout.get("tiles")
    charts = layout.get("charts")
    if not isinstance(tiles, list) or not isinstance(charts, list):
        raise LayoutProblem("The layout is incomplete.")
    if len(tiles) > MAX_TILES:
        raise LayoutProblem(f"Home has room for {MAX_TILES + 1} tiles, including Buyers.")
    if len(charts) > MAX_CHARTS:
        raise LayoutProblem(f"Home has room for {MAX_CHARTS} charts.")
    clean_tiles, seen = [], set()
    for t in tiles:
        kind, code = (t or {}).get("kind"), (t or {}).get("question")
        if not tile_kind_allowed(kind, code):
            raise LayoutProblem("That tile can’t be shown.")
        tile = {"kind": kind, "question": code}
        if kind == "share":
            answer = t.get("answer")
            if answer not in answers_for(code):
                raise LayoutProblem(f"“{answer}” isn’t an answer to {BY_CODE[code].title}.")
            tile["answer"] = answer
        key = (kind, code, tile.get("answer"))
        if key in seen:
            raise LayoutProblem("That tile is already on Home.")
        seen.add(key)
        clean_tiles.append(tile)
    for code in charts:
        if code not in BY_CODE or BY_CODE[code].kind not in CHART_KINDS:
            raise LayoutProblem("That chart can’t be shown.")
    if len(set(charts)) != len(charts):
        raise LayoutProblem("That chart is already on Home.")
    return {"tiles": clean_tiles, "charts": list(charts),
            "movers": bool(layout.get("movers", True)), "top_models": bool(layout.get("top_models", True))}


def check_profile(layout: dict) -> dict:
    clean = {}
    for theme, _, _ in THEMES:
        codes = layout.get(theme, [])
        allowed = set(profile_questions(theme))
        if not isinstance(codes, list) or any(c not in allowed for c in codes) or len(set(codes)) != len(codes):
            raise LayoutProblem("One of the sections lists a question that doesn’t belong there.")
        clean[theme] = list(codes)
    if not any(clean.values()):
        raise LayoutProblem("Keep at least one card on Buyer Profile.")
    return clean


def home_of(saved: Optional[dict]) -> dict:
    return saved or DEFAULT_HOME


def profile_of(saved: Optional[dict]) -> dict:
    return saved or default_profile()
