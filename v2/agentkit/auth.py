"""Owner login for the v2 server.

Once the assistant can act as the user (their Google account, their logged-in sites), anyone who reaches
the server could do the same. So every page, API, WebSocket and the live feed needs the owner's login.

- One owner. The password is kept only as a scrypt hash in ~/.agentkit/owner.json (AGENTKIT_SECRETS_DIR),
  set with:  python -m agentkit.auth set-password
- A login gives a signed cookie (HttpOnly, SameSite=Strict, 7 days). Its key is ~/.agentkit/session.key, so a
  restart does not log anyone out; deleting that file logs everyone out.
- Programs on this machine (the browser service, test scripts) send the X-Agentkit-Service header with the
  token in ~/.agentkit/service.token.
- Until an owner password is set the check is off (as before), and features that use stored accounts refuse
  to run (owner_required()).
- Open without login: /login, /api/login, /api/health, and the stylesheet the login page uses.
"""
from __future__ import annotations

import getpass
import hashlib
import hmac
import json
import os
import secrets as _random
import sys
import time
from pathlib import Path

from .secrets import DEFAULT_DIR, _write_private

COOKIE = "agentkit_session"
SESSION_S = 7 * 24 * 3600
# /oauth/google/callback: Google's redirect carries no SameSite=Strict cookie; it is checked by its one-time
# state instead (agentkit/google_connect.py)
OPEN_PATHS = {"/login", "/api/login", "/api/logout", "/api/health", "/web/app.css", "/favicon.ico",
              "/oauth/google/callback"}
SERVICE_HEADER = "x-agentkit-service"


def _dir() -> Path:
    return Path(os.environ.get("AGENTKIT_SECRETS_DIR", DEFAULT_DIR))


def _owner_file() -> Path:
    return _dir() / "owner.json"


def _private_bytes(name: str, make) -> bytes:
    path = _dir() / name
    if not path.exists():
        _write_private(path, make())
    return path.read_bytes().strip()


def session_key() -> bytes:
    return _private_bytes("session.key", lambda: _random.token_bytes(32).hex().encode() + b"\n")


def service_token() -> str:
    return _private_bytes("service.token", lambda: _random.token_urlsafe(32).encode() + b"\n").decode()


# ---- the password ------------------------------------------------------------------------------
def _hash(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32).hex()


def enabled() -> bool:
    return _owner_file().exists()


def set_password(password: str) -> None:
    if len(password) < 8:
        raise ValueError("8자 이상으로 정해 주세요")
    salt = _random.token_bytes(16)
    _write_private(_owner_file(), json.dumps({"salt": salt.hex(), "hash": _hash(password, salt),
                                               "set_at": time.time()}).encode())


def check_password(password: str) -> bool:
    try:
        data = json.loads(_owner_file().read_text())
    except (OSError, ValueError):
        return False
    return hmac.compare_digest(_hash(password or "", bytes.fromhex(data["salt"])), data["hash"])


