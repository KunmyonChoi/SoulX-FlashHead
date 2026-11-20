"""Connecting Google (agentkit/google_connect.py) against a mocked Google."""
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import runtime_fakes  # noqa: F401

CLIENT = {"installed": {"client_id": "123-abc.apps.googleusercontent.com", "client_secret": "shh"}}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTKIT_SECRETS_DIR", str(tmp_path))
    import agentkit.secrets as secrets_module
    monkeypatch.setattr(secrets_module, "_default", secrets_module.SecretStore(tmp_path))
    from agentkit import auth, google_connect
    app = FastAPI()
    app.add_middleware(auth.OwnerOnly)
    auth.add_routes(app)
    app.include_router(google_connect.router)
    seen = []

    def google(request):
        seen.append(request)
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "A", "refresh_token": "R", "expires_in": 3600,
                                             "scope": " ".join(google_connect.SCOPES)})
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": "me@gmail.com"})
        if request.url.path == "/revoke":
            return httpx.Response(200)
        return httpx.Response(404)
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(google), **kw))
    return app, auth, google_connect, secrets_module, seen


def test_refuses_until_the_owner_login_exists(setup):
    app, auth, *_ = setup
    client = TestClient(app, base_url="http://localhost:7871")
    assert client.post("/api/connect/google/client", json=CLIENT).status_code == 409


def test_full_connect_and_disconnect(setup):
    app, auth, google_connect, secrets_module, seen = setup
    auth.set_password("owner password 1")
    client = TestClient(app, base_url="http://localhost:7871")
    assert client.post("/api/connect/google/client", json=CLIENT).status_code == 401           # needs the login
    client.post("/api/login", json={"password": "owner password 1"})
    assert client.post("/api/connect/google/client", json={"web": {"client_id": "x"}}).status_code == 400
    assert client.post("/api/connect/google/client", json=CLIENT).json()["google"]["client"]
    url = client.get("/api/connect/google/start").json()["url"]
    q = parse_qs(urlparse(url).query)
    assert q["redirect_uri"] == ["http://localhost:7871/oauth/google/callback"] and q["code_challenge_method"] == ["S256"]
    assert q["access_type"] == ["offline"] and "gmail.compose" in q["scope"][0]
    # Google's redirect: no cookie (SameSite=Strict), accepted by its state
    fresh = TestClient(app, base_url="http://localhost:7871")
    assert fresh.get("/oauth/google/callback", params={"state": "forged", "code": "c"}).status_code == 400
    done = fresh.get("/oauth/google/callback", params={"state": q["state"][0], "code": "c"})
    assert done.status_code == 200 and "me@gmail.com" in done.text
    token_request = next(r for r in seen if r.url.path == "/token")
    assert "code_verifier=" in token_request.content.decode()
    stored = secrets_module.get_store().get("google")
    assert stored["refresh_token"] == "R" and stored["client_id"].startswith("123-")
    status = client.get("/api/connect").json()
    assert status["google"]["connected"] and status["google"]["account"] == "me@gmail.com" and "R" not in json.dumps(status)
    assert fresh.get("/oauth/google/callback", params={"state": q["state"][0], "code": "c"}).status_code == 400   # once only
    off = client.post("/api/connect/google/disconnect").json()
    assert off["revoked"] and not off["google"]["connected"] and secrets_module.get_store().get("google") is None


def test_start_needs_a_localhost_address(setup):
    app, auth, google_connect, *_ = setup
    auth.set_password("owner password 1")
    client = TestClient(app, base_url="http://10.0.0.5:7871")
    client.post("/api/login", json={"password": "owner password 1"})
    client.post("/api/connect/google/client", json=CLIENT)
    assert client.get("/api/connect/google/start").status_code == 409
