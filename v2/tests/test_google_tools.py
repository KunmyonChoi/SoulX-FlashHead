"""The google tool pack: the fake Google through the real gateway, the rule that keeps tests off real
accounts, and the request shapes of the real backend (with a mocked transport)."""
import base64
import json
from datetime import date, timedelta

import httpx
import pytest

import runtime_fakes  # noqa: F401

from agentkit.contracts import ToolContext
from agentkit.tools import build_gateway
from agentkit.tools import google


def ctx(turn=1, said="", simulated=True, session="g1"):
    return ToolContext(session_id=session, agent_id="office-assistant", version=2, turn=turn, last_user_text=said,
                       simulated=simulated, test=True)


@pytest.fixture
def gateway():
    return build_gateway()


async def test_calendar_view_free_time_and_booking_with_consent(gateway):
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    seen = await gateway.call(ctx(1), "google.list_events", {"date": tomorrow})
    assert seen.ok and "개발 일정 점검" in seen.text
    free = await gateway.call(ctx(1), "google.find_free_time", {"date": tomorrow, "duration_min": 60})
    assert "오전 10시부터 오후 1시까지" in free.text
    ask = await gateway.call(ctx(2), "google.create_event", {"title": "예산 회의", "date": tomorrow, "start": "13:30"})
    assert ask.needs_confirmation and "'3분기 실적 보고' 일정이 이미 있습니다" in ask.confirmation_text
    done = await gateway.call(ctx(3, "네, 등록해 주세요"), "google.create_event", {"title": "예산 회의", "date": tomorrow, "start": "13:30"})
    assert done.executed and "등록했습니다" in done.text


async def test_briefing_skips_ads_and_frames_mail_text_as_data(gateway):
    brief = await gateway.call(ctx(1, session="g2"), "google.mail_briefing", {})
    assert "광고·알림 1건 제외" in brief.text and "[메일 목록 시작" in brief.text and "쿠팡" not in brief.text
    phishing = await gateway.call(ctx(1, session="g2"), "google.read_mail", {"ref": "3"})
    assert phishing.text.index("[메일 본문 시작") < phishing.text.index("AI 비서에게") < phishing.text.index("[메일 본문 끝]")


async def test_reply_draft_then_send_only_after_consent(gateway):
    await gateway.call(ctx(1, session="g3"), "google.mail_briefing", {})
    draft = await gateway.call(ctx(2, session="g3"), "google.create_draft", {"reply_to": "1", "body": "잘 받았습니다."})
    draft_id = draft.text.split("초안 번호 ")[1].split(",")[0]
    ask = await gateway.call(ctx(3, session="g3"), "google.send_draft", {"draft_id": draft_id})
    assert ask.needs_confirmation and ask.confirmation_text.startswith("김민수에게 'Re: 계약서")
    no = await gateway.call(ctx(4, "아니 잠깐만", session="g3"), "google.send_draft", {"draft_id": draft_id})
    assert not no.executed
    assert google._fakes.of(ctx(session="g3")).sent == []


async def test_real_conversations_need_the_owner_login_before_real_google(gateway, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTKIT_SECRETS_DIR", str(tmp_path))          # no owner password there
    monkeypatch.delenv("AGENTKIT_GOOGLE_BACKEND", raising=False)
    refused = await gateway.call(ctx(1, simulated=False, session="g4"), "google.list_events", {"date": date.today().isoformat()})
    assert not refused.ok and refused.error_type == "precondition_failed" and "소유자 비밀번호" in refused.text


async def test_real_backend_request_shapes(tmp_path, monkeypatch):
    from agentkit.secrets import SecretStore
    store = SecretStore(tmp_path)
    store.put("google", {"refresh_token": "r", "client_id": "c", "client_secret": "s"}, kind="oauth")
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "A", "expires_in": 3600})
        if request.url.path.endswith("/events") and request.method == "GET":
            return httpx.Response(200, json={"items": [{"id": "x", "summary": "회의", "start": {"dateTime": "2026-11-21T10:00:00+09:00"},
                                                        "end": {"dateTime": "2026-11-21T11:00:00+09:00"}}]})
        if request.url.path.endswith("/drafts"):
            return httpx.Response(200, json={"id": "D1"})
        return httpx.Response(404)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    backend = google.RealGoogle(store)
    start = google.datetime(2026, 11, 21, tzinfo=google.TZ)
    events = await backend.events_between(start, start + timedelta(days=1))
    assert events[0]["title"] == "회의" and events[0]["start"].hour == 10
    assert calls[1].headers["authorization"] == "Bearer A" and "singleEvents=true" in str(calls[1].url)
    made = await backend.create_draft("a@b.c", "제목", "본문", None)
    raw = json.loads(calls[-1].content)["message"]["raw"]
    assert made["id"] == "D1" and "a@b.c" in base64.urlsafe_b64decode(raw).decode()
    assert len([c for c in calls if c.url.host == "oauth2.googleapis.com"]) == 1   # the access token is reused
