"""The calculation engine: counts buyers and answers for any group, from published uploads only.

This file only counts. app/views.py turns the counts into what clients see and applies the display rules
(hidden, caution, grey changes, never 0% for a gap). Keep it that way: rules live in one place.

Speed: for each quarter (and filter choice) every answer is counted once for ALL models together, and that
summary ("cube") is kept in memory. Any brand, model group or the whole market is then a sum over models.
The summaries are rebuilt automatically whenever an upload is published or undone (see data_version).
"""
import threading
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from typing import Optional, Union

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.catalogue import E5_NONE_KEY
from app.models import AnswerOption, Brand, CarModel, Question


@dataclass(frozen=True)
class Scope:
    """Which buyers: a purchase period, some models (None = all new-car buyers), and filters.
    The period is one quarter ("2026Q1") or several together (a tuple, e.g. the trailing four quarters).
    Each filter is a tuple of option ids; a buyer must have chosen one of them."""
    quarter: Union[str, tuple]
    model_ids: Optional[tuple] = None
    filters: tuple = ()

    @property
    def quarters(self) -> tuple:
        return (self.quarter,) if isinstance(self.quarter, str) else tuple(self.quarter)

    def in_quarter(self, quarter: Union[str, tuple]) -> "Scope":
        return Scope(quarter, self.model_ids, self.filters)

    def market(self) -> "Scope":
        return Scope(self.quarter, None, self.filters)


@dataclass(frozen=True)
class Counts:
    buyers: int  # buyers in the group
    asked: int  # of those, how many were asked the question (the base, n)
    chosen: dict  # option id -> buyers who chose it


@dataclass
class Cube:
    """Counts for one quarter and filter choice, per model of the car bought."""
    buyers: dict = field(default_factory=dict)  # model id -> buyers
    asked: dict = field(default_factory=dict)  # model id -> {question id: buyers asked}
    chosen: dict = field(default_factory=dict)  # model id -> {option id: buyers who chose it}
    considered: dict = field(default_factory=dict)  # model id -> {considered model id: buyers}


MAX_CUBES = 24  # quarter + filter combinations kept in memory (oldest dropped first)
_cubes: OrderedDict = OrderedDict()
_lock = threading.Lock()


def _kept(key, compute):
    with _lock:
        if key in _cubes:
            _cubes.move_to_end(key)
            return _cubes[key]
    value = compute()
    with _lock:
        _cubes[key] = value
        while len(_cubes) > MAX_CUBES:
            _cubes.popitem(last=False)
    return value


def _add(total: dict, part: dict) -> None:
    for k, n in part.items():
        total[k] = total.get(k, 0) + n


def _base_sql(scope: Scope, all_quarters: bool = False) -> tuple:
    """SQL for the group's buyers, as a WITH clause named 'base', plus its parameters."""
    where, params = [], {}
    if not all_quarters:
        where.append("r.quarter = ANY(:quarters)")
        params["quarters"] = list(scope.quarters)
    if scope.model_ids is not None:
        where.append("mn.model_id = ANY(:models)")
        params["models"] = list(scope.model_ids)
    for i, option_ids in enumerate(scope.filters):
        where.append(f"EXISTS (SELECT 1 FROM answers f{i} WHERE f{i}.respondent_id = r.id AND f{i}.option_id = ANY(:f{i}))")
        params[f"f{i}"] = list(option_ids)
    sql = ("WITH base AS (SELECT r.id, mn.model_id FROM respondents r "
           "JOIN uploads u ON u.id = r.upload_id AND u.status = 'published' "
           "JOIN model_names mn ON mn.name = r.model_name"
           + (" WHERE " + " AND ".join(where) if where else "") + ") ")
    return sql, params


