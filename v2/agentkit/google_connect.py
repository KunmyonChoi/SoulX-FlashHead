"""Connecting the owner's Google account (OAuth 2.0 for a desktop-type client, with PKCE).

The user never types a Google password here: the browser goes to Google's own sign-in and consent page,
and Google sends back a one-time code, which is exchanged for a refresh token. That token is stored
encrypted (agentkit/secrets, entry "google") and used by agentkit/tools/google.py.

- The client file (client_secret_....json, "Desktop app" type, made by the user in Google Cloud Console)
  is stored encrypted too (entry "google_client"): upload it on /connect, or
  python -m agentkit.google_connect set-client <file>.
- Return address: http://localhost:<port>/oauth/google/callback. Google accepts loopback addresses for
  desktop clients; the user's browser reaches it through the SSH tunnel. The page must be opened as
  http://localhost:<port> for this to work.
- The return request comes from Google's site, so the SameSite=Strict login cookie is not sent with it.
  It is accepted only with the one-time `state` made when the logged-in owner started (10 minutes).
- Scopes: calendar.events (see and change events), gmail.modify (read, mark read; not permanent delete),
  gmail.compose (drafts and sending). Free; the app may stay unverified for one's own use.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets as _random
import sys
import time
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Body, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .auth import owner_required
from .secrets import get_store

router = APIRouter()
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
SCOPES = ["https://www.googleapis.com/auth/calendar.events",
          "https://www.googleapis.com/auth/gmail.modify",
          "https://www.googleapis.com/auth/gmail.compose"]
CALLBACK = "/oauth/google/callback"
_pending = {}      # state -> {"verifier", "redirect_uri", "at"}


def read_client(raw: dict) -> dict:
    """{client_id, client_secret} from a downloaded client file, or ValueError saying what is wrong."""
    section = raw.get("installed") or raw.get("web")
    if not section or not section.get("client_id"):
        raise ValueError("Google Cloud Console에서 내려받은 OAuth 클라이언트 파일(client_secret_….json)이 아닙니다")
    if "installed" not in raw:
        raise ValueError("'데스크톱 앱' 유형의 클라이언트가 필요합니다 (지금 파일은 웹 애플리케이션 유형입니다)")
    return {"client_id": section["client_id"], "client_secret": section.get("client_secret", "")}


def _status():
    store = get_store()
    names = {entry["name"]: entry for entry in store.names()}
    google = names.get("google")
    return {"owner_login": owner_required() is None,
            "google": {"client": "google_client" in names, "connected": bool(google),
                       "account": (google or {}).get("meta", {}).get("account"),
                       "scopes": (google or {}).get("meta", {}).get("scopes", []),
                       "connected_at": (google or {}).get("updated_at")}}


def _refuse_without_owner():
    problem = owner_required()
    return JSONResponse({"ok": False, "detail": problem}, status_code=409) if problem else None


@router.get("/api/connect")
async def connect_status():
    return _status()


@router.post("/api/connect/google/client")
async def set_client(body: dict = Body(default=None)):
    if (refused := _refuse_without_owner()) is not None:
        return refused
    try:
        client = read_client(body or {})
    except ValueError as e:
        return JSONResponse({"ok": False, "detail": str(e)}, status_code=400)
    get_store().put("google_client", client, kind="oauth_client", meta={"client_id": client["client_id"][:12] + "…"})
    return {"ok": True, **_status()}


@router.get("/api/connect/google/start")
async def start(request: Request):
    if (refused := _refuse_without_owner()) is not None:
        return refused
    client = get_store().get("google_client")
    if not client:
        return JSONResponse({"ok": False, "detail": "먼저 OAuth 클라이언트 파일을 올려 주세요"}, status_code=409)
    host = request.url.hostname or ""
    if host not in ("localhost", "127.0.0.1"):
        return JSONResponse({"ok": False, "detail": "Google 연결은 이 페이지를 http://localhost 주소로 열었을 때만 됩니다 "
                                                    "(SSH 터널로 접속하세요)"}, status_code=409)
    redirect_uri = f"http://{host}:{request.url.port or 80}{CALLBACK}"
    verifier = _random.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = _random.token_urlsafe(32)
    for old in [k for k, v in _pending.items() if v["at"] < time.time() - 600]:
        _pending.pop(old, None)
    _pending[state] = {"verifier": verifier, "redirect_uri": redirect_uri, "at": time.time()}
    url = AUTH_URL + "?" + urlencode({
        "client_id": client["client_id"], "redirect_uri": redirect_uri, "response_type": "code",
        "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true",
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
    return {"ok": True, "url": url}


def _page(title, text, ok=True):
    return HTMLResponse(f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<link rel="stylesheet" href="/web/app.css"></head><body><main style="max-width:520px;margin:12vh auto;padding:0 16px">
<h1 style="font-size:22px">{title}</h1><p>{text}</p><p><a href="/connect">계정 연결 화면으로</a></p></main></body></html>""",
                        status_code=200 if ok else 400)


