"""Data cut rules, end to end."""
from sqlalchemy import select

from app.db import SessionLocal
from app.models import ActivityLog, User
from tests.test_engine import buyers, client, publish
from tests.test_profile import entity_key


def cut(c, status=200, **body):
    r = c.post("/api/client/cut", json=body)
    assert r.status_code == status, r.text
    return r.json()


def test_table_has_all_buyers_first_and_marks_options_a_model_doesnt_offer():
    publish(buyers(40, fuel="Petrol"), buyers(40, model="Seltos", fuel="CNG"))
    c = client()
    creta, seltos = entity_key(c, "Creta"), entity_key(c, "Seltos")
    t = cut(c, questions=["E10", "B2"], columns=[creta, seltos])
    assert [col["key"] for col in t["columns"]] == ["all", creta, seltos]
    fuel = t["blocks"][0]
    assert set(fuel["answers"]) == {"Petrol", "CNG"}
    assert fuel["cells"][creta]["values"]["Petrol"] == {"pct": 100.0, "gap": 50}
    assert fuel["cells"][creta]["not_offered"] == ["CNG"]
    assert "CNG" not in fuel["cells"][creta]["values"]
    assert fuel["cells"]["all"]["values"]["CNG"]["pct"] == 50.0


def test_caps_are_enforced_on_the_server():
    publish(buyers(40))
    c = client()
    creta = entity_key(c, "Creta")
    cut(c, 422, questions=["B1", "B2", "B6", "D2", "E10", "E3"], columns=[creta])
    keys = [e["key"] for e in c.get("/api/client/entities").json()][:26]
    cut(c, 422, questions=["B1"], columns=keys)
    cut(c, 400, questions=["E2r1c1"], columns=[creta])  # exact numbers aren't a data cut question


def test_hidden_columns_get_no_numbers():
    publish(buyers(40), buyers(4, model="Seltos"))
    c = client()
    seltos = entity_key(c, "Seltos")
    t = cut(c, questions=["B2"], columns=[seltos])
    assert t["columns"][1]["base"] == {"status": "hidden"}
    assert t["blocks"][0]["cells"][seltos] == {"status": "hidden"}


def test_copy_to_excel_counts_against_the_allowance_and_is_logged():
    publish(buyers(40, fuel="Petrol"), buyers(40, model="Seltos", fuel="CNG"))
    c = client()
    body = {"questions": ["E10"], "columns": [entity_key(c, "Creta"), entity_key(c, "Seltos")]}
    before = c.get("/api/client/allowance").json()
    assert before["used"] == 0 and before["limit"] == 20

    preview = c.post("/api/client/cut/export", json={**body, "preview": True}).json()
    assert preview["allowance"]["used"] == 0  # previewing is free
    rows = preview["rows"]
    assert rows[0][0].startswith("BuyerLenz data cut")
    assert "Hyundai Motor India" in rows[1][0]
    assert rows[3] == ["Question", "Answer", "All new-car buyers", "Creta", "Seltos"]
    assert rows[4] == ["E10 Fuel type", "Base (n)", 80, 40, 40]
    cng = next(r for r in rows[4:] if r[1] == "CNG")
    assert cng == ["", "CNG", 50, "not offered", 100]  # a real number, or a clear state; never a fake 0

    done = c.post("/api/client/cut/export", json=body).json()
    assert done["allowance"]["used"] == 1 and done["allowance"]["remaining"] == 19
    with SessionLocal() as db:
        assert db.scalar(select(ActivityLog).where(ActivityLog.action == "export")) is not None


def test_no_copy_once_the_allowance_is_used():
    publish(buyers(40))
    c = client()
    with SessionLocal() as db:
        uid = db.scalar(select(User.id).where(User.email == "arjun@hyundai.example"))
        db.add_all(ActivityLog(user_id=uid, action="export", detail={}) for _ in range(20))
        db.commit()
    r = c.post("/api/client/cut/export", json={"questions": ["B2"], "columns": [entity_key(c, "Creta")]})
    assert r.status_code == 429 and "allowance" in r.json()["detail"]["message"]


def test_trend_keeps_gaps():
    publish(buyers(40, month="September 2025"), buyers(40, model="Seltos", month="November 2025"),
            buyers(40, month="January 2026"))
    c = client()
    creta = entity_key(c, "Creta")
    t = c.post("/api/client/cut/trend", json={"columns": [creta], "question": "B2", "answer": "Male"}).json()
    assert [q["id"] for q in t["quarters"]] == ["2025Q3", "2025Q4", "2026Q1"]
    pts = t["lines"][0]["points"]
    assert pts[0]["pct"] == 100.0 and pts[1] == {"pct": None, "status": "no_data"} and pts[2]["pct"] == 100.0
    assert c.post("/api/client/cut/trend", json={"columns": [creta] * 9, "question": "B2", "answer": "Male"}).status_code == 422


def test_saved_cuts():
    publish(buyers(40))
    c = client()
    creta = entity_key(c, "Creta")
    saved = c.post("/api/client/saved-cuts", json={"name": "My cut", "questions": ["B2"], "columns": [creta]}).json()
    assert [s["name"] for s in c.get("/api/client/saved-cuts").json()] == ["My cut"]
    assert c.delete(f"/api/client/saved-cuts/{saved['id']}").status_code == 200
    assert c.get("/api/client/saved-cuts").json() == []
