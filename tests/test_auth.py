from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from app.db import SessionLocal
from app.models import ActivityLog, LoginSession, SeatRequest
from tests.conftest import CHROME_WIN, PASSWORD, new_browser

ADMIN = {"email": "admin@novus.example", "password": PASSWORD}
CLIENT = {"email": "arjun@hyundai.example", "password": PASSWORD}


def actions():
    with SessionLocal() as db:
        return [a.action for a in db.scalars(select(ActivityLog).order_by(ActivityLog.id))]


def test_sign_in_and_who_am_i():
    b = new_browser()
    r = b.post("/api/auth/login", json={**ADMIN, "email": "  Admin@Novus.example "})
    assert r.status_code == 200
    assert r.json() == {"name": "Asha Admin", "email": "admin@novus.example", "role": "admin", "company": "Novus Insights",
                        "must_change_password": False}
    assert b.get("/api/auth/me").json()["role"] == "admin"
    assert "sign_in" in actions()


def test_wrong_password_and_unknown_email_get_the_same_message():
    b = new_browser()
    wrong = b.post("/api/auth/login", json={**ADMIN, "password": "nope"})
    unknown = b.post("/api/auth/login", json={"email": "nobody@x.example", "password": "nope"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert b.get("/api/auth/me").status_code == 401


def test_five_wrong_passwords_lock_the_login_even_with_the_right_password():
    b = new_browser()
    for _ in range(5):
        b.post("/api/auth/login", json={**CLIENT, "password": "nope"})
    r = b.post("/api/auth/login", json=CLIENT)
    assert r.status_code == 429
    assert r.json()["detail"]["code"] == "locked"


def test_one_device_at_a_time():
    mac, windows = new_browser(), new_browser(CHROME_WIN)
    assert mac.post("/api/auth/login", json=CLIENT).status_code == 200

    blocked = windows.post("/api/auth/login", json=CLIENT)
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "active_elsewhere"
    assert blocked.json()["detail"]["device"] == "Mac · Chrome"
    assert mac.get("/api/auth/me").status_code == 200  # the first device is untouched

    assert windows.post("/api/auth/login", json={**CLIENT, "force": True}).status_code == 200
    gone = mac.get("/api/auth/me")
    assert gone.status_code == 401
    assert gone.json()["detail"]["code"] == "replaced"
    assert windows.get("/api/auth/me").status_code == 200


def test_signing_in_again_in_the_same_browser_is_not_blocked():
    b = new_browser()
    b.post("/api/auth/login", json=CLIENT)
    assert b.post("/api/auth/login", json=CLIENT).status_code == 200


def test_sign_out():
    b = new_browser()
    b.post("/api/auth/login", json=CLIENT)
    assert b.post("/api/auth/logout").status_code == 200
    assert b.get("/api/auth/me").status_code == 401
    assert new_browser(CHROME_WIN).post("/api/auth/login", json=CLIENT).status_code == 200  # seat is free again
    assert "sign_out" in actions()


def test_idle_session_expires():
    b = new_browser()
    b.post("/api/auth/login", json=CLIENT)
    with SessionLocal() as db:
        db.execute(update(LoginSession).values(last_seen=datetime.now(timezone.utc) - timedelta(hours=9)))
        db.commit()
    r = b.get("/api/auth/me")
    assert r.status_code == 401 and r.json()["detail"]["code"] == "expired"


def test_admin_area_is_for_admins_only():
    client, admin = new_browser(), new_browser(CHROME_WIN)
    assert client.get("/api/admin/summary").status_code == 401  # not signed in
    client.post("/api/auth/login", json=CLIENT)
    assert client.get("/api/admin/summary").status_code == 403  # signed in, but not Novus staff
    admin.post("/api/auth/login", json=ADMIN)
    r = admin.get("/api/admin/summary")
    assert r.status_code == 200 and r.json()["client_companies"] == 1


def test_ask_for_another_seat_needs_the_password():
    new_browser().post("/api/auth/login", json=CLIENT)
    other = new_browser(CHROME_WIN)
    assert other.post("/api/auth/seat-request", json={**CLIENT, "password": "nope"}).status_code == 401
    r = other.post("/api/auth/seat-request", json=CLIENT)
    assert r.status_code == 200 and r.json()["company"] == "Hyundai Motor India"
    with SessionLocal() as db:
        assert db.scalar(select(SeatRequest)) is not None
    assert "seat_request" in actions()
