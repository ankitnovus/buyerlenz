"""What clients see: turns engine counts into display-ready numbers, with every BuyerLenz rule applied.

- Fewer than MIN_SHOW buyers asked: status "hidden". No percentages and no count leave the server.
- Fewer than MIN_SOLID: status "caution", and the market comparison is switched off.
- No buyers at all in the group: "no_data". Buyers but nobody asked: "not_asked". Never shown as 0%.
- Options a model doesn't offer (product questions) are left out, never shown as 0%.
- Changes need 30+ buyers in both quarters; changes of FLAT points or less are marked flat (grey).
"""
from typing import Optional

from app.catalogue import BY_CODE, E5_NONE_KEY, E5_NONE_LABEL, SATISFACTION, SATISFIED_LABEL
from app.engine import Counts, Engine, Scope
from app.rules import FLAT, sample_status

MOVER_POINTS = 4  # "What changed" lists movements of at least this many points
MOVER_QUESTIONS = ("B1", "B6", "D2", "E10", "E13", "E3", "E15", "E20", "F1", "F4")


def status_of(c: Counts) -> str:
    if c.buyers == 0:
        return "no_data"
    if c.asked == 0:
        return "not_asked"
    return sample_status(c.asked)


def pct(c: Counts, option_ids: tuple) -> float:
    return sum(c.chosen.get(i, 0) for i in option_ids) / c.asked * 100


def answer_rows(engine: Engine, code: str) -> list:
    """[(label, option ids)] in the question's order. Satisfaction gets a 'Satisfied (4–5)' headline first."""
    opts = engine.options_for(code)
    rows = [(o.label, (o.id,)) for o in opts]
    if BY_CODE[code].rating:
        top = tuple(o.id for o in opts if o.key in SATISFACTION[3:])
        rows.insert(0, (SATISFIED_LABEL, top))
    return rows


def change(now: Counts, before: Counts, ids: tuple) -> dict:
    if before.buyers == 0:
        return {"kind": "new"}  # nothing in the comparison quarter
    if status_of(now) != "ok" or status_of(before) != "ok":
        return {"kind": "few"}  # under 30 buyers in one of the two quarters: change not shown
    points = round(pct(now, ids) - pct(before, ids))
    return {"kind": "change", "points": points, "flat": abs(points) <= FLAT}


def counts_for(engine: Engine, scope: Scope, code: str) -> Counts:
    return engine.rejections(scope) if code == "E7" else engine.counts(scope, code)


def bars(engine: Engine, scope: Scope, code: str, compare: Optional[str] = None, top: Optional[int] = None) -> dict:
    """One question for one group: the answers as percentages, with base, market and change where allowed."""
    q, qrow = BY_CODE[code], engine.questions[code]
    c = counts_for(engine, scope, code)
    status = status_of(c)
    out = {"code": code, "title": qrow.title, "wording": qrow.wording, "status": status}
    if status in ("hidden", "no_data", "not_asked"):
        return out  # nothing else leaves the server

    m = counts_for(engine, scope.market(), code)
    show_market = scope.model_ids is not None and status == "ok" and status_of(m) == "ok"
    before = counts_for(engine, scope.in_quarter(compare), code) if compare else None

    rows = answer_rows(engine, code)
    if q.product:
        offered = engine.offered(scope.model_ids, code)
        rows = [r for r in rows if any(i in offered for i in r[1])]
    items = []
    for label, ids in rows:
        item = {"label": label, "pct": round(pct(c, ids), 1)}
        if show_market:
            item["market_pct"] = round(pct(m, ids), 1)
        if before is not None:
            item["change"] = change(c, before, ids)
        items.append(item)
    if not q.natural_order:  # largest first
        items.sort(key=lambda i: -i["pct"])
    if top and len(items) > top:
        out["more"] = len(items) - top
        items = items[:top]

    out.update(n=c.asked, options=items, market_comparison=show_market)
    if q.rating:
        score = {o.id: SATISFACTION.index(o.key) + 1 for o in engine.options_for(code) if o.key in SATISFACTION}
        out["mean"] = round(sum(score.get(i, 0) * n for i, n in c.chosen.items()) / c.asked, 2)
    return out


