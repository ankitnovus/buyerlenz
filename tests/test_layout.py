"""Customisable Home and Buyer Profile: each person's own layout, with every rule still applied."""
from app.db import SessionLocal
from app.models import ActivityLog, Company, User
from app.security import hash_password
from tests.conftest import PASSWORD, new_browser
from tests.test_engine import buyers, client, publish

MY_HOME = {"tiles": [{"kind": "share", "question": "E10", "answer": "CNG"},
                     {"kind": "amount", "question": "E4c2"},
                     {"kind": "satisfaction", "question": "F1"}],
           "charts": ["B2", "E15"], "movers": False, "top_models": False}


def save_home(c, layout, status=200):
    r = c.put("/api/client/layout/home", json={"layout": layout})
    assert r.status_code == status, r.text
    return r.json()


def test_standard_layout_until_a_person_customises():
    publish(buyers(40))
    c = client()
    home = c.get("/api/client/home").json()
    assert not home["customised"]
    assert [t["question"] for t in home["tiles"]] == ["F1", "F4", "E3", "E5"]
    assert [ch["code"] for ch in home["charts"]] == ["E10", "D2", "B6", "E15", "E7"]
    menu = c.get("/api/client/layout").json()["menu"]
    assert any(t["question"] == "E10" and "Petrol" in t["answers"] for t in menu["tiles"])


def test_saved_home_is_used_and_reset_brings_back_the_standard():
    publish(buyers(30, fuel="CNG", emi="10000"), buyers(10, fuel="Petrol", emi="50000"))
    c = client()
    save_home(c, MY_HOME)
    home = c.get("/api/client/home").json()
    assert home["customised"]
    cng, emi, _ = (t["data"] for t in home["tiles"])
    assert cng["headline"]["pct"] == 75.0 and cng["n"] == 40
    assert emi["median"] == 10000 and emi["n"] == 40  # the middle value, not pulled up by the 50,000s
    assert [ch["code"] for ch in home["charts"]] == ["B2", "E15"]
    assert home["top_models"] is None and home["movers"] is None
    save_home(c, None)
    assert [t["question"] for t in c.get("/api/client/home").json()["tiles"]] == ["F1", "F4", "E3", "E5"]


def test_limits_and_nonsense_are_refused():
    publish(buyers(40))
    c = client()
    eight = [{"kind": "share", "question": "E10", "answer": "Petrol"}] + [
        {"kind": "share", "question": "B2", "answer": "Male"}] * 7
    save_home(c, {**MY_HOME, "tiles": eight}, 400)  # 8 + the Buyers tile is too many
    save_home(c, {**MY_HOME, "tiles": [{"kind": "share", "question": "E10", "answer": "Hydrogen"}]}, 400)
    save_home(c, {**MY_HOME, "tiles": [{"kind": "satisfaction", "question": "E10"}]}, 400)
    save_home(c, {**MY_HOME, "charts": ["E2r2c1"]}, 400)  # an amount isn't a bar chart
    save_home(c, {**MY_HOME, "charts": ["B1"] * 10}, 400)


def test_each_person_keeps_their_own_layout():
    publish(buyers(40, fuel="CNG"))
    with SessionLocal() as db:
        co = db.query(Company).filter_by(name="Hyundai Motor India").one()
        db.add(User(email="meera@hyundai.example", name="Meera", password_hash=hash_password(PASSWORD),
                    role="client", company_id=co.id))
        db.commit()
    c = client()
    save_home(c, MY_HOME)
    colleague = new_browser()
    colleague.post("/api/auth/login", json={"email": "meera@hyundai.example", "password": PASSWORD})
    assert not colleague.get("/api/client/home").json()["customised"]


def test_custom_tiles_follow_the_small_group_rules():
    publish(buyers(5, fuel="CNG", emi="12000"), buyers(40, model="Seltos"))
    c = client()
    save_home(c, MY_HOME)
    for t in c.get("/api/client/home").json()["tiles"]:
        assert t["data"]["status"] == "hidden" and "n" not in t["data"]
    preview = c.post("/api/client/tile", json={"tile": {"kind": "share", "question": "E10", "answer": "CNG"}}).json()
    assert preview["data"]["status"] == "hidden" and "headline" not in preview["data"]


def test_profile_shows_the_chosen_cards_in_order():
    publish(buyers(40))
    c = client()
    creta = next(e["key"] for e in c.get("/api/client/entities").json() if e["label"] == "Creta")
    layout = c.get("/api/client/layout").json()["profile"]
    mine = {**{t: [] for t in layout}, "who": ["B2", "B1"], "feel": ["F1"]}
    assert c.put("/api/client/layout/profile", json={"layout": mine}).status_code == 200
    p = c.post("/api/client/profile", json={"entities": [creta]}).json()
    assert [[card["code"] for card in s["cards"]] for s in p["sections"]] == [["B2", "B1"], [], [], ["F1"]]
    bad = {**mine, "who": ["F1"]}  # satisfaction doesn't belong in "Who they are"
    assert c.put("/api/client/layout/profile", json={"layout": bad}).status_code == 400
    empty = {t: [] for t in layout}
    assert c.put("/api/client/layout/profile", json={"layout": empty}).status_code == 400


def test_layout_changes_are_logged():
    publish(buyers(40, fuel="CNG"))
    c = client()
    save_home(c, MY_HOME)
    save_home(c, None)
    with SessionLocal() as db:
        actions = [a.action for a in db.query(ActivityLog).order_by(ActivityLog.id)]
    assert actions[-2:] == ["layout_changed", "layout_reset"]
