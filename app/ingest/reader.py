"""Read one survey Excel file into tidy answers. No database here, so it is easy to test.

File layout: row 1 = question wording, row 2 = column code (e.g. E15r112), then one row per buyer.
Blank cell = the question was not asked to that buyer. Only questions in app/catalogue.py are read.
"""
import io
import re
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import IO, Iterator, Optional, Union

import openpyxl

from app.catalogue import (BY_CODE, INCOME_RANGE_LABELS, MODEL_COLUMN, MONTH_COLUMN, NOT_SELECTED, QUESTIONS,
                           RECORD_COLUMNS, E5_NONE_COLUMN, E5_NONE_KEY, E5_NONE_LABEL, Q)
from app.rules import EXPECTED_START, SURVEY_START


def month_name(year_month: tuple) -> str:
    return datetime(year_month[0], year_month[1], 1).strftime("%B %Y")

PREFER_NOT_TO_SAY = "Prefer not to say"
FREE_TEXT_CODES = re.compile(r"(oe$|^A7_1|^C2|^D5|^B7_1$|^G1_1|^E23|^E24|^E4_1)")
PERSONAL_WORDS = re.compile(r"\b(name|mobile|phone number|e-?mail|address|location|interviewer)\b", re.I)


class FileProblem(Exception):
    """The file can't be read at all (wrong layout, missing key columns)."""


# An .xlsx file is a zip archive. A real survey export unpacks to about 10 times its size (Creta: 5 MB -> 46 MB;
# a 49,000-buyer file about 3.3 GB); a booby-trapped one is built to unpack to far more, or to expand hundreds
# of times. Checked before opening. Files are read row by row, so a large real file is fine.
MAX_UNZIPPED_MB = 6000
MAX_PARTS = 500
MAX_RATIO = 200  # how much any single part may expand


def check_archive(source: Union[str, bytes]) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(source) if isinstance(source, bytes) else source) as z:
            parts = z.infolist()
    except zipfile.BadZipFile:
        raise FileProblem("This doesn’t look like an Excel (.xlsx) file.")
    if len(parts) > MAX_PARTS:
        raise FileProblem("This file has an unusual structure and wasn’t opened, to keep the server safe.")
    if sum(p.file_size for p in parts) > MAX_UNZIPPED_MB * 1024 * 1024:
        raise FileProblem("This file unpacks to far more data than a survey export and wasn’t opened, "
                          "to keep the server safe.")
    if any(p.file_size > 1024 * 1024 and p.file_size > MAX_RATIO * max(p.compress_size, 1) for p in parts):
        raise FileProblem("This file is compressed in an unusual way and wasn’t opened, to keep the server safe.")


@dataclass
class Buyer:
    record: str
    model_name: str
    purchase_month: Optional[datetime]
    quarter: Optional[str]
    asked: set = field(default_factory=set)  # question codes this buyer was asked
    answers: dict = field(default_factory=lambda: defaultdict(set))  # question code -> option keys chosen
    numbers: dict = field(default_factory=dict)  # question code -> exact number
    considered: list = field(default_factory=list)  # (rank, e5 code, test drive, dealer visit)
    rejections: list = field(default_factory=list)  # (rank, option key)


@dataclass
class ParsedFile:
    buyers: list
    options: dict  # question code -> {option key: label}, in file order
    imported_columns: int
    dropped: list  # (column code, reason)
    warnings: Counter  # (warning, question code) -> number of buyers affected
    wordings: dict = field(default_factory=dict)  # question code -> survey wording from row 1