def amount_tile(engine: Engine, scope: Scope, code: str, compare: Optional[str] = None) -> dict:
    """Typical amount (₹) for a number question, with base, market and change where allowed.
    Changes are in percent of the amount; 3% or less is within noise (grey), like the 3-point rule."""
    qrow = engine.questions[code]
    n, median = engine.amount(scope, code)
    status = status_of(Counts(buyers=engine.buyers(scope), asked=n, chosen={}))
    out = {"code": code, "title": qrow.title, "wording": qrow.wording, "status": status}
    if status in ("hidden", "no_data", "not_asked"):
        return out
    out.update(n=n, median=round(median))
    if scope.model_ids is not None and status == "ok":
        mn, mmed = engine.amount(scope.market(), code)
        if sample_status(mn) == "ok":
            out["market_median"] = round(mmed)
    if compare:
        bn, bmed = engine.amount(scope.in_quarter(compare), code)
        if bn == 0:
            out["change"] = {"kind": "new"}
        elif status != "ok" or sample_status(bn) != "ok" or not bmed:
            out["change"] = {"kind": "few"}
        else:
            percent = round((median - bmed) / bmed * 100)
            out["change"] = {"kind": "change", "percent": percent, "flat": abs(percent) <= FLAT}
    return out

def movers(engine: Engine, scope: Scope, compare: str, limit: int = 6) -> list:
    """The biggest changes vs the comparison quarter, across a fixed set of questions."""
    found = []
    for code in MOVER_QUESTIONS:
        b = bars(engine, scope, code, compare)
        for o in b.get("options", []):
            ch = o.get("change", {})
            if ch.get("kind") == "change" and abs(ch["points"]) >= MOVER_POINTS:
                if BY_CODE[code].rating and o["label"] != SATISFIED_LABEL:
                    continue
                found.append({"question": b["title"], "code": code, "label": o["label"], "pct": o["pct"],
                              "points": ch["points"]})
    return sorted(found, key=lambda f: -abs(f["points"]))[:limit]


# ---------- Buyer Profile ----------

THEMES = (
    ("who", "Who they are", "Demographics"),
    ("think", "How they think", "Attitudes, needs and habits"),
    ("bought", "What they bought", "The car, the money and the reasons"),
    ("feel", "How they feel", "Satisfaction and image"),
)
PROFILE_KINDS = {"single", "multi", "slots", "banded", "consider", "reject"}
STANDOUT_POINTS, STANDOUT_FLOOR = 6, 12  # "What stands out": at least 6 points apart, one side at least 12%


def group_counts(engine: Engine, scope: Scope, code: str) -> Counts:
    if code == "E5":
        return engine.considered(scope)
    return counts_for(engine, scope, code)


def profile_rows(engine: Engine, code: str, counted: list) -> list:
    if code != "E5":
        return answer_rows(engine, code)
    seen = {mid for c in counted for mid in c.chosen if mid != E5_NONE_KEY}  # models considered by anyone in view
    rows = [(engine.models[mid].name, (mid,)) for mid in seen if mid in engine.models]
    if any(o.key == E5_NONE_KEY for o in engine.options_for("E5")):
        rows.insert(0, (E5_NONE_LABEL, (E5_NONE_KEY,)))  # bought without weighing up an alternative
    return rows


def trend_point(engine: Engine, scope: Scope, code: str, ids: tuple) -> dict:
    c = group_counts(engine, scope, code)
    status = status_of(c)
    if status in ("ok", "caution"):
        return {"pct": round(pct(c, ids), 1), "caution": status == "caution"}
    return {"pct": None, "status": status}  # a gap stays a gap; never drawn as zero


def profile_card(engine: Engine, groups: list, code: str, compare: Optional[str], trend_quarters: list) -> dict:
    """One question for up to 8 groups ([(key, label, Scope)]), with all new-car buyers when allowed."""
    q, qrow = BY_CODE[code], engine.questions[code]
    counted = [group_counts(engine, s, code) for _, _, s in groups]
    statuses = [status_of(c) for c in counted]
    market = group_counts(engine, groups[0][2].market(), code)
    show_market = status_of(market) == "ok" and "caution" not in statuses

    rows = profile_rows(engine, code, counted + [market])
    note = ""
    if q.product:
        offered = set().union(*(engine.offered(s.model_ids, code) for _, _, s in groups))
        left_out = [label for label, ids in rows if not any(i in offered for i in ids)]
        rows = [r for r in rows if any(i in offered for i in r[1])]
        if len(groups) == 1 and left_out and code == "E10":
            note = (f"{groups[0][1]} buyers in this survey haven’t bought {', '.join(x.lower() for x in left_out)}, "
                    "so those are left out rather than shown as 0%.")
    ref = next((c for c, st in zip(counted, statuses) if st in ("ok", "caution")), None)
    if not q.natural_order and ref:
        rows.sort(key=lambda r: (r[1] != (E5_NONE_KEY,), -pct(ref, r[1])))  # E5 "no other model" stays first

    out_groups = []
    for (key, _, scope), c, status in zip(groups, counted, statuses):
        g = {"key": key, "status": status}
        if status in ("ok", "caution"):
            before = group_counts(engine, scope.in_quarter(compare), code) if compare else None
            values = {}
            for label, ids in rows:
                v = {"pct": round(pct(c, ids), 1)}
                if before is not None:
                    v["change"] = change(c, before, ids)
                if trend_quarters:
                    v["trend"] = [trend_point(engine, scope.in_quarter(t), code, ids) for t in trend_quarters]
                values[label] = v
            g.update(n=c.asked, values=values)
            if q.rating:
                score = {o.id: SATISFACTION.index(o.key) + 1 for o in engine.options_for(code) if o.key in SATISFACTION}
                g["mean"] = round(sum(score.get(i, 0) * n for i, n in c.chosen.items()) / c.asked, 2)
        out_groups.append(g)

    out = {"code": code, "title": qrow.title, "wording": qrow.wording, "kind": q.kind, "note": note,
           "answers": [label for label, _ in rows], "groups": out_groups, "market_comparison": show_market}
    if show_market:
        out["market"] = {label: round(pct(market, ids), 1) for label, ids in rows}
    return out