@router.get(CALLBACK)
async def callback(state: str = "", code: str = "", error: str = ""):
    pending = _pending.pop(state, None)
    if not pending or pending["at"] < time.time() - 600:
        return _page("연결하지 못했습니다", "연결 요청이 없거나 10분이 지났습니다. 다시 시작해 주세요.", ok=False)
    if error or not code:
        return _page("연결하지 않았습니다", f"Google에서 동의하지 않았습니다 ({error or '코드 없음'}).", ok=False)
    client = get_store().get("google_client") or {}
    async with httpx.AsyncClient(timeout=15) as http:
        response = await http.post(TOKEN_URL, data={
            "code": code, "client_id": client.get("client_id"), "client_secret": client.get("client_secret", ""),
            "redirect_uri": pending["redirect_uri"], "grant_type": "authorization_code", "code_verifier": pending["verifier"]})
        if response.status_code != 200:
            return _page("연결하지 못했습니다", f"토큰을 받지 못했습니다 (HTTP {response.status_code}).", ok=False)
        token = response.json()
        if not token.get("refresh_token"):
            return _page("연결하지 못했습니다", "Google이 오래 쓸 수 있는 토큰을 주지 않았습니다. "
                                            "Google 계정의 '타사 연결'에서 이 앱을 지운 뒤 다시 시도해 주세요.", ok=False)
        account = None
        try:
            profile = await http.get(PROFILE_URL, headers={"Authorization": f"Bearer {token['access_token']}"})
            account = profile.json().get("emailAddress") if profile.status_code == 200 else None
        except httpx.HTTPError:
            pass
    granted = str(token.get("scope", "")).split()
    get_store().put("google", {"refresh_token": token["refresh_token"], "client_id": client.get("client_id"),
                               "client_secret": client.get("client_secret", "")},
                    kind="oauth", meta={"account": account, "scopes": granted})
    try:
        from .tools import google as google_tools
        google_tools._real["backend"] = None          # forget an access token of an earlier connection
    except Exception:
        pass
    missing = [s.rsplit("/", 1)[-1] for s in SCOPES if s not in granted]
    note = f" 다만 허용하지 않은 권한이 있습니다: {', '.join(missing)}." if missing else ""
    return _page("Google을 연결했습니다", f"{account or '계정'}을(를) 연결했습니다.{note} 이제 업무 비서가 일정과 메일을 다룰 수 있습니다.")


@router.post("/api/connect/google/disconnect")
async def disconnect():
    stopped = 0
    try:
        from .tools import google as google_tools
        stopped = google_tools.cancel_real_sends()      # before the token goes: a waiting mail must not go out after
    except Exception:
        pass
    store = get_store()
    saved = store.get("google")
    revoked = False
    if saved and saved.get("refresh_token"):
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                revoked = (await http.post(REVOKE_URL, data={"token": saved["refresh_token"]})).status_code == 200
        except httpx.HTTPError:
            pass
    store.delete("google")
    try:
        from .tools import google as google_tools
        google_tools._real["backend"] = None
    except Exception:
        pass
    return {"ok": True, "revoked": revoked, "stopped_sends": stopped, **_status()}


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) == 2 and argv[0] == "set-client":
        client = read_client(json.load(open(argv[1])))
        get_store().put("google_client", client, kind="oauth_client", meta={"client_id": client["client_id"][:12] + "…"})
        print("클라이언트를 암호화해 저장했습니다. 내려받은 파일은 지워도 됩니다.")
        return 0
    print("사용법: python -m agentkit.google_connect set-client <client_secret_….json>")
    return 2


if __name__ == "__main__":
    sys.exit(main())
