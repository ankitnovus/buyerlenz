"""The admin activity log and its "worth a look" flags."""
from sqlalchemy import select

from app.db import SessionLocal
from app.models import ActivityLog, User
from tests.conftest import CHROME_WIN, new_browser
from tests.test_accounts import admin
from tests.test_engine import CLIENT, buyers, client, publish
from tests.test_profile import entity_key


def log_rows(a, **params):
    r = a.get("/api/admin/activity", params=params)
    assert r.status_code == 200, r.text
    return r.json()["rows"]


def test_log_reads_in_plain_english_and_filters():
    publish(buyers(40))
    c = client()
    creta = entity_key(c, "Creta")
    c.post("/api/client/cut/export", json={"questions": ["E10"], "columns": [creta]})
    a = admin()

    rows = log_rows(a)
    assert rows[0]["summary"].startswith("Signed in on Windows")  # the admin, newest first
    export = next(r for r in rows if r["action"] == "export")
    assert export["user"] == "Arjun Mehta" and export["company"] == "Hyundai Motor India"
    assert export["summary"].startswith("Copied to Excel, 1 question(s) × 1 column(s) · Q")

    assert {r["action"] for r in log_rows(a, kind="exports")} == {"export"}
    assert {r["company"] for r in log_rows(a, client="novus")} == {"Novus Insights"}
    filters = a.get("/api/admin/activity/filters").json()
    arjun = next(u["id"] for u in filters["users"] if u["name"] == "Arjun Mehta")
    assert {r["user"] for r in log_rows(a, user=arjun)} == {"Arjun Mehta"}


def test_clients_cannot_read_the_log():
    assert client().get("/api/admin/activity").status_code == 403


def test_worth_a_look_flags_heavy_copying_shared_logins_and_guessing():
    with SessionLocal() as db:
        arjun = db.scalar(select(User).where(User.email == CLIENT["email"]))
        db.add_all([ActivityLog(user_id=arjun.id, company_id=arjun.company_id, action="export", detail={}, ip="1.2.3.4")
                    for _ in range(15)])
        db.commit()
    for _ in range(4):  # signing in again and again while signed in elsewhere
        new_browser(CHROME_WIN).post("/api/auth/login", json={**CLIENT, "force": True})
    for i in range(10):
        new_browser().post("/api/auth/login", json={"email": f"nobody{i}@example.com", "password": "guess"})

    titles = {f["title"] for f in admin().get("/api/admin/activity/watch").json()}
    assert {"Close to the monthly copy allowance", "Many copies in one day", "This login may be shared",
            "Someone may be guessing passwords"} <= titles
    assert admin().get("/api/admin/summary").json()["worth_a_look"] >= 4


def test_quiet_use_raises_no_flags():
    publish(buyers(40))
    c = client()
    c.get("/api/client/home")
    assert admin().get("/api/admin/activity/watch").json() == []


def test_staff_views_are_marked_in_the_log():
    publish(buyers(40))
    a = admin()
    kia = next(b["id"] for b in a.get("/api/admin/brands").json() if b["name"] == "Kia")
    a.post("/api/admin/view-as", json={"brand_id": kia})
    assert a.get("/api/client/home").status_code == 200
    home = next(r for r in log_rows(a) if r["action"] == "view_home")
    assert home["staff"] is True and home["summary"].endswith("(viewing as Kia)")
