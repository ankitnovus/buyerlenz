"""The Summary report: the client's standard report (same sections, order and shading as their master file),
for up to 5 models side by side. Every BuyerLenz rule applies per column: under 10 buyers nothing is shown,
under 30 is marked caution, "not asked" / "no data" / "not offered" are never shown as 0%, and a true zero is "-".

Phase 2 sections (family size, loan tenure, running and mileage, additional vehicles, months considering,
years to keep) are added once their data is imported."""
import re
from typing import Optional

from app.catalogue import BY_CODE, E5_NONE_KEY, SATISFIED_LABEL
from app.engine import Counts, Engine, Scope
from app.rules import MIN_SHOW, sample_status
from app.views import counts_for, pct, status_of

AGE_BANDS = (("<=20 Years", 0, 20), ("21 to 25 Years", 21, 25), ("26 to 30 Years", 26, 30), ("31 to 35 Years", 31, 35),
             ("36 to 40 Years", 36, 40), ("41 to 45 Years", 41, 45), ("46 to 50 Years", 46, 50),
             ("51 to 60 Years", 51, 60), (">=61 Years", 61, 150))
INCOME_BANDS = (("Less than or equal to 30,000", 0, 30_000), ("INR 30,001 to 40,000", 30_001, 40_000),
                ("INR 40,001 to 50,000", 40_001, 50_000), ("INR 50,001 to 60,000", 50_001, 60_000),
                ("INR 60,001 to 80,000", 60_001, 80_000), ("INR 80,001 to 1,00,000", 80_001, 100_000),
                ("INR 1,00,001 to 1,50,000", 100_001, 150_000), ("INR 1,50,001 to 2,00,000", 150_001, 200_000),
                ("INR 2,00,001 to 2,50,000", 200_001, 250_000), ("More than INR 2,50,000", 250_001, 10**12))
TOP = 5
NONE_CONSIDERED = "Pre-determined purchase [no model considered at all]"