def question_wording(header: str, q: Q) -> str:
    """'E15. Safety - What were the top five reasons for choosing this (Pipe in the model from E8) car...'
    -> 'What were the top five reasons for choosing this car...'"""
    text = re.sub(r"^[A-Za-z]+\d+(?:_\d+)?\.\s*\.?\s*", "", str(header).strip())
    if q.kind != "single" and " - " in text:
        text = text.split(" - ")[-1]
    # The survey tool's model placeholder becomes "car" (unless "car" already follows)
    text = re.sub(r"\s*\((?:pipe|pipein)[^)]*\)(\s+car\b)?", lambda m: m.group(1) or " car", text, flags=re.I)
    text = re.sub(r"_{2,}", "", text)
    return re.sub(r"\s+", " ", text).strip()


def column_label(header: str, code: str) -> str:
    """'E15. Safety - What were the top five reasons...' -> 'Safety'."""
    text = re.sub(rf"^{re.escape(code.split('r')[0])}\.\s*", "", str(header).strip())
    text = re.sub(r"^[A-Z]+\d+(?:_\d+)?\.\s*", "", text)
    return text.split(" - ")[0].strip()


def parse_month(value: str) -> Optional[datetime]:
    try:
        return datetime.strptime(value.strip(), "%B %Y")
    except (ValueError, AttributeError):
        return None


def parse_number(value: str) -> Optional[float]:
    cleaned = re.sub(r"[₹,\s]|INR|Rs\.?", "", value, flags=re.I)
    try:
        n = float(cleaned)
    except ValueError:
        return None
    return n if n >= 0 else None


def band_for(n: float, bands: tuple) -> Optional[str]:
    return next((label for label, lo, hi in bands if lo <= n <= hi), None)


def e5_sort_key(code: str) -> tuple:
    """'17' < '17_1' < '18': the E5 list order. Rank 1 is the considered model with the lowest code.
    Assumption to confirm with the Novus data team (E7 says 'show the model name code rank wise in E5')."""
    return tuple(int(p) for p in code.split("_"))


