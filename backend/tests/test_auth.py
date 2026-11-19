from conftest import PASSWORD, add_member, auth, signup


def test_health(client):
    assert client.get("/api/health").json()["status"] == "ok"


def test_signup_creates_org_and_admin(client):
    token = signup(client)
    me = client.get("/api/auth/me", headers=auth(token)).json()["user"]
    assert me["role"] == "admin"
    assert me["organization"]["name"] == "Acme"


def test_signup_duplicate_email_rejected(client):
    signup(client)
    res = client.post("/api/auth/signup", json={
        "organization_name": "Other", "name": "X", "email": "ADMIN@acme.com", "password": PASSWORD,
    })
    assert res.status_code == 409


def test_signup_validation_errors(client):
    res = client.post("/api/auth/signup", json={"organization_name": "", "name": "X", "email": "nope", "password": "short"})
    assert res.status_code == 422
    fields = {d["field"] for d in res.json()["error"]["details"]}
    assert {"organization_name", "email", "password"} <= fields


def test_login_success_and_wrong_password(client):
    signup(client)
    ok = client.post("/api/auth/login", json={"email": "admin@acme.com", "password": PASSWORD})
    assert ok.status_code == 200 and ok.json()["token"]

    bad = client.post("/api/auth/login", json={"email": "admin@acme.com", "password": "wrong-password"})
    assert bad.status_code == 401
    unknown = client.post("/api/auth/login", json={"email": "ghost@acme.com", "password": "wrong-password"})
    assert unknown.status_code == 401
    # Same message either way, so attackers can't discover which emails exist
    assert bad.json() == unknown.json()


def test_requests_without_token_are_rejected(client):
    assert client.get("/api/meetings").status_code == 401
    assert client.get("/api/meetings", headers=auth("garbage")).status_code == 401


def test_invite_flow(client, invites):
    admin = signup(client)
    member = add_member(client, admin, invites)
    me = client.get("/api/auth/me", headers=auth(member)).json()["user"]
    assert me["role"] == "member" and me["status"] == "active"

    # Invite tokens are single-use
    again = client.post("/api/auth/accept-invite", json={"token": invites["maya@acme.com"], "password": PASSWORD})
    assert again.status_code == 400


def test_invited_user_cannot_log_in_before_accepting(client, invites):
    admin = signup(client)
    client.post("/api/users/invite", json={"email": "new@acme.com"}, headers=auth(admin))
    res = client.post("/api/auth/login", json={"email": "new@acme.com", "password": PASSWORD})
    assert res.status_code == 401


def test_members_cannot_manage_users(client, invites):
    admin = signup(client)
    member = add_member(client, admin, invites)
    assert client.get("/api/users", headers=auth(member)).status_code == 403
    assert client.post("/api/users/invite", json={"email": "x@acme.com"}, headers=auth(member)).status_code == 403


def test_disabled_user_loses_access(client, invites):
    admin = signup(client)
    member = add_member(client, admin, invites)
    users = client.get("/api/users", headers=auth(admin)).json()["items"]
    maya = next(u for u in users if u["email"] == "maya@acme.com")

    res = client.patch(f"/api/users/{maya['id']}", json={"status": "disabled"}, headers=auth(admin))
    assert res.status_code == 200
    assert client.get("/api/auth/me", headers=auth(member)).status_code == 401


def test_login_is_rate_limited(settings):
    from fastapi.testclient import TestClient

    from meetmind import create_app
    from meetmind.db import Base

    settings.ratelimit_enabled = True
    settings.ratelimit_storage_uri = "memory://"
    app = create_app(settings)
    Base.metadata.create_all(app.state.engine)
    with TestClient(app) as c:
        body = {"email": "ghost@acme.com", "password": "wrong-password"}
        codes = [c.post("/api/auth/login", json=body).status_code for _ in range(11)]
    # login allows 10 attempts per minute per IP
    assert codes == [401] * 10 + [429]


def test_openapi_docs_available_outside_production(client):
    spec = client.get("/api/openapi.json").json()
    assert spec["info"]["title"] == "MeetMind API"
    assert "/api/meetings/{meeting_id}/recordings" in spec["paths"]
