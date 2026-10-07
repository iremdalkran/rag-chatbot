from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import login_client, make_user, register


def test_first_user_becomes_admin_and_registration_closes(client):
    assert client.get("/api/health").json()["setup_required"] is True
    r = register(client)
    assert r.status_code == 200
    assert r.json()["is_admin"] is True

    other = TestClient(app)
    r = register(other, name="Mehmet", email="mehmet@firma.com")
    assert r.status_code == 403


def test_session_cookie_is_httponly_and_logout_invalidates(client):
    r = register(client)
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/api/auth/me").status_code == 200

    token = client.cookies.get("session")
    client.post("/api/auth/logout")
    stolen = TestClient(app)
    stolen.cookies.set("session", token)
    assert stolen.get("/api/auth/me").status_code == 401


def test_requires_login(client):
    for path in ("/api/chats", "/api/documents", "/api/datasets", "/api/auth/me"):
        assert client.get(path).status_code == 401


def test_weak_password_rejected(client):
    r = register(client, password="1234")
    assert r.status_code == 400


def test_login_lockout_after_failures(client):
    register(client)
    make_user(client, "Ali", "ali@firma.com")
    c = TestClient(app)
    for _ in range(5):
        assert c.post("/api/auth/login", json={"email": "ali@firma.com", "password": "yanlis-sifre"}).status_code == 401
    r = c.post("/api/auth/login", json={"email": "ali@firma.com", "password": "guclu-sifre-1"})
    assert r.status_code == 401
    assert "dakika" in r.json()["detail"]


def test_admin_only_endpoints(client):
    register(client)
    make_user(client, "Ali", "ali@firma.com")
    ali = login_client("ali@firma.com")
    assert ali.get("/api/admin/users").status_code == 403


def test_password_change_logs_out_other_sessions(client):
    register(client)
    make_user(client, "Ali", "ali@firma.com")
    first = login_client("ali@firma.com")
    second = login_client("ali@firma.com")
    r = first.post("/api/auth/password", json={"old_password": "guclu-sifre-1", "new_password": "yeni-sifre-123"})
    assert r.status_code == 200
    assert first.get("/api/auth/me").status_code == 200
    assert second.get("/api/auth/me").status_code == 401


def test_cross_origin_post_blocked(client):
    r = client.post("/api/auth/login", json={"email": "a@b.co", "password": "x"},
                    headers={"Origin": "http://kotu-site.com"})
    assert r.status_code == 403


def test_security_headers(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "script-src 'self'" in r.headers["content-security-policy"]