# ---- the cookie --------------------------------------------------------------------------------
def make_cookie(now: float | None = None) -> str:
    expires = int((now if now is not None else time.time()) + SESSION_S)
    mac = hmac.new(session_key(), str(expires).encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{mac}"


def cookie_ok(value: str | None, now: float | None = None) -> bool:
    try:
        expires, mac = (value or "").split(".", 1)
        good = hmac.new(session_key(), expires.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(mac, good) and int(expires) > (now if now is not None else time.time())
    except (ValueError, OSError):
        return False


def _cookies(scope) -> dict:
    for name, value in scope.get("headers") or []:
        if name == b"cookie":
            out = {}
            for part in value.decode("latin-1").split(";"):
                if "=" in part:
                    k, v = part.strip().split("=", 1)
                    out[k] = v
            return out
    return {}


def _header(scope, wanted: bytes) -> str:
    for name, value in scope.get("headers") or []:
        if name == wanted:
            return value.decode("latin-1")
    return ""


def allowed(scope) -> bool:
    """Whether this request may go through."""
    if not enabled() or scope.get("path") in OPEN_PATHS:
        return True
    token = _header(scope, SERVICE_HEADER.encode())
    if token and hmac.compare_digest(token, service_token()):
        return True
    return cookie_ok(_cookies(scope).get(COOKIE))


class OwnerOnly:
    """ASGI middleware: pages redirect to /login, APIs answer 401, WebSockets are closed with 4401."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or allowed(scope):
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            await receive()                                   # websocket.connect
            # accept first: a close before the handshake is turned into an HTTP 403 by the server, and the page
            # then sees 1006 instead of 4401 and can't send the user to /login (review 2026-11-20)
            await send({"type": "websocket.accept"})
            await send({"type": "websocket.close", "code": 4401, "reason": "login_required"})
            return
        path = scope.get("path") or "/"
        if path.startswith("/api/"):
            body = json.dumps({"error": "login_required", "detail": "로그인이 필요합니다"}).encode()
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        query = scope.get("query_string", b"").decode()
        target = path + ("?" + query if query else "")
        from urllib.parse import quote
        await send({"type": "http.response.start", "status": 303,
                    "headers": [(b"location", ("/login?next=" + quote(target, safe="")).encode()), (b"content-length", b"0")]})
        await send({"type": "http.response.body", "body": b""})


def owner_required() -> str | None:
    """None if features that act as the user may run; otherwise why not (to show the user)."""
    if not enabled():
        return "서버에 소유자 비밀번호가 없습니다. 먼저 `python -m agentkit.auth set-password`로 정해 주세요."
    return None


# ---- routes ------------------------------------------------------------------------------------
LOGIN_PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>로그인 · 음성 에이전트</title><link rel="stylesheet" href="/web/app.css"></head>
<body><main style="max-width:360px;margin:12vh auto;padding:0 16px">
<h1 style="font-size:22px">음성 에이전트</h1>
<p class="muted">이 서버의 소유자 비밀번호를 입력하세요.</p>
<form id="f"><label for="pw">비밀번호</label>
<input id="pw" type="password" autocomplete="current-password" required style="width:100%;margin:6px 0 12px">
<button type="submit">로그인</button></form><p id="msg" role="alert"></p>
<script>
document.getElementById('f').onsubmit = async e => {
  e.preventDefault();
  const r = await fetch('/api/login', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({password: document.getElementById('pw').value})});
  if (r.ok) { const next = new URLSearchParams(location.search).get('next') || '/';
    // only a page of this server: "/\\evil.example" is read by browsers as another site (review 2026-11-20)
    let to = '/';
    try { const u = new URL(next, location.origin); if (u.origin === location.origin && !/[\\\\]/.test(next)) to = u.pathname + u.search + u.hash; } catch (err) {}
    location.href = to; }
  else { document.getElementById('msg').textContent = (await r.json()).detail || '로그인하지 못했습니다'; }
};
</script></main></body></html>"""

_failures = {"count": 0, "until": 0.0}
_login_lock = None      # one password check at a time: parallel tries all passed the lockout check (review 2026-11-20)


def add_routes(app):
    from fastapi import Body
    from fastapi.responses import HTMLResponse, JSONResponse

    @app.get("/login")
    async def login_page():
        return HTMLResponse(LOGIN_PAGE)

    @app.post("/api/login")
    async def login(body: dict = Body(default=None)):
        import asyncio
        global _login_lock
        if not enabled():
            return JSONResponse({"ok": True, "detail": "로그인이 꺼져 있습니다"})
        if _login_lock is None:
            _login_lock = asyncio.Lock()
        async with _login_lock:          # the check, the wait after a wrong password and the count, one try at a time
            if time.time() < _failures["until"]:
                return JSONResponse({"ok": False, "detail": "잠시 뒤에 다시 시도하세요"}, status_code=429)
            if not check_password(str((body or {}).get("password") or "")):
                _failures["count"] += 1
                if _failures["count"] >= 5:
                    _failures.update(count=0, until=time.time() + 60)
                await asyncio.sleep(1.0)
                return JSONResponse({"ok": False, "detail": "비밀번호가 맞지 않습니다"}, status_code=401)
            _failures["count"] = 0
        response = JSONResponse({"ok": True})
        response.set_cookie(COOKIE, make_cookie(), max_age=SESSION_S, httponly=True, samesite="strict", path="/")
        return response

    @app.post("/api/logout")
    async def logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        return response


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if argv[:1] == ["set-password"]:
        first = getpass.getpass("새 소유자 비밀번호: ")
        if getpass.getpass("한 번 더: ") != first:
            print("두 번 입력한 비밀번호가 다릅니다."); return 1
        set_password(first)
        print(f"저장했습니다 ({_owner_file()}, 해시만 저장). 서버를 다시 시작할 필요는 없습니다.")
        service_token()
        return 0
    if argv[:1] == ["status"]:
        print("로그인:", "켜짐" if enabled() else "꺼짐 (소유자 비밀번호 없음)")
        return 0
    print("사용법: python -m agentkit.auth set-password | status"); return 2


if __name__ == "__main__":
    sys.exit(main())
