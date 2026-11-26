"""Periods: the trailing four quarters by default; quarter on quarter (any two quarters) when chosen;
trends over the trailing four quarters; nothing before January 2025, and a flag for anything before July 2025."""
import io

from app.ingest.reader import read_file
from tests.sample_file import buyer, make_file
from tests.test_engine import buyers, client, home, publish

FIVE_QUARTERS = ("September 2025", "November 2025", "February 2026", "May 2026", "August 2026")


def five_quarters():
    publish(*[buyers(40, month=m, loan="Yes" if i < 4 else "No") for i, m in enumerate(FIVE_QUARTERS)])


def test_default_is_the_trailing_four_quarters_with_no_changes():
    five_quarters()
    h = home()
    assert h["quarter"]["id"] == "year" and h["quarter"]["label"] == "Q4 2025 – Q3 2026"
    assert h["quarter"]["covered"] == "Nov 2025 – Aug 2026"
    assert h["buyers"]["n"] == 160  # four quarters together; September 2025 (the fifth back) is outside
    loan = next(t["data"] for t in h["tiles"] if t["question"] == "E3")
    assert loan["headline"]["pct"] == 75.0 and "change" not in loan["headline"]
    assert h["compare"] is None and h["movers"] is None


def test_quarter_on_quarter_compares_any_two_quarters():
    five_quarters()
    h = home(quarter="2026Q3", compare="2025Q3")  # same quarter a year apart
    assert h["quarter"]["id"] == "2026Q3" and h["compare"]["id"] == "2025Q3"
    assert h["buyers"]["n"] == 40
    loan = next(t["data"] for t in h["tiles"] if t["question"] == "E3")
    assert loan["headline"]["pct"] == 0.0 and loan["headline"]["change"]["points"] == -100
    assert home(quarter="2026Q3", compare="2026Q3")["compare"] is None  # a quarter can't be compared with itself


def test_trends_show_the_trailing_four_quarters():
    five_quarters()
    c = client()
    creta = next(e["key"] for e in c.get("/api/client/entities").json() if e["label"] == "Creta")
    p = c.post("/api/client/profile", json={"entities": [creta], "trend": True}).json()
    assert [q["id"] for q in p["trend_quarters"]] == ["2025Q4", "2026Q1", "2026Q2", "2026Q3"]
    t = c.post("/api/client/cut/trend", json={"columns": [creta], "question": "E3", "answer": "Yes"}).json()
    assert [q["id"] for q in t["quarters"]] == ["2025Q4", "2026Q1", "2026Q2", "2026Q3"]


def test_a_shorter_history_is_labelled_as_such():
    publish(buyers(40, month="November 2025"), buyers(40, month="February 2026"))
    q = home()["quarter"]
    assert q["label"] == "Q4 2025 – Q1 2026" and q["quarters"] == 2 and q["partial"] is True


def test_purchases_before_january_2025_are_left_out_and_before_july_2025_flagged():
    p = read_file(io.BytesIO(make_file([buyer("1", month="December 2024"), buyer("2", month="March 2025"),
                                        buyer("3", month="July 2025")])))
    assert [b.record for b in p.buyers] == ["2", "3"]
    assert p.warnings[("bought before January 2025, the earliest date accepted (left out)", "E9_1")] == 1
    assert p.warnings[("bought before July 2025, earlier than expected (kept; check the file)", "E9_1")] == 1
