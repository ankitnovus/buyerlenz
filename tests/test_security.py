"""Security protections: who can reach what, requests from other websites, sign-in limits, unsafe files,
and the activity log that can't be changed."""
import io
import re
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.cuts_api import safe_cell
from app.db import engine
from app.main import app
from tests.conftest import new_browser
from tests.test_accounts import admin
from tests.test_engine import CLIENT, client


def api_routes(prefix):
    """Every (method, address) the server offers under prefix, with 1 for any id in the address."""
    for path, methods in app.openapi()["paths"].items():
        if path.startswith(prefix):
            for method in methods:
                yield method.upper(), re.sub(r"\{[^}]+\}", "1", path)


def call(c, method, path):
    return c.request(method, path, json={} if method != "GET" else None)


def test_every_admin_page_refuses_visitors_and_clients():
    routes = list(api_routes("/api/admin"))
    assert len(routes) >= 25  # all of them, so a new admin page is checked automatically
    c = client()
    for method, path in routes:
        assert call(new_browser(), method, path).status_code == 401, (method, path)
        assert call(c, method, path).status_code == 403, (method, path)


def test_every_client_page_refuses_visitors():
    for method, path in api_routes("/api/client"):
        assert call(new_browser(), method, path).status_code == 401, (method, path)


def test_changes_must_come_from_the_app():
    bare = TestClient(app)  # no X-BuyerLenz header: what a hidden form on another website would send
    r = bare.post("/api/auth/login", json={**CLIENT, "force": True})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "not_from_app"
    c = client()
    r = c.post("/api/client/saved-cuts", json={"name": "x", "questions": ["B1"], "columns": ["all"]},
               headers={"origin": "https://evil.example"})
    assert r.status_code == 403


def test_safety_headers_and_hidden_developer_pages():
    r = new_browser().get("/api/health")
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"
    for path in ("/docs", "/api/docs", "/openapi.json", "/api/openapi.json"):
        assert new_browser().get(path).status_code == 404


def test_oversized_requests_and_passwords_are_refused():
    c = new_browser()
    assert c.post("/api/auth/login", json={"email": CLIENT["email"], "password": "x" * 5000}).status_code == 422
    assert c.post("/api/auth/login", content=b"{" + b" " * 400_000 + b"}",
                  headers={"content-type": "application/json"}).status_code == 413


def test_many_failures_from_one_address_pause_sign_in_there():
    c = new_browser()
    for i in range(20):
        c.post("/api/auth/login", json={"email": f"guess{i}@example.com", "password": "wrong"})
    r = c.post("/api/auth/login", json={**CLIENT, "force": True})  # even the right password waits
    assert r.status_code == 429 and r.json()["detail"]["code"] == "throttled"


def test_guessing_the_current_password_locks_and_signs_out():
    c = client()
    for _ in range(4):
        assert c.post("/api/auth/change-password",
                      json={"current_password": "wrong", "new_password": "a-new-password-1"}).status_code == 400
    r = c.post("/api/auth/change-password", json={"current_password": "wrong", "new_password": "a-new-password-1"})
    assert r.status_code == 401
    assert c.get("/api/auth/me").status_code == 401
    assert new_browser().post("/api/auth/login", json={**CLIENT, "force": True}).status_code == 429


def bomb() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("xl/worksheets/sheet1.xml", b"0" * (20 * 1024 * 1024))  # 20 MB that squeezes to a few KB
    return buf.getvalue()


def test_booby_trapped_excel_files_are_not_opened():
    r = admin().post("/api/admin/uploads", files={"file": ("trap.xlsx", bomb())})
    assert r.status_code == 400
    assert "keep the server safe" in r.json()["detail"]["message"]


def test_the_activity_log_cannot_be_changed_or_deleted():
    client()  # writes a sign-in entry
    for sql in ("UPDATE activity_log SET action = 'nothing'", "DELETE FROM activity_log"):
        with pytest.raises(Exception, match="can only be added to"):
            with engine.begin() as conn:
                conn.execute(text(sql))


def test_copied_cells_cannot_run_as_excel_formulas():
    assert safe_cell("=HYPERLINK(\"http://x\")") == "'=HYPERLINK(\"http://x\")"
    assert safe_cell("@SUM(A1)") == "'@SUM(A1)"
    assert safe_cell("Petrol\tCNG") == "Petrol CNG"
    assert safe_cell("Creta") == "Creta" and safe_cell(42) == 42


def test_changes_from_the_apps_own_address_are_accepted():
    c = client()
    r = c.post("/api/client/saved-cuts", json={"name": "x", "questions": ["B1"], "columns": ["all"]},
               headers={"origin": "http://testserver"})
    assert r.json().get("detail", {}).get("code") != "not_from_app"
