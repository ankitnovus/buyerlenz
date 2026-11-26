"""The calculation rules, checked end to end: upload made-up files, publish, then read what a client gets."""
from sqlalchemy import select, text

from app.db import SessionLocal
from app.engine import Engine, Scope
from app.models import ActivityLog
from app.views import bars
from tests.conftest import CHROME_WIN, PASSWORD, new_browser
from tests.sample_file import buyer, make_file

ADMIN = {"email": "admin@novus.example", "password": PASSWORD}
CLIENT = {"email": "arjun@hyundai.example", "password": PASSWORD}
_counter = iter(range(1, 10**6))


def buyers(count, **kw):
    return [buyer(str(next(_counter)), **kw) for _ in range(count)]


def publish(*groups, publish_it=True):
    a = new_browser(CHROME_WIN)
    a.post("/api/auth/login", json=ADMIN)
    rows = [b for g in groups for b in g]
    uid = a.post("/api/admin/uploads", files={"file": ("f.xlsx", make_file(rows))}).json()["id"]
    r = a.get(f"/api/admin/uploads/{uid}").json()  # read in the background, finished by now
    assert r["checks"]["blocking"] == [], r["checks"]
    if publish_it:
        assert a.post(f"/api/admin/uploads/{r['id']}/publish").status_code == 200
    a.post("/api/auth/logout")


def client():
    c = new_browser()
    c.post("/api/auth/login", json={**CLIENT, "force": True})  # one device per login: take over the seat
    return c


