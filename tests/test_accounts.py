"""Client accounts: temporary passwords, seats, removed quarters, deactivation, and View as client."""
from tests.conftest import CHROME_WIN, PASSWORD, new_browser
from tests.test_engine import buyers, publish

ADMIN = {"email": "admin@novus.example", "password": PASSWORD}
NEW_PASSWORD = "a-brand-new-password"


def admin():
    a = new_browser(CHROME_WIN)
    a.post("/api/auth/login", json={**ADMIN, "force": True})
    return a


def hyundai_brand(a):
    return next(b["id"] for b in a.get("/api/admin/brands").json() if b["name"] == "Hyundai")


def new_client(a, **extra):
    body = {"name": "Test Motors", "brand_id": hyundai_brand(a), "seat_limit": 1, **extra}
    r = a.post("/api/admin/clients", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def add_user(a, company_id, email="priya@test.example"):
    return a.post(f"/api/admin/clients/{company_id}/users", json={"name": "Priya Shah", "email": email})


def first_sign_in(email, temp):
    c = new_browser()
    me = c.post("/api/auth/login", json={"email": email, "password": temp, "force": True}).json()
    assert me["must_change_password"] is True
    assert c.get("/api/client/context").json()["detail"]["code"] == "change_password"  # blocked until changed
    assert c.post("/api/auth/change-password", json={"current_password": "wrong", "new_password": NEW_PASSWORD}).status_code == 400
    assert c.post("/api/auth/change-password", json={"current_password": temp, "new_password": "short"}).status_code == 400
    assert c.post("/api/auth/change-password", json={"current_password": temp, "new_password": NEW_PASSWORD}).status_code == 200
    return c


def test_new_user_gets_a_temporary_password_and_must_change_it():
    a = admin()
    co = new_client(a)
    r = add_user(a, co["id"]).json()
    assert len(r["temporary_password"]) == 14
    c = first_sign_in(r["email"], r["temporary_password"])
    assert c.get("/api/client/context").status_code == 200


def test_seat_limit():
    a = admin()
    co = new_client(a)
    assert add_user(a, co["id"]).status_code == 200
    assert add_user(a, co["id"], "second@test.example").status_code == 409
    assert a.patch(f"/api/admin/clients/{co['id']}", json={"seat_limit": 2}).status_code == 200
    assert add_user(a, co["id"], "second@test.example").status_code == 200
    assert a.patch(f"/api/admin/clients/{co['id']}", json={"seat_limit": 1}).status_code == 409


def test_clients_see_every_quarter_until_one_is_removed():
    publish(buyers(40, month="September 2025"), buyers(40, month="November 2025"))
    a = admin()
    co = new_client(a)  # no quarters chosen: every published one is visible
    r = add_user(a, co["id"]).json()
    c = first_sign_in(r["email"], r["temporary_password"])
    assert [q["id"] for q in c.get("/api/client/context").json()["quarters"]] == ["2025Q3", "2025Q4"]
    assert [q["id"] for q in a.get("/api/admin/quarters").json()] == ["2025Q3", "2025Q4"]
    assert a.patch(f"/api/admin/clients/{co['id']}", json={"removed_quarters": ["2025Q3"]}).status_code == 200
    assert [q["id"] for q in c.get("/api/client/context").json()["quarters"]] == ["2025Q4"]
    assert c.get("/api/client/home", params={"quarter": "2025Q3"}).status_code == 404
    assert c.get("/api/client/home", params={"compare": "prev"}).json()["compare"] is None  # Q3 was removed


def test_deactivated_users_and_closed_accounts_are_signed_out():
    a = admin()
    co = new_client(a)
    r = add_user(a, co["id"]).json()
    c = first_sign_in(r["email"], r["temporary_password"])
    a.post(f"/api/admin/users/{r['id']}/deactivate")
    assert c.get("/api/auth/me").status_code == 401
    assert new_browser().post("/api/auth/login", json={"email": r["email"], "password": NEW_PASSWORD}).status_code == 401
    a.post(f"/api/admin/users/{r['id']}/reactivate")
    assert new_browser().post("/api/auth/login", json={"email": r["email"], "password": NEW_PASSWORD}).status_code == 200
    a.patch(f"/api/admin/clients/{co['id']}", json={"active": False})
    assert new_browser().post("/api/auth/login", json={"email": r["email"], "password": NEW_PASSWORD}).status_code == 401


def test_reset_password_signs_out_and_issues_a_new_temporary_one():
    a = admin()
    co = new_client(a)
    r = add_user(a, co["id"]).json()
    c = first_sign_in(r["email"], r["temporary_password"])
    reset = a.post(f"/api/admin/users/{r['id']}/reset-password").json()
    assert c.get("/api/auth/me").status_code == 401
    first_sign_in(r["email"], reset["temporary_password"])


def test_view_as_client_is_for_novus_staff_only():
    publish(buyers(40, month="September 2025"), buyers(40, month="November 2025"))
    a = admin()
    assert a.get("/api/client/context").status_code == 403  # not viewing as anyone yet
    assert a.post("/api/admin/view-as", json={"brand_id": hyundai_brand(a)}).status_code == 200
    ctx = a.get("/api/client/context").json()
    assert ctx["brand"] == "Hyundai" and ctx["staff_view"] is True
    assert [q["id"] for q in ctx["quarters"]] == ["2025Q3", "2025Q4"]  # staff see every quarter
    assert a.get("/api/auth/me").json()["view_as"] == "Hyundai"
    a.post("/api/admin/view-as", json={"brand_id": None})
    assert a.get("/api/client/context").status_code == 403

    client = new_browser()
    client.post("/api/auth/login", json={"email": "arjun@hyundai.example", "password": PASSWORD, "force": True})
    assert client.post("/api/admin/view-as", json={"brand_id": 1}).status_code == 403


def test_client_detail_lists_users_and_seat_requests():
    a = admin()
    co = new_client(a, seat_limit=2)
    r = add_user(a, co["id"]).json()
    c = first_sign_in(r["email"], r["temporary_password"])
    new_browser().post("/api/auth/seat-request", json={"email": r["email"], "password": NEW_PASSWORD})
    d = a.get(f"/api/admin/clients/{co['id']}").json()
    assert d["users"][0]["signed_in_on"] == "Mac · Chrome" and d["seats_used"] == 1
    assert d["open_seat_requests"] == 1
    a.post(f"/api/admin/seat-requests/{d['seat_requests'][0]['id']}/handled")
    assert a.get(f"/api/admin/clients/{co['id']}").json()["open_seat_requests"] == 0
    assert c.get("/api/auth/me").status_code == 200
