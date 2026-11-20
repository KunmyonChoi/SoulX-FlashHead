"""Owner login (agentkit/auth.py): off until a password is set, then pages, APIs and the WebSocket need it."""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import runtime_fakes  # noqa: F401

from agentkit import auth


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTKIT_SECRETS_DIR", str(tmp_path / "s"))
    from fastapi import FastAPI, WebSocket
    from fastapi.responses import HTMLResponse
    app = FastAPI()
    app.add_middleware(auth.OwnerOnly)
    auth.add_routes(app)

    @app.get("/api/health")
    async def health():
        return {"ok": True}

    @app.get("/api/agents")
    async def agents():
        return []

    @app.get("/monitor")
    async def monitor():
        return HTMLResponse("monitor")

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        await socket.send_text("ready")
        await socket.close()
    return app


def test_off_until_a_password_is_set(app):
    client = TestClient(app)
    assert client.get("/api/agents").status_code == 200
    assert auth.owner_required() is not None


def test_on_after_a_password_is_set(app):
    auth.set_password("correct horse battery")
    client = TestClient(app)
    assert client.get("/api/health").status_code == 200                       # stays open
    assert client.get("/api/agents").status_code == 401
    page = client.get("/monitor?x=1", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/login?next=%2Fmonitor%3Fx%3D1"
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws") as socket:
            socket.receive_text()
    assert client.post("/api/login", json={"password": "wrong password"}).status_code == 401
    ok = client.post("/api/login", json={"password": "correct horse battery"})
    assert ok.status_code == 200 and "httponly" in ok.headers["set-cookie"].lower() and "samesite=strict" in ok.headers["set-cookie"].lower()
    assert client.get("/api/agents").status_code == 200
    with client.websocket_connect("/ws") as socket:
        assert socket.receive_text() == "ready"
    assert auth.owner_required() is None


def test_service_token_and_forged_or_expired_cookies(app):
    auth.set_password("correct horse battery")
    client = TestClient(app)
    assert client.get("/api/agents", headers={"X-Agentkit-Service": auth.service_token()}).status_code == 200
    assert client.get("/api/agents", headers={"X-Agentkit-Service": "guess"}).status_code == 401
    assert client.get("/api/agents", cookies={auth.COOKIE: "9999999999.deadbeef"}).status_code == 401
    assert not auth.cookie_ok(auth.make_cookie(now=0))                          # expired long ago
    assert auth.cookie_ok(auth.make_cookie())


def test_short_password_is_refused(app):
    with pytest.raises(ValueError):
        auth.set_password("short")


def test_a_refused_websocket_is_closed_with_4401_after_the_handshake(app):
    auth.set_password("correct horse battery")
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect("/ws") as socket:      # the handshake is accepted ...
            socket.receive_text()
    assert closed.value.code == 4401                         # ... so the page sees 4401 and goes to /login


async def test_parallel_wrong_passwords_are_tried_one_at_a_time(app, monkeypatch):
    import asyncio
    import time as _time
    import httpx
    auth.set_password("correct horse battery")
    auth._failures.update(count=0, until=0.0)
    auth._login_lock = None
    tried = []
    real_check = auth.check_password
    monkeypatch.setattr(auth, "check_password", lambda pw: tried.append(pw) or real_check(pw))
    sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda s: sleep(0))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        answers = await asyncio.gather(*[client.post("/api/login", json={"password": f"guess {i}"}) for i in range(30)])
    assert len(tried) == 5                                   # then locked for a minute
    assert sum(a.status_code == 429 for a in answers) == 25
    auth._failures.update(count=0, until=0.0)


def test_the_login_page_only_returns_to_this_server():
    assert "u.origin === location.origin" in auth.LOGIN_PAGE and "/[\\\\]/.test(next)" in auth.LOGIN_PAGE