def standouts(engine: Engine, scope: Scope, limit: int = 3) -> list:
    """The biggest differences between a group and all new-car buyers (both need 30+ buyers)."""
    found = []
    for theme in ("who", "think", "bought"):
        for q in (x for x in BY_CODE.values() if x.theme == theme and x.kind in {"single", "multi", "slots", "banded"}):
            if q.product or q.code not in engine.questions:
                continue
            c, m = engine.counts(scope, q.code), engine.counts(scope.market(), q.code)
            if status_of(c) != "ok" or status_of(m) != "ok":
                continue
            for label, ids in answer_rows(engine, q.code):
                a, b = pct(c, ids), pct(m, ids)
                if abs(a - b) >= STANDOUT_POINTS and max(a, b) >= STANDOUT_FLOOR:
                    found.append({"label": label, "question": engine.questions[q.code].title,
                                  "pct": round(a), "market_pct": round(b), "more": a > b, "gap": abs(a - b)})
    found.sort(key=lambda f: -f["gap"])
    return [{k: v for k, v in f.items() if k != "gap"} for f in found[:limit]]


# ---------- Data cuts ----------

def cut_block(engine: Engine, columns: list, code: str, compare: Optional[str]) -> dict:
    """One question down the side, columns ([(key, label, Scope)], first = all buyers) across the top.
    Each cell: status, base, and per answer the % (plus gap from all buyers and change, where allowed).
    Answers a column's models don't offer are listed as not offered, never shown as 0%."""
    q, qrow = BY_CODE[code], engine.questions[code]
    counted = {key: group_counts(engine, s, code) for key, _, s in columns}
    market_key = columns[0][0]
    market = counted[market_key]
    rows = profile_rows(engine, code, list(counted.values()))
    if q.product:
        anywhere = set().union(*(engine.offered(s.model_ids, code) for _, _, s in columns))
        rows = [r for r in rows if any(i in anywhere for i in r[1])]
    if not q.natural_order:
        rows.sort(key=lambda r: (r[1] != (E5_NONE_KEY,), -pct(market, r[1]) if market.asked else 0))

    cells = {}
    for key, _, scope in columns:
        c = counted[key]
        status = status_of(c)
        cell = {"status": status}
        if status in ("ok", "caution"):
            offered = engine.offered(scope.model_ids, code) if q.product else None
            before = group_counts(engine, scope.in_quarter(compare), code) if compare else None
            values, not_offered = {}, []
            for label, ids in rows:
                if offered is not None and not any(i in offered for i in ids):
                    not_offered.append(label)
                    continue
                v = {"pct": round(pct(c, ids), 1)}
                if key != market_key and status == "ok" and status_of(market) == "ok":
                    v["gap"] = round(pct(c, ids) - pct(market, ids))
                if before is not None:
                    v["change"] = change(c, before, ids)
                values[label] = v
            cell.update(n=c.asked, values=values)
            if not_offered:
                cell["not_offered"] = not_offered
        cells[key] = cell
    return {"code": code, "title": qrow.title, "wording": qrow.wording, "kind": q.kind,
            "answers": [label for label, _ in rows], "cells": cells}


def series(engine: Engine, scope: Scope, code: str, label: str, quarters: list) -> list:
    """One answer's % for one group across quarters. Missing or hidden quarters are None, never 0."""
    rows = dict(profile_rows(engine, code, [group_counts(engine, scope.in_quarter(q), code) for q in quarters]))
    ids = rows.get(label)
    if ids is None:
        return [{"pct": None, "status": "no_data"} for _ in quarters]
    return [trend_point(engine, scope.in_quarter(q), code, ids) for q in quarters]