class FileReader:
    """Reads a survey file one buyer at a time, so memory stays small however big the file is
    (a 49,000-buyer file needs about the same memory as a 700-buyer one).

    Opening checks the two header rows straight away; buyers() then walks the rows; finish() gives the
    answer lists, wordings and warnings once every row has been read."""

    def __init__(self, source: Union[str, IO[bytes]]):
        try:
            self.workbook = openpyxl.load_workbook(source, read_only=True, data_only=True)
            self.rows = self.workbook.worksheets[0].iter_rows(values_only=True)
            labels, codes = next(self.rows), next(self.rows)
        except Exception as e:
            raise FileProblem(f"This file couldn’t be read as an Excel workbook ({type(e).__name__}).") from e

        # Row 2 holds the codes; a column without a code is ignored
        self.columns = [(i, str(c).strip()) for i, c in enumerate(codes) if c is not None and str(c).strip()]
        self.headers = {code: str(labels[i] if i < len(labels) and labels[i] is not None else "")
                        for i, code in self.columns}
        codes = [code for _, code in self.columns]
        self.record_column = next((c for c in RECORD_COLUMNS if c in self.headers), RECORD_COLUMNS[0])
        for needed, what in ((self.record_column, "interview number (“record” or “respid”)"), (MODEL_COLUMN, "model purchased (E8)"),
                             (MONTH_COLUMN, "month of purchase (E9_1)")):
            if needed not in self.headers:
                raise FileProblem(f"The column for {what} (code “{needed}” in row 2) is missing. "
                                  "Check the file has question wording in row 1 and codes in row 2.")

        # Which columns belong to which question
        owner: dict = {}
        for q in QUESTIONS:
            pattern = q.column_pattern()
            for c in codes:
                if c not in owner and not c.endswith("oe") and pattern.match(c):
                    owner[c] = q
        special = {self.record_column, MODEL_COLUMN, MONTH_COLUMN}
        self.dropped = []
        for c in codes:
            if c in owner or c in special:
                continue
            label = self.headers[c]
            reason = ("free text" if FREE_TEXT_CODES.search(c) or "specify" in label.lower()
                      else "possibly personal" if PERSONAL_WORDS.search(label)
                      else "not used")
            self.dropped.append((c, reason))
        self.by_question: dict = defaultdict(list)
        for c, q in owner.items():
            self.by_question[q.code].append(c)
        self.imported_columns = len(owner) + len(special)
        keep = set(owner) | special
        self.keep = [(i, code) for i, code in self.columns if code in keep]  # only these cells are ever looked at

        self.options: dict = defaultdict(dict)
        self.warnings: Counter = Counter()
        self.seen: set = set()
        self.read = 0  # rows read so far, for the progress line
        # Several-answer questions: the option list comes from the columns, in file order. Their labels are
        # the text most often written in each column, so they're settled once every row is read.
        self.multi_counts: dict = {}
        for q in QUESTIONS:
            for c in self.by_question.get(q.code, []):
                if q.kind == "multi":
                    self.multi_counts[c] = Counter()  # its option is created when someone first chooses it
                elif q.kind == "reject":
                    self.options[q.code].setdefault(re.sub(r"c\d$", "", c), column_label(self.headers[c], c))

    def buyers(self) -> Iterator[Buyer]:
        try:
            for raw in self.rows:
                self.read += 1
                row = {}
                for i, code in self.keep:
                    v = raw[i] if i < len(raw) else None
                    if v is not None:
                        v = str(v).strip()
                        if v:
                            row[code] = v
                b = self.buyer(row)
                if b is not None:
                    yield b
        finally:
            self.workbook.close()

    def buyer(self, row: dict) -> Optional[Buyer]:
        options, warnings = self.options, self.warnings
        record, model = row.get(self.record_column), row.get(MODEL_COLUMN)
        month = parse_month(row.get(MONTH_COLUMN) or "")
        if not record or not model:
            if row:  # a completely empty row (often at the end of a sheet) isn't a buyer
                warnings[("missing interview number or model", "")] += 1
            return None
        if record in self.seen:
            warnings[("interview number repeated in this file (later copy skipped)", "")] += 1
            return None
        if month is None:
            warnings[("no readable month of purchase (buyer skipped)", MONTH_COLUMN)] += 1
            return None
        if (month.year, month.month) < SURVEY_START:
            warnings[(f"bought before {month_name(SURVEY_START)}, the earliest date accepted (left out)", MONTH_COLUMN)] += 1
            return None
        if (month.year, month.month) < EXPECTED_START:
            warnings[(f"bought before {month_name(EXPECTED_START)}, earlier than expected (kept; check the file)",
                      MONTH_COLUMN)] += 1
        self.seen.add(record)
        b = Buyer(record=record, model_name=model, purchase_month=month,
                  quarter=f"{month.year}Q{(month.month - 1) // 3 + 1}")

        for q in QUESTIONS:
            cols = self.by_question.get(q.code, [])
            if not cols:
                continue
            values = {c: row[c] for c in cols if c in row}
            if q.kind == "single":
                v = values.get(q.code)
                if v is not None:
                    b.asked.add(q.code)
                    b.answers[q.code].add(v)
                    options[q.code].setdefault(v, v)
            elif q.kind == "slots":
                for v in values.values():
                    b.asked.add(q.code)
                    b.answers[q.code].add(v)
                    options[q.code].setdefault(v, v)
            elif q.kind == "multi":
                if values:
                    b.asked.add(q.code)
                    for c, v in values.items():
                        if v.lower() not in NOT_SELECTED:
                            b.answers[q.code].add(c)
                            self.multi_counts[c][v] += 1
                            options[q.code].setdefault(c, column_label(self.headers[c], c))
            elif q.kind == "number":
                v = values.get(q.code)
                if v is not None:
                    n = parse_number(v)
                    if n is None:
                        warnings[("not a valid number (left out)", q.code)] += 1
                    else:
                        b.asked.add(q.code)
                        b.numbers[q.code] = n
            elif q.kind == "banded":
                read_banded(q, values, b, options, warnings)
            elif q.kind == "consider":
                read_considered(values, b, options)
            elif q.kind == "reject":
                if values:
                    b.asked.add(q.code)
                    for c, v in values.items():
                        if v.lower() not in NOT_SELECTED:
                            b.rejections.append((int(c[-1]), re.sub(r"c\d$", "", c)))
        return b

    def multi_labels(self) -> dict:
        """{question code: {column: label}} for several-answer questions: the most common text in each
        column (ties: the alphabetically first). Columns nobody chose get no option at all."""
        out: dict = defaultdict(dict)
        for q in QUESTIONS:
            if q.kind == "multi":
                for c in self.by_question.get(q.code, []):
                    counts = self.multi_counts[c]
                    if counts:
                        top = max(counts.values())
                        out[q.code][c] = min(v for v, n in counts.items() if n == top)
        return out

    def finish(self) -> dict:
        """Everything except the buyers, once all rows are read."""
        for code, labels in self.multi_labels().items():
            self.options[code].update(labels)
        return {
            "options": {k: dict(v) for k, v in self.options.items()},
            "wordings": {code: question_wording(self.headers[cols[0]], BY_CODE[code])
                         for code, cols in self.by_question.items()},
        }