# (title, kind, question, options) in the master file's order
SECTIONS = (
    ("Age", "bands", "B1", {"bands": AGE_BANDS, "average": "Average (years)"}),
    ("Gender", "all", "B2", {"answers": ['Male', 'Female']}),
    ("Educational Qualifications", "all", "B3", {"answers": ['Illiterate', 'Literate but no formal certification / elementary school-Upto 4 years', 'Middle school - 5 to 9 years of schooling', 'SSC / HSC (School up to 10–12 years)', 'Some College', 'Graduate: General (B.A, B.Ed., BBA, B.Sc., B. Com., Diploma, Diploma in Medical Science, or equivalent)', 'Postgraduate: General (M.A, M.Ed., MBA, M.Sc., M. Com., PHD in General Science, etc.)', 'Graduate: Professional (B.Tech, B.Arch., BA LLB, MBBS, B.Pharma, or equivalent)', 'Postgraduate: Professional (M.Tech, M.Arch., LLM, Doctor of Medicine, Master of Surgery, PHD, or equivalent)']}),
    ("Marital Status", "all", "B4", {"answers": ['Single', 'Married', 'Divorced', 'Widow']}),
    ("Monthly Household Income", "bands", "B6", {"bands": INCOME_BANDS, "average": "Average (₹)"}),
    ("Employment Status", "all", "B7", {"answers": ['Salaried', 'Business', 'Self employed', 'Unemployed', 'Student', 'Housewife', 'Retired']}),
    ("Ownership Status of Residence", "all", "B8", {"answers": ['Owned', 'Rented', 'Government Provided Residence']}),
    ("Housing Type", "all", "B9", {"answers": ['High Rise Apartments', 'Independent Builder Floor', 'Independent House/Bungalow/Villa']}),
    ("Primary Parking Arrangement", "all", "B10", {"answers": ['Roadside', 'Dedicated Parking Inside Home, but not Covered', 'Covered Parking inside Home', 'Basement Parking', 'Metro Parking/Paid Parking/ Parking Lots']}),
    ("Own a Two-Wheeler", "all", "C1", {"answers": ['Yes']}),
    ("Top 5 Needs to Buy the New Car in Family", "top", "D1", {"noun": "Reason"}),
    ("Type of Car Buyer", "all", "D2", {"answers": ['First Time Buyer', 'Additional Car Buyer', 'Replacement Car Buyer']}),
    ("Type of Vehicle Purchase in Case of Replacement", "all", "D4", {"answers": ['First-hand car', 'Pre-owned car (second hand)']}),
    ("Top 5 Parameters Considered Before Purchase", "top", "E1_1", {"noun": "Factor"}),
    ("Pre-determined Budget", "average", "E2r1c1", {"average": "Average (₹)"}),
    ("Loan Amount: Monthly EMI", "average", "E4c2", {"average": "Average (₹)"}),
    ("Top 5 Models Considered", "considered", "E5", {}),
    ("Top 5 Reasons for Rejecting Considered Models", "rejected", "E7", {}),
    ("Transmission Type", "all", "E13", {"answers": ['Manual', 'Automatic']}),
    ("Top 5 Reasons to Buy This Car", "top", "E15", {"noun": "Reason"}),
    ("Top 5 Satisfaction Factors", "top", "E18", {"noun": "Satisfaction"}),
    ("Top 5 Dissatisfaction Factors", "top", "E19", {"noun": "Dissatisfaction", "pin": "No Dislikes"}),
    ("Top 5 Words About the Car", "top", "E20", {"noun": "Word"}),
    ("Top 5 Features Used in Car", "top", "E21", {"noun": "Feature"}),
    ("Installed/Purchased Any Accessory for Car", "all", "E22", {"answers": ['Yes, all the accessories are from Showroom', 'Yes, all the accessories are from Aftermarket', 'Few from Showroom; Few from Aftermarket', 'No additional accessories installed in my car']}),
    ("Overall, Satisfaction with Your Car", "all", "F1", {"answers": ['Extremely Dissatisfied', 'Dissatisfied', 'Average', 'Satisfied', 'Extremely Satisfied']}),
    ("Top 5 “One Single Reason” for Satisfaction with the Car", "top", "F2", {"noun": "Reason"}),
    ("Top 5 “One Single Reason” for Not Giving a 5-Star Rating", "top", "F3", {"noun": "Reason"}),
    ("Overall, Satisfaction with Dealership", "all", "F4", {"answers": ['Extremely Dissatisfied', 'Dissatisfied', 'Average', 'Satisfied', 'Extremely Satisfied']}),
    ("Reason for Extreme Satisfaction with the Dealership", "all", "F5", {"answers": ['Friendly and helpful staff', 'Good customer service', 'Clear explanation of features', 'Transparent pricing', 'Quick and smooth process', 'Professional Behaviour', 'Clean and well maintained showroom', 'Test drive experience', 'Finance and documentation support', 'Showroom Ambience', 'Delivery was on Promised Date', 'Others']}),
    ("Primary Factor That Prevented a 5-Star Rating", "all", "F6", {"answers": ['Unfriendly and limited assistance from the staff', 'The service provided did not meet my expectations for professional support', 'The explanation of features was insufficient or difficult to follow', 'Non - transparent pricing Lack of clarity regarding the pricing', 'The process was time consuming and lacked efficiency', 'Unprofessional Behaviour', 'Dirty and not well maintained showroom Poor showroom ambience', 'Bad test drive experience', 'No Finance and documentation support', 'Delivery delayed', 'Others']}),
    ("Owned Mobile Phone", "all", "G1", {"answers": ['Apple', 'Samsung', 'OnePlus', 'Xiaomi', 'Vivo', 'Others']}),
    ("Top Smart Devices Used", "all", "G2", {"answers": ['Watch', 'Washing Machine', 'Speaker (Amazon Alexa, Google Home, etc.)', 'Lights', 'Doorbell', 'Curtains', 'Locks', 'Plugs', 'Switches', 'AC', 'Refrigerator', 'None']}),
    ("Abroad Travelling for Holiday Purposes in Last 2 Years", "all", "G3", {"answers": ['None', '1', '2-4', 'More than 4']}),
    ("Future Fuel Type Purchase", "all", "G5", {"answers": ['Petrol', 'Diesel', 'CNG', 'Strong Hybrid', 'Electric/EV', 'Plug-in Hybrid (PHEV)']}),
)


def share(c: Counts, ids: tuple) -> dict:
    return {"pct": round(pct(c, ids), 1)}