def home(**params):
    r = client().get("/api/client/home", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def option(block, label):
    return next(o for o in block["options"] if o["label"] == label)


def tile_of(h, code):
    """A Home tile's numbers, by question (F1 car satisfaction, F4 dealer, E3 loan in the standard layout)."""
    return next(t["data"] for t in h["tiles"] if t["question"] == code)


def chart_of(h, code):
    return next(c for c in h["charts"] if c["code"] == code)


def test_percentages_and_base():
    publish(buyers(30, f1="Extremely Satisfied", loan="Yes"), buyers(10, f1="Average", loan="No"))
    h = home()
    car = tile_of(h, "F1")
    assert car["status"] == "ok" and car["n"] == 40
    assert car["headline"]["pct"] == 75.0  # Satisfied (4-5) = 30 of 40
    assert car["mean"] == round((30 * 5 + 10 * 3) / 40, 2)
    assert tile_of(h, "E3")["headline"]["pct"] == 75.0
    assert h["buyers"] == {"status": "ok", "n": 40, "share_pct": 100.0}


def test_market_comparison_only_when_both_groups_are_solid():
    publish(buyers(40, f1="Extremely Satisfied"), buyers(40, model="Seltos", f1="Average"))
    car = tile_of(home(), "F1")
    assert car["market_comparison"] is True
    assert car["headline"]["pct"] == 100.0 and car["headline"]["market_pct"] == 50.0


def test_small_base_is_flagged_and_market_comparison_switched_off():
    publish(buyers(15), buyers(40, model="Seltos"))
    car = tile_of(home(), "F1")
    assert car["status"] == "caution" and car["n"] == 15
    assert car["market_comparison"] is False
    assert "market_pct" not in car["headline"]


def test_hidden_groups_send_no_numbers_at_all():
    publish(buyers(5), buyers(40, model="Seltos"))
    h = home()
    for block in (tile_of(h, "F1"), tile_of(h, "F4"), tile_of(h, "E3"), chart_of(h, "E10"), chart_of(h, "E15")):
        assert block["status"] == "hidden"
        assert set(block) <= {"code", "title", "wording", "status", "kind"}, block
    assert h["buyers"] == {"status": "hidden"}
    assert "n" not in h["top_models"][0]


def test_fuel_the_model_does_not_offer_is_left_out_not_zero():
    publish(buyers(20, fuel="Petrol"), buyers(20, fuel="Diesel"), buyers(40, model="Seltos", fuel="CNG"))
    labels = [o["label"] for o in chart_of(home(), "E10")["options"]]
    assert sorted(labels) == ["Diesel", "Petrol"]


def test_a_question_nobody_was_asked_is_not_asked_not_zero():
    publish(buyers(40, f3=None))
    with SessionLocal() as db:
        e = Engine(db)
        model_ids = tuple(db.execute(text("SELECT model_id FROM model_names WHERE name = 'Creta'")).scalars())
        f3 = bars(e, Scope("2025Q4", model_ids), "F3")
    assert f3["status"] == "not_asked" and "options" not in f3


def test_unpublished_uploads_are_invisible_to_clients():
    publish(buyers(40), publish_it=False)
    assert home() == {"quarters": [], "empty": True}


def test_changes_vs_previous_quarter_and_grey_when_small():
    publish(buyers(40, month="September 2025", loan="Yes"),
            buyers(40, month="November 2025", loan="Yes"), buyers(10, month="November 2025", loan="No"))
    h = home(quarter="2025Q4", compare="prev")
    assert h["compare"]["id"] == "2025Q3"
    ch = tile_of(h, "E3")["headline"]["change"]
    assert ch == {"kind": "change", "points": -20, "flat": False}  # 100% -> 80%
    assert h["movers"][0]["points"] == -20

    publish(buyers(38, month="December 2025", loan="Yes"), buyers(2, month="December 2025", loan="No"))
    small = tile_of(home(quarter="2025Q4", compare="prev"), "F1")["headline"]["change"]
    assert small["kind"] == "change" and small["flat"] is True


def test_change_needs_30_buyers_in_both_quarters():
    publish(buyers(12, month="September 2025"), buyers(40, month="November 2025"))
    ch = tile_of(home(quarter="2025Q4", compare="prev"), "E3")["headline"]["change"]
    assert ch == {"kind": "few"}


def test_why_buyers_rejected_the_brand():
    # Kia buyers who considered a Creta (rank 1 = lowest code: 47 before 55) and rejected it as too expensive
    publish(buyers(40), buyers(40, model="Seltos", considered=(("47", False, False),), rejected=((1, "79"),)))
    rej = chart_of(home(), "E7")
    assert rej["status"] == "ok" and rej["n"] == 40
    assert option(rej, "Too expensive")["pct"] == 100.0


def test_quarter_labels_and_partial_quarters():
    publish(buyers(40, month="September 2025"))
    q = home(quarter="2025Q3")["quarter"]
    assert q == {"id": "2025Q3", "label": "Q3 2025", "months": "Jul–Sep 2025", "covered": "Sep 2025 only",
                 "partial": True, "buyers": 40, "from": "Sep 2025", "to": "Sep 2025", "kind": "quarter"}


def test_only_client_accounts_with_a_brand_can_open_client_pages():
    a = new_browser(CHROME_WIN)
    a.post("/api/auth/login", json=ADMIN)
    assert a.get("/api/client/home").status_code == 403
    assert client().get("/api/client/context").json()["brand"] == "Hyundai"


def test_viewing_home_is_logged():
    publish(buyers(40))
    home()
    with SessionLocal() as db:
        entry = db.scalars(select(ActivityLog).where(ActivityLog.action == "view_home")).first()
    assert entry.detail["quarter"] == "Q4 2025"  # the period's name, as people read it


def test_numbers_refresh_straight_after_a_publish_or_undo():
    publish(buyers(40))
    c = client()
    assert c.get("/api/client/home").json()["buyers"]["n"] == 40
    publish(buyers(40))  # the kept summaries must not hide the new upload
    assert c.get("/api/client/home").json()["buyers"]["n"] == 80
    a = new_browser(CHROME_WIN)
    a.post("/api/auth/login", json={**ADMIN, "force": True})
    newest = a.get("/api/admin/uploads").json()[0]["id"]
    assert a.post(f"/api/admin/uploads/{newest}/roll-back").status_code == 200
    assert c.get("/api/client/home").json()["buyers"]["n"] == 40
