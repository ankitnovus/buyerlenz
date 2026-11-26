"""Buyer Profile rules, end to end."""
from tests.test_engine import buyers, client, publish


def entity_key(c, label, kind="model"):
    return next(e["key"] for e in c.get("/api/client/entities").json() if e["label"] == label and e["kind"] == kind)


def profile(c, **body):
    r = c.post("/api/client/profile", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def card(p, code):
    return next(cd for s in p["sections"] for cd in s["cards"] if cd["code"] == code)


def test_one_model_against_all_buyers():
    publish(buyers(40, buyer_type="First Time Buyer"), buyers(40, model="Seltos", buyer_type="Replacement Car Buyer"))
    c = client()
    p = profile(c, entities=[entity_key(c, "Creta")])
    assert [s["theme"] for s in p["sections"]] == ["who", "think", "bought", "feel"]
    assert p["entities"][0]["mine"] is True and p["entities"][0]["base"]["n"] == 40
    assert p["entities"][0]["base"]["share_pct"] == 50.0
    d2 = card(p, "D2")
    assert d2["groups"][0]["values"]["First Time Buyer"]["pct"] == 100.0
    assert d2["market"]["First Time Buyer"] == 50.0
    assert p["standouts"][0]["items"][0]["label"] in ("First Time Buyer", "Replacement Car Buyer")


def test_compare_several_and_at_most_eight():
    publish(buyers(40), buyers(40, model="Seltos"))
    c = client()
    keys = [entity_key(c, "Creta"), entity_key(c, "Seltos"), entity_key(c, "Hyundai", "brand")]
    p = profile(c, entities=keys)
    assert [g["key"] for g in card(p, "B2")["groups"]] == keys
    nine = [e["key"] for e in c.get("/api/client/entities").json()[:9]]
    assert c.post("/api/client/profile", json={"entities": nine}).status_code == 422


def test_hidden_groups_get_no_numbers_and_counts_are_never_sent():
    publish(buyers(40), buyers(5, model="Seltos"))
    c = client()
    listed = next(e for e in c.get("/api/client/entities").json() if e["label"] == "Seltos")
    assert listed["status"] == "hidden" and "n" not in listed
    p = profile(c, entities=[entity_key(c, "Creta"), listed["key"]])
    seltos = p["entities"][1]["base"]
    assert seltos == {"status": "hidden"}
    group = card(p, "B2")["groups"][1]
    assert group == {"key": listed["key"], "status": "hidden"}


def test_filters_shrink_the_base_and_small_groups_lose_the_market_comparison():
    publish(buyers(25, fuel="Diesel"), buyers(25, fuel="Petrol"), buyers(40, model="Seltos", fuel="Diesel"))
    c = client()
    diesel = next(o["id"] for f in c.get("/api/client/context").json()["filters"] if f["code"] == "E10"
                  for o in f["options"] if o["label"] == "Diesel")
    p = profile(c, entities=[entity_key(c, "Creta")], filters={"E10": diesel})
    assert p["filtered"] is True
    b2 = card(p, "B2")
    assert b2["groups"][0]["status"] == "caution" and b2["groups"][0]["n"] == 25
    assert b2["market_comparison"] is False and "market" not in b2
    assert c.post("/api/client/profile", json={"entities": [entity_key(c, "Creta")], "filters": {"E10": 999999}}).status_code == 400


def test_trend_keeps_gaps_as_gaps():
    publish(buyers(40, month="September 2025"), buyers(40, month="February 2026"),
            buyers(40, model="Seltos", month="November 2025"))
    c = client()
    p = profile(c, entities=[entity_key(c, "Creta")], trend=True)
    assert [q["id"] for q in p["trend_quarters"]] == ["2025Q3", "2025Q4", "2026Q1"]
    trend = card(p, "B2")["groups"][0]["values"]["Male"]["trend"]
    assert trend[0]["pct"] == 100.0 and trend[2]["pct"] == 100.0
    assert trend[1] == {"pct": None, "status": "no_data"}  # Creta had no buyers in Q4: a gap, never 0


def test_models_also_considered_are_named():
    publish(buyers(40, considered=(("9", False, False), ("55", False, False))))
    c = client()
    e5 = card(profile(c, entities=[entity_key(c, "Creta")]), "E5")
    assert set(e5["answers"]) == {"Grand Vitara", "Seltos"}
    assert e5["groups"][0]["values"]["Seltos"]["pct"] == 100.0


def test_fuel_note_for_one_model():
    publish(buyers(40, fuel="Petrol"), buyers(40, model="Seltos", fuel="Diesel"))
    c = client()
    e10 = card(profile(c, entities=[entity_key(c, "Creta")]), "E10")
    assert e10["answers"] == ["Petrol"]
    assert "haven’t bought diesel" in e10["note"]


def test_age_bands_keep_their_order():
    publish(buyers(30, age="30"), buyers(10, age="20"))
    c = client()
    assert card(profile(c, entities=[entity_key(c, "Creta")]), "B1")["answers"][:5] == [
        "18–25", "26–35", "36–45", "46–55", "56 and above"]


def test_buyers_who_considered_no_other_model_are_shown():
    publish(buyers(30), buyers(10, considered=()))  # 10 of 40 bought without weighing up an alternative
    c = client()
    creta = entity_key(c, "Creta")
    p = c.post("/api/client/profile", json={"entities": [creta]}).json()
    e5 = next(card for s in p["sections"] for card in s["cards"] if card["code"] == "E5")
    assert e5["answers"][0] == "Didn’t consider any other model"  # always first, apart from the models
    assert e5["groups"][0]["values"]["Didn’t consider any other model"]["pct"] == 25.0
    tile = next(t for t in c.get("/api/client/home").json()["tiles"] if t["question"] == "E5")
    assert tile["data"]["headline"]["pct"] == 25.0 and tile["data"]["n"] == 40