def read_file(source: Union[str, IO[bytes]]) -> ParsedFile:
    """The whole file at once: for tests and small files. Uploads use FileReader directly."""
    reader = FileReader(source)
    buyers = list(reader.buyers())
    rest = reader.finish()
    return ParsedFile(buyers=buyers, options=rest["options"], imported_columns=reader.imported_columns,
                      dropped=reader.dropped, warnings=reader.warnings, wordings=rest["wordings"])


def read_banded(q: Q, values: dict, b: Buyer, options: dict, warnings: Counter) -> None:
    for label, *_ in q.bands:
        options[q.code].setdefault(label, label)
    exact = values.get(q.code)
    if exact is not None:
        n = parse_number(exact)
        band = band_for(n, q.bands) if n is not None else None
        if band is None:
            warnings[("outside the questionnaire's bands (left out)", q.code)] += 1
            return
        b.asked.add(q.code)
        b.numbers[q.code] = n
        b.answers[q.code].add(band)
        return
    if q.code == "B6":
        ranged = INCOME_RANGE_LABELS.get(values.get("B6_1") or "")
        refused = (values.get("noanswerB6_n1") or "").lower() not in NOT_SELECTED | {""}
        if ranged:
            b.asked.add(q.code)
            b.answers[q.code].add(ranged)
        elif refused or values.get("B6_1"):
            options[q.code].setdefault(PREFER_NOT_TO_SAY, PREFER_NOT_TO_SAY)
            b.asked.add(q.code)
            b.answers[q.code].add(PREFER_NOT_TO_SAY)


def read_considered(values: dict, b: Buyer, options: dict) -> None:
    """E5 grid: c1 = considered, c2 = test drive, c3 = dealership visit, one row per model code.
    E5_1r1 = "I did not consider any other model": stored as an E5 answer, so it can be counted and shown."""
    none = values.get(E5_NONE_COLUMN)
    if none is not None and none.lower() not in NOT_SELECTED:
        options["E5"].setdefault(E5_NONE_KEY, E5_NONE_LABEL)
        b.answers["E5"].add(E5_NONE_KEY)
    grid: dict = defaultdict(dict)
    for c, v in values.items():
        m = re.match(r"^E5r(\d+(?:_\d+)?)c(\d)$", c)
        if m:
            grid[m.group(1)][m.group(2)] = v.lower() not in NOT_SELECTED
    if not values:
        return
    b.asked.add("E5")
    picked = sorted((code for code, cells in grid.items() if cells.get("1")), key=e5_sort_key)
    for rank, code in enumerate(picked, start=1):
        b.considered.append((rank, code, grid[code].get("2", False), grid[code].get("3", False)))
