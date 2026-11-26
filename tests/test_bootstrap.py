"""First start of an online server: the first admin comes from the host's settings and must pick a new password."""
from tests.conftest import new_browser
from app import bootstrap

START = "a-starting-password"


def test_first_admin_is_created_once_and_must_change_password(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAIL", "Owner@Novus.example")
    monkeypatch.setenv("ADMIN_NAME", "Product Owner")
    monkeypatch.setenv("ADMIN_PASSWORD", START)
    bootstrap.main()
    me = new_browser().post("/api/auth/login", json={"email": "owner@novus.example", "password": START}).json()
    assert me["role"] == "admin" and me["must_change_password"] is True

    monkeypatch.setenv("ADMIN_PASSWORD", "a-different-password")
    bootstrap.main()  # running again changes nothing
    assert new_browser().post("/api/auth/login", json={"email": "owner@novus.example", "password": START,
                                                        "force": True}).status_code == 200


def test_short_starting_password_creates_nobody(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAIL", "short@novus.example")
    monkeypatch.setenv("ADMIN_PASSWORD", "short")
    bootstrap.main()
    assert new_browser().post("/api/auth/login", json={"email": "short@novus.example", "password": "short"}).status_code == 401