class Engine:
    """One per request. Remembers results, so asking twice for the same numbers costs nothing."""

    def __init__(self, db: Session):
        self.db = db
        self._cache: dict = {}
        self.questions = {q.code: q for q in db.scalars(select(Question))}
        self.options: dict = defaultdict(list)
        for o in db.scalars(select(AnswerOption).order_by(AnswerOption.sort, AnswerOption.id)):
            self.options[o.question_id].append(o)
        self.models = {m.id: m for m in db.scalars(select(CarModel))}
        self.brands = {b.id: b for b in db.scalars(select(Brand))}
        self.question_of = {o.id: qid for qid, opts in self.options.items() for o in opts}
        # Changes whenever published data changes (publish, undo, model names), so old summaries are never used
        self.version = tuple(db.execute(text(
            "SELECT (SELECT count(*) FROM uploads WHERE status IN ('published', 'rolled_back')), "
            "(SELECT max(greatest(published_at, rolled_back_at)) FROM uploads), "
            "(SELECT count(*) FROM model_names), (SELECT count(*) FROM answer_options)")).one())

    def _remember(self, key, compute):
        if key not in self._cache:
            self._cache[key] = compute()
        return self._cache[key]

    def options_for(self, code: str) -> list:
        return self.options[self.questions[code].id]

    def cube(self, quarter: str, filters: tuple = ()) -> Cube:
        """Every count for one quarter and filter choice, per model bought. Built once, shared by all requests."""
        def compute():
            base, params = _base_sql(Scope(quarter, None, filters))
            c = Cube()
            c.buyers = dict(self.db.execute(text(base + "SELECT model_id, count(*) FROM base GROUP BY model_id"), params).all())
            for mid, qid, n in self.db.execute(text(
                base + "SELECT b.model_id, k.question_id, count(*) FROM asked k JOIN base b ON b.id = k.respondent_id "
                "GROUP BY 1, 2"), params):
                c.asked.setdefault(mid, {})[qid] = n
            for mid, oid, n in self.db.execute(text(
                base + "SELECT b.model_id, a.option_id, count(*) FROM answers a JOIN base b ON b.id = a.respondent_id "
                "GROUP BY 1, 2"), params):
                c.chosen.setdefault(mid, {})[oid] = n
            for mid, cid, n in self.db.execute(text(
                base + "SELECT b.model_id, c.model_id, count(DISTINCT c.respondent_id) FROM considerations c "
                "JOIN base b ON b.id = c.respondent_id WHERE c.model_id IS NOT NULL GROUP BY 1, 2"), params):
                c.considered.setdefault(mid, {})[cid] = n
            return c
        return _kept((self.version, quarter, filters), compute)

    def _models(self, cube: Cube, scope: Scope) -> list:
        return list(cube.buyers) if scope.model_ids is None else [m for m in scope.model_ids if m in cube.buyers]

    def _cubes_for(self, scope: Scope) -> list:
        """The summaries for each quarter in the scope's period."""
        return [self.cube(q, scope.filters) for q in scope.quarters]

    def buyers(self, scope: Scope) -> int:
        def compute():
            return sum(cube.buyers[m] for cube in self._cubes_for(scope) for m in self._models(cube, scope))
        return self._remember(("buyers", scope), compute)

    def _all_counts(self, scope: Scope) -> tuple:
        """Every question for one group: ({question id: asked}, {question id: {option id: n}})."""
        def compute():
            asked, flat = {}, {}
            for cube in self._cubes_for(scope):
                for m in self._models(cube, scope):
                    _add(asked, cube.asked.get(m, {}))
                    _add(flat, cube.chosen.get(m, {}))
            chosen: dict = defaultdict(dict)
            for oid, n in flat.items():
                chosen[self.question_of.get(oid)][oid] = n
            return asked, chosen
        return self._remember(("all", scope), compute)

    def counts(self, scope: Scope, code: str) -> Counts:
        qid = self.questions[code].id
        asked, chosen = self._all_counts(scope)
        return Counts(buyers=self.buyers(scope), asked=asked.get(qid, 0), chosen=chosen.get(qid, {}))

    def amount(self, scope: Scope, code: str) -> tuple:
        """Typical (middle) amount for a number question: (buyers who gave a number, median or None).
        The middle value, so one very large loan or price can't pull the figure up."""
        def compute():
            base, params = _base_sql(scope)
            params["qid"] = self.questions[code].id
            n, median = self.db.execute(text(
                base + "SELECT count(*), percentile_cont(0.5) WITHIN GROUP (ORDER BY v.value) FROM number_answers v "
                "JOIN base b ON b.id = v.respondent_id WHERE v.question_id = :qid"), params).one()
            return n, median
        return self._remember(("amount", scope, code), compute)

    def considered(self, scope: Scope) -> Counts:
        """Models also considered (E5): base = buyers asked E5; chosen = {model id: buyers who considered it},
        plus "none": buyers who considered no other model."""
        def compute():
            qid, asked, chosen = self.questions["E5"].id, 0, {}
            none_id = next((o.id for o in self.options_for("E5") if o.key == E5_NONE_KEY), None)
            for cube in self._cubes_for(scope):
                for m in self._models(cube, scope):
                    asked += cube.asked.get(m, {}).get(qid, 0)
                    _add(chosen, cube.considered.get(m, {}))
                    if none_id is not None:
                        _add(chosen, {E5_NONE_KEY: cube.chosen.get(m, {}).get(none_id, 0)})
            return Counts(buyers=self.buyers(scope), asked=asked, chosen=chosen)
        return self._remember(("considered", scope), compute)

    def offered(self, model_ids: Optional[tuple], code: str) -> set:
        """Options any buyer of these models has ever chosen (all published quarters).
        Used for product questions, so a fuel or feature the model doesn't have is left out."""
        def every_quarter():
            base, params = _base_sql(Scope(""), all_quarters=True)
            out: dict = defaultdict(set)
            for mid, oid in self.db.execute(text(
                base + "SELECT DISTINCT b.model_id, a.option_id FROM answers a JOIN base b ON b.id = a.respondent_id"), params):
                out[mid].add(oid)
            return out
        ever = _kept((self.version, "offered"), every_quarter)
        qid = self.questions[code].id
        models = ever.keys() if model_ids is None else model_ids
        return {o for m in models for o in ever.get(m, ()) if self.question_of.get(o) == qid}

    def number_summary(self, scope: Scope, code: str, bands: tuple = ()) -> tuple:
        """For a number question: (buyers who gave a number, their average, {band label: buyers}).
        bands: ((label, lowest, highest), ...), inclusive at both ends."""
        def compute():
            base, params = _base_sql(scope)
            params["qid"] = self.questions[code].id
            cases = []
            for i, (_, lo, hi) in enumerate(bands):
                params[f"lo{i}"], params[f"hi{i}"] = lo, hi
                cases.append(f", count(*) FILTER (WHERE v.value >= :lo{i} AND v.value <= :hi{i})")
            row = self.db.execute(text(
                base + "SELECT count(*), avg(v.value)" + "".join(cases) + " FROM number_answers v "
                "JOIN base b ON b.id = v.respondent_id WHERE v.question_id = :qid"), params).one()
            return row[0], (float(row[1]) if row[1] is not None else None), \
                {label: row[2 + i] for i, (label, _, _) in enumerate(bands)}
        return self._remember(("numbers", scope, code, bands), compute)

    def rejected_model(self, scope: Scope, model_id: int) -> Counts:
        """Why buyers in the scope rejected one model they considered (E7, linked to E5 by rank).
        Base: buyers in the scope who considered that model and gave at least one reason for rejecting it
        (the summary report's base; it came closest to the client's master file)."""
        def compute():
            base, params = _base_sql(scope)
            params["qid"], params["model"] = self.questions["E7"].id, model_id
            cand = (", cand AS (SELECT DISTINCT c.respondent_id, c.rank FROM considerations c "
                    "JOIN base b ON b.id = c.respondent_id "
                    "JOIN asked k ON k.respondent_id = c.respondent_id AND k.question_id = :qid "
                    "WHERE c.model_id = :model AND EXISTS (SELECT 1 FROM rejections r "
                    "WHERE r.respondent_id = c.respondent_id AND r.rank = c.rank)) ")
            sql = base.rstrip() + cand
            asked = self.db.execute(text(sql + "SELECT count(DISTINCT respondent_id) FROM cand"), params).scalar_one()
            rows = self.db.execute(text(
                sql + "SELECT j.option_id, count(DISTINCT j.respondent_id) FROM rejections j "
                "JOIN cand ON cand.respondent_id = j.respondent_id AND cand.rank = j.rank GROUP BY j.option_id"
            ), params).all()
            return Counts(buyers=asked, asked=asked, chosen=dict(rows))
        return self._remember(("rejected_model", scope, model_id), compute)

    def rejections(self, scope: Scope) -> Counts:
        """Why buyers rejected these models (E7). Base: buyers who considered one of them and bought a
        different one. For all new-car buyers: everyone who rejected any considered model."""
        def compute():
            others = Scope(scope.quarter, None, scope.filters)
            base, params = _base_sql(others)
            params["qid"] = self.questions["E7"].id
            if scope.model_ids is None:
                cand = ("cand AS (SELECT DISTINCT c.respondent_id, c.rank FROM considerations c "
                        "JOIN base b ON b.id = c.respondent_id "
                        "JOIN asked k ON k.respondent_id = c.respondent_id AND k.question_id = :qid) ")
            else:
                params["models"] = list(scope.model_ids)
                cand = ("cand AS (SELECT DISTINCT c.respondent_id, c.rank FROM considerations c "
                        "JOIN base b ON b.id = c.respondent_id "
                        "JOIN asked k ON k.respondent_id = c.respondent_id AND k.question_id = :qid "
                        "WHERE c.model_id = ANY(:models) AND NOT (b.model_id = ANY(:models))) ")
            sql = base.rstrip() + ", " + cand
            asked = self.db.execute(text(sql + "SELECT count(DISTINCT respondent_id) FROM cand"), params).scalar_one()
            rows = self.db.execute(text(
                sql + "SELECT j.option_id, count(DISTINCT j.respondent_id) FROM rejections j "
                "JOIN cand ON cand.respondent_id = j.respondent_id AND cand.rank = j.rank GROUP BY j.option_id"
            ), params).all()
            return Counts(buyers=asked, asked=asked, chosen=dict(rows))
        return self._remember(("rejections", scope), compute)

    def buyers_by_model(self, quarter: Union[str, tuple], model_ids: Optional[tuple]) -> dict:
        """model id -> buyers in the period, for the given models (None: every model)."""
        out: dict = {}
        for cube in self._cubes_for(Scope(quarter)):
            wanted = cube.buyers if model_ids is None else model_ids
            _add(out, {m: cube.buyers[m] for m in wanted if m in cube.buyers})
        return out

    def published_quarters(self) -> list:
        """[(quarter, buyers, first month, last month)] for every quarter with published buyers, oldest first."""
        return self.db.execute(text(
            "SELECT r.quarter, count(*), min(r.purchase_month), max(r.purchase_month) FROM respondents r "
            "JOIN uploads u ON u.id = r.upload_id AND u.status = 'published' "
            "WHERE r.quarter IS NOT NULL GROUP BY r.quarter ORDER BY r.quarter"
        )).all()