def plain(label: str) -> str:
    """For matching the master file's wording to the survey's: lower case, letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", str(label).lower().replace("smart ", ""))


# Where the master file words an answer differently from the survey export
ALIASES = {"2-4": "2 to 4", "Delivery delayed": "Delivery Period was Delayed post Payment",
           "SSC / HSC (School up to 10–12 years)": "SSC / HSC (School up to 10 to 12 years)"}
OTHERS = re.compile(r"^(e\d\w*:\s*)?others?\b", re.I)  # "Others", "Other", "Others 1", "E7r167c1: Others (...)"


def match_answers(wanted: list, options: list) -> list:
    """[(label to show, option ids)] in the master file's order. An answer nobody chose still appears (a true
    zero, "-"); an answer in the data that the master doesn't list is added at the end, so nothing is hidden."""
    by_plain = {plain(o.label): o for o in options}
    used, out = set(), []
    for label in wanted:
        key = plain(ALIASES.get(label, label))
        o = by_plain.get(key) or next((x for k, x in by_plain.items() if k.startswith(key) and x.id not in used), None)
        if o is not None:
            used.add(o.id)
        out.append((label, (o.id,) if o else ()))
    out += [(o.label, (o.id,)) for o in options if o.id not in used]
    return out


class Report:
    def __init__(self, engine: Engine, columns: list):
        """columns: [(key, label, Scope)]"""
        self.engine, self.columns = engine, columns
        self.column_status = {}
        for key, _, scope in columns:
            n = engine.buyers(scope)
            self.column_status[key] = "no_data" if n == 0 else sample_status(n)

    def visible(self, key: str) -> bool:
        return self.column_status[key] in ("ok", "caution")

    def blank(self, key: str, status: Optional[str] = None) -> dict:
        return {"status": status or self.column_status[key]}

    def section_bases(self, counts: dict) -> dict:
        """The base (n) of a section in each column; only sent when 10 or more."""
        return {key: c.asked for key, c in counts.items() if self.visible(key) and c.asked >= MIN_SHOW}

    # ---------- section kinds ----------

    def all_answers(self, code: str, wanted: list) -> dict:
        q, engine = BY_CODE[code], self.engine
        options = [o for o in engine.options_for(code) if o.label != SATISFIED_LABEL]
        if code == "C1":  # the master shows only "Yes"
            options = [o for o in options if o.label == "Yes"]
        answer_rows = match_answers(wanted, options)
        counts = {key: counts_for(engine, s, code) for key, _, s in self.columns}
        offered = {key: engine.offered(s.model_ids, code) for key, _, s in self.columns} if q.product else {}
        rows = []
        for label, ids in answer_rows:
            cells = {}
            for key, _, _ in self.columns:
                c, st = counts[key], status_of(counts[key])
                if not self.visible(key) or st not in ("ok", "caution"):
                    cells[key] = self.blank(key, st if self.visible(key) else None)
                elif q.product and ids and not any(i in offered[key] for i in ids):
                    cells[key] = {"status": "not_offered"}
                else:
                    cells[key] = share(c, ids) if ids else {"pct": 0.0}  # nobody chose it: a true zero
            rows.append({"label": label, "cells": cells})
        return {"rows": rows, "bases": self.section_bases(counts)}

    def top(self, code: str, noun: str, pin: Optional[str] = None) -> dict:
        engine = self.engine
        counts = {key: counts_for(engine, s, code) for key, _, s in self.columns}
        labels = {o.id: o.label for o in engine.options_for(code)}
        pinned_ids = tuple(i for i, label in labels.items() if label == pin)
        rows = [{"label": pin, "pinned": True, "cells": {}}] if pinned_ids else []
        rows += [{"label": f"{noun} {i} - %", "cells": {}} for i in range(1, TOP + 1)]
        for key, _, _ in self.columns:
            c, st = counts[key], status_of(counts[key])
            if not self.visible(key) or st not in ("ok", "caution"):
                for r in rows:
                    r["cells"][key] = self.blank(key, st if self.visible(key) else None)
                continue
            if pinned_ids:
                rows[0]["cells"][key] = {"text": pin, **share(c, pinned_ids)}
            ranked = self.ranked(code, c, exclude=pinned_ids)
            ranked_rows = rows[1:] if pinned_ids else rows
            for r, item in zip(ranked_rows, ranked + [None] * TOP):
                r["cells"][key] = {"text": labels.get(item[1], ""), **share(c, (item[1],))} if item else {"status": "none"}
        return {"rows": rows, "bases": self.section_bases(counts)}

    def ranked(self, code: str, c: Counts, exclude: tuple = ()) -> list:
        """[(buyers, option id)], most chosen first; ties in questionnaire order; "Others" left out, as in the master."""
        order = {o.id: i for i, o in enumerate(self.engine.options_for(code))}
        labels = {o.id: o.label for o in self.engine.options_for(code)}
        items = [(n, oid) for oid, n in c.chosen.items()
                 if n > 0 and oid not in exclude and not OTHERS.match(labels.get(oid, ""))]
        return sorted(items, key=lambda x: (-x[0], order.get(x[1], 10**6)))

    def bands(self, code: str, bands: tuple, average: str) -> dict:
        summaries = {key: self.engine.number_summary(s, code, bands) for key, _, s in self.columns}
        rows = [{"label": average, "average": True, "cells": {}}] + [{"label": b[0], "cells": {}} for b in bands]
        for key, _, _ in self.columns:
            n, mean, counts = summaries[key]
            st = "not_asked" if n == 0 else sample_status(n)
            if not self.visible(key) or st not in ("ok", "caution"):
                for r in rows:
                    r["cells"][key] = self.blank(key, st if self.visible(key) else None)
                continue
            rows[0]["cells"][key] = {"value": round(mean, 1)}
            for r, (label, _, _) in zip(rows[1:], bands):
                r["cells"][key] = {"pct": round(counts[label] / n * 100, 1)}
        return {"rows": rows, "bases": {k: v[0] for k, v in summaries.items() if self.visible(k) and v[0] >= MIN_SHOW}}

    def average(self, code: str, average: str) -> dict:
        out = self.bands(code, (), average)
        return out

    def considered(self) -> dict:
        engine = self.engine
        counts = {key: engine.considered(s) for key, _, s in self.columns}
        rows = [{"label": NONE_CONSIDERED, "pinned": True, "cells": {}}] + \
               [{"label": f"Model {i} - %", "cells": {}} for i in range(1, TOP + 1)]
        for key, _, _ in self.columns:
            c, st = counts[key], status_of(counts[key])
            if not self.visible(key) or st not in ("ok", "caution"):
                for r in rows:
                    r["cells"][key] = self.blank(key, st if self.visible(key) else None)
                continue
            rows[0]["cells"][key] = share(c, (E5_NONE_KEY,))
            for r, (mid, n) in zip(rows[1:], self.most_considered(c) + [(None, 0)] * TOP):
                r["cells"][key] = {"text": engine.models[mid].name, **share(c, (mid,))} if mid else {"status": "none"}
        return {"rows": rows, "bases": self.section_bases(counts)}

    def most_considered(self, c: Counts) -> list:
        ranked = sorted(((n, mid) for mid, n in c.chosen.items()
                         if mid != E5_NONE_KEY and n > 0 and mid in self.engine.models), reverse=True)
        return [(mid, n) for n, mid in ranked[:TOP]]

    def rejected(self) -> list:
        """One part per rank: each column's #1 (#2, ...) most-considered model, named, with its top 5 reasons."""
        engine = self.engine
        considered = {key: engine.considered(s) for key, _, s in self.columns}
        labels = {o.id: o.label for o in engine.options_for("E7")}
        parts = []
        for rank in range(TOP):
            subject, rows, bases = {}, [{"label": f"Reason {i} - %", "cells": {}} for i in range(1, TOP + 1)], {}
            for key, _, scope in self.columns:
                tops = self.most_considered(considered[key]) if self.visible(key) else []
                if not self.visible(key) or status_of(considered[key]) not in ("ok", "caution") or rank >= len(tops):
                    subject[key] = self.blank(key, None if not self.visible(key) else "none")
                    for r in rows:
                        r["cells"][key] = subject[key]
                    continue
                mid = tops[rank][0]
                c = engine.rejected_model(scope, mid)
                st = status_of(c)
                subject[key] = {"text": engine.models[mid].name}
                if st not in ("ok", "caution"):
                    for r in rows:
                        r["cells"][key] = {"status": st}
                    continue
                bases[key] = c.asked
                ranked = self.ranked("E7", c)
                for r, item in zip(rows, ranked + [None] * TOP):
                    r["cells"][key] = {"text": labels.get(item[1], ""), **share(c, (item[1],))} if item else {"status": "none"}
            parts.append({"title": f"Top 5 Reasons for Rejecting Considered Model #{rank + 1}",
                          "subject": subject, "rows": rows, "bases": bases})
        return parts

    # ---------- the whole report ----------

    def build(self) -> list:
        out = []
        for title, kind, code, opts in SECTIONS:
            if code not in self.engine.questions:
                continue
            if kind == "rejected":
                out += [{"kind": "rejected", "code": code, **part} for part in self.rejected()]
                continue
            body = {"all": lambda: self.all_answers(code, opts.get("answers", [])),
                    "top": lambda: self.top(code, opts["noun"], opts.get("pin")),
                    "bands": lambda: self.bands(code, opts["bands"], opts["average"]),
                    "average": lambda: self.average(code, opts["average"]),
                    "considered": self.considered}[kind]()
            out.append({"title": title, "kind": kind, "code": code, **body})
        return out
