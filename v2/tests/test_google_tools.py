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


async def test_list_mail_filters_counts_and_marks(gateway):
    listed = await gateway.call(ctx(1, session="g5"), "google.list_mail", {"filter": "unread"})
    data = listed.display[0]["data"]
    assert listed.display[0]["kind"] == "mail_list" and data["counts"] == {"unread": 3, "ads": 2, "all": 7, "reply": 2}
    assert [(r["ref"], r["from"], r["marks"]) for r in data["rows"]] == [
        (1, "김민수", ["reply", "attachment"]), (2, "이서연", ["reply"]), (3, "보안팀", ["risk"])]
    assert "[메일 목록 시작" in listed.text
    reply = await gateway.call(ctx(1, session="g5"), "google.list_mail", {"filter": "reply"})
    assert [r["from"] for r in reply.display[0]["data"]["rows"]] == ["김민수", "이서연"]


async def test_cleanup_is_planned_then_run_with_consent_then_undone(gateway):
    s = "g6"
    await gateway.call(ctx(1, session=s), "google.list_mail", {"filter": "ads"})
    plan = await gateway.call(ctx(2, session=s), "google.cleanup_mail", {"spam": "1", "archive": "2"})
    assert plan.needs_confirmation and not plan.executed and "보관 1통, 스팸으로 이동 1통" in plan.confirmation_text
    fake = google._fakes.of(ctx(session=s))
    assert await fake.counts() == {"unread": 3, "ads": 2, "all": 7}                    # nothing changed yet
    done = await gateway.call(ctx(3, "응, 정리해", session=s), "google.cleanup_mail", {"spam": "1", "archive": "2"})
    report = done.display[0]["data"]
    assert done.executed and done.display[0]["kind"] == "cleanup_report" and report["done"] == 2
    assert "보관 1통, 스팸 1통" in done.text and await fake.counts() == {"unread": 3, "ads": 0, "all": 5}
    undo = await gateway.call(ctx(4, session=s), "google.undo_cleanup", {"report_id": report["report_id"], "refs": "1"})
    assert undo.executed and undo.display[0]["data"]["undone"] == 1 and await fake.counts() == {"unread": 3, "ads": 1, "all": 6}
    rest = await gateway.call(ctx(5, session=s), "google.undo_cleanup", {})
    assert rest.display[0]["data"]["undone"] == 2 and await fake.counts() == {"unread": 3, "ads": 2, "all": 7}
    again = await gateway.call(ctx(6, session=s), "google.undo_cleanup", {})
    assert not again.executed and "이미 모두" in again.text


async def test_cleanup_refuses_numbers_not_on_screen_or_twice(gateway):
    s = "g7"
    await gateway.call(ctx(1, session=s), "google.list_mail", {"filter": "ads"})
    missing = await gateway.call(ctx(2, session=s), "google.cleanup_mail", {"spam": "9"})
    assert not missing.ok and missing.error_type == "invalid_input"
    twice = await gateway.call(ctx(2, session=s), "google.cleanup_mail", {"spam": "1", "trash": "1"})
    assert not twice.ok and "두 가지" in twice.text


async def test_real_backend_cleanup_request_shapes(tmp_path, monkeypatch):
    from agentkit.secrets import SecretStore
    store = SecretStore(tmp_path)
    store.put("google", {"refresh_token": "r", "client_id": "c"}, kind="oauth")
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "A", "expires_in": 3600})
        return httpx.Response(200, json={})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    backend = google.RealGoogle(store)
    await backend.apply("spam", ["a", "b"])
    await backend.revert("spam", ["a"])
    await backend.apply("trash", ["c"])
    await backend.revert("trash", ["c"])
    bodies = [(c.url.path, json.loads(c.content) if c.content else None) for c in calls if c.url.host != "oauth2.googleapis.com"]
    assert bodies == [
        ("/gmail/v1/users/me/messages/batchModify", {"ids": ["a", "b"], "addLabelIds": ["SPAM"], "removeLabelIds": ["INBOX"]}),
        ("/gmail/v1/users/me/messages/batchModify", {"ids": ["a"], "addLabelIds": ["INBOX"], "removeLabelIds": ["SPAM"]}),
        ("/gmail/v1/users/me/messages/c/trash", None), ("/gmail/v1/users/me/messages/c/untrash", None)]


async def test_real_backend_retries_a_rate_limit_and_does_not_call_it_a_missing_permission(tmp_path, monkeypatch):
    from agentkit.secrets import SecretStore
    store = SecretStore(tmp_path)
    store.put("google", {"refresh_token": "r", "client_id": "c"}, kind="oauth")
    answers = iter([httpx.Response(403, json={"error": {"errors": [{"reason": "userRateLimitExceeded"}]}}),
                    httpx.Response(200, json={"messages": [{"id": "x"}]})])

    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "A", "expires_in": 3600})
        return next(answers)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    real_sleep = google.asyncio.sleep
    monkeypatch.setattr(google.asyncio, "sleep", lambda seconds: real_sleep(0))
    assert await google.RealGoogle(store)._ids("in:inbox", 10) == ["x"]


async def test_send_waits_and_can_be_cancelled_in_time(gateway, monkeypatch):
    import asyncio
    monkeypatch.setattr(google, "SEND_DELAY", 0.3)
    s = "g8"
    await gateway.call(ctx(1, session=s), "google.mail_briefing", {})
    args = {"reply_to": "1", "body": "잘 받았습니다."}
    await gateway.call(ctx(2, session=s), "google.send_mail", args)
    sent = await gateway.call(ctx(3, "보내", session=s), "google.send_mail", args)
    pending = sent.display[0]["data"]
    assert sent.executed and sent.display[0]["kind"] == "send_pending" and pending["state"] == "waiting" and "뒤에 보냅니다" in sent.text
    fake = google._fakes.of(ctx(session=s))
    stop = await gateway.call(ctx(4, session=s), "google.cancel_send", {})
    assert stop.executed and stop.display[0]["data"]["state"] == "cancelled" and "임시보관함" in stop.text
    await asyncio.sleep(0.5)
    assert fake.sent == [] and len(fake.drafts) == 1                       # never sent, the draft kept


async def test_send_goes_out_after_the_wait_and_cannot_be_cancelled_then(gateway, monkeypatch):
    import asyncio
    monkeypatch.setattr(google, "SEND_DELAY", 0.1)
    s = "g9"
    await gateway.call(ctx(1, session=s), "google.mail_briefing", {})
    args = {"reply_to": "2", "body": "4시 괜찮습니다."}
    await gateway.call(ctx(2, session=s), "google.send_mail", args)
    await gateway.call(ctx(3, "응 보내", session=s), "google.send_mail", args)
    await asyncio.sleep(0.4)
    fake = google._fakes.of(ctx(session=s))
    assert len(fake.sent) == 1 and fake.sent[0]["subject"].startswith("Re: 내일 회의")
    late = await gateway.call(ctx(4, session=s), "google.cancel_send", {"send_id": "s1"})
    assert not late.executed and "이미 보냈습니다" in late.text


async def test_mail_numbers_keep_their_meaning_when_the_list_changes(gateway):
    s = "g10"
    unread = await gateway.call(ctx(1, session=s), "google.list_mail", {"filter": "unread"})
    kim = next(r["ref"] for r in unread.display[0]["data"]["rows"] if r["from"] == "김민수")
    ads = await gateway.call(ctx(1, session=s), "google.list_mail", {"filter": "ads"})
    assert kim not in [r["ref"] for r in ads.display[0]["data"]["rows"]]       # the ad list gets numbers of its own
    ask = await gateway.call(ctx(2, session=s), "google.send_mail", {"reply_to": str(kim), "body": "잘 받았습니다."})
    assert ask.confirmation_text.startswith("김민수에게")                          # still 김민수, not the first ad
    again = await gateway.call(ctx(3, session=s), "google.list_mail", {"filter": "unread"})
    assert next(r["ref"] for r in again.display[0]["data"]["rows"] if r["from"] == "김민수") == kim


async def test_an_all_day_entry_does_not_block_free_time():
    from datetime import datetime as dt
    day = date.today() + timedelta(days=3)
    start = dt.combine(day, dt.min.time(), google.TZ)

    class Calendar:
        async def events_between(self, a, b):
            return [{"title": "생일", "start": start, "end": start + timedelta(days=1), "all_day": True},
                    {"title": "회의", "start": start.replace(hour=10), "end": start.replace(hour=11), "all_day": False}]
    slots = await google._free_slots(Calendar(), day, 30, (9, 0), (18, 0))
    assert [(a.hour, b.hour) for a, b in slots] == [(9, 10), (11, 18)]
    assert "하루 종일 '생일'" in google._event_line({"title": "생일", "start": start, "end": start + timedelta(days=1), "all_day": True})


def test_the_prompt_calendar_names_this_and_next_week():
    from datetime import datetime as dt
    from agentkit.runtime.prompt import calendar_text
    text = calendar_text(dt(2026, 11, 20, 17, 0, tzinfo=google.TZ))           # a Friday
    assert "11월 25일은 수요일(다음 주 수요일)" in text and "11월 29일은 일요일(다음 주 일요일)" in text
    assert "11월 20일은 금요일(오늘, 이번 주 금요일)" in text


async def test_sending_an_edited_card_leaves_no_older_proposal_behind(gateway, monkeypatch):
    monkeypatch.setattr(google, "SEND_DELAY", 0)
    s = "g11"
    await gateway.call(ctx(1, session=s), "google.list_mail", {"filter": "unread"})
    asked = await gateway.call(ctx(2, session=s), "google.send_mail", {"reply_to": "1", "body": "잘 받았습니다."})
    assert asked.needs_confirmation and len(gateway.pending(s)) == 1
    touch = ToolContext(session_id=s, agent_id="office-assistant", version=2, turn=3, last_user_text="", simulated=True,
                        test=True, touch_consent=True)
    sent = await gateway.call(touch, "google.send_mail", {"reply_to": "1", "body": "잘 받았습니다. 금요일까지 드리겠습니다."})
    assert sent.ok and not sent.needs_confirmation
    assert gateway.pending(s) == []              # the unedited wording can no longer be sent by a later "응"


async def test_a_failing_second_cleanup_group_leaves_the_first_undoable(gateway, monkeypatch):
    s = "g12"
    await gateway.call(ctx(1, session=s), "google.list_mail", {"filter": "all"})
    fake = google.backend(ctx(1, session=s))
    real_apply = fake.apply

    async def apply(action, ids):
        if action == "trash":
            raise google.ToolFailure("upstream", "Gmail 오류")
        return await real_apply(action, ids)
    monkeypatch.setattr(fake, "apply", apply)
    plan = {"spam": "4,5", "trash": "3"}
    await gateway.call(ctx(2, session=s), "google.cleanup_mail", plan)
    done = await gateway.call(ctx(3, said="응, 정리해", session=s), "google.cleanup_mail", plan)
    assert done.ok and "하지 못했습니다" in done.text
    undo = await gateway.call(ctx(4, said="되돌려 줘", session=s), "google.undo_cleanup", {})
    assert undo.ok and "2통" in undo.text           # the two mails moved to spam come back


async def test_disconnecting_stops_a_waiting_send_to_the_real_account(monkeypatch):
    import asyncio
    monkeypatch.setattr(google, "SEND_DELAY", 0.3)
    sent = []

    class Real(google.RealGoogle):
        def __init__(self):
            pass

        async def send_draft(self, draft_id):
            sent.append(draft_id)
    real = Real()
    result = await google._schedule(ctx(session="g13", simulated=False), real, {"id": "d1"}, "a@example.com", "제목")
    assert result
    assert google.cancel_real_sends() == 1
    await asyncio.sleep(0.5)
    assert sent == []


def test_only_lookalike_domains_are_marked_suspicious():
    mail = lambda address: {"from_email": address, "subject": "검토 부탁드립니다", "snippet": "", "unread": True, "category": "primary"}
    for address in ["kim@g2b.go.kr", "lee@k8s.io", "park@w3c.org", "a@b2b.co.kr"]:
        assert "risk" not in google.marks(mail(address)) and "reply" in google.marks(mail(address)), address
    for address in ["security-check@examp1e.com", "help@g00gle.com", "pay@paypa1.com", "x@xn--80ak6aa92e.com"]:
        assert "risk" in google.marks(mail(address)), address


async def test_address_lookup_runs_in_parallel_and_is_kept_for_the_consented_call():
    import asyncio
    calls, running, most = [], [0], [0]

    class Real(google.RealGoogle):
        def __init__(self):
            self._token = None

        async def _call(self, method, path, params=None, **kw):
            calls.append(path)
            if path.endswith("/messages"):
                return {"messages": [{"id": f"x{i}"} for i in range(10)]}
            running[0] += 1; most[0] = max(most[0], running[0])
            await asyncio.sleep(0.01)
            running[0] -= 1
            return {"payload": {"headers": [{"name": "From", "value": "김민수 <minsu@example.com>"}]}}
    real = Real()
    assert await real.find_email("김민수") == ["minsu@example.com"]
    assert len(calls) == 11 and 1 < most[0] <= 5
    assert await real.find_email("김민수") == ["minsu@example.com"] and len(calls) == 11    # no second round


def test_spoken_dates_are_worked_out_in_code():
    friday = date(2026, 11, 20)
    cases = {"다음 주 수요일": date(2026, 11, 25), "다음주수요일": date(2026, 11, 25), "이번 주 일요일": date(2026, 11, 22),
             "수요일": date(2026, 11, 25), "내일": date(2026, 11, 21), "모레": date(2026, 11, 22), "11월 25일": date(2026, 11, 25),
             "25일": date(2026, 11, 25), "3일": date(2026, 12, 3), "3일 뒤": date(2026, 11, 23), "다음 주": date(2026, 11, 23),
             "다다음 주 화요일": date(2026, 12, 1), "2026-12-01": date(2026, 12, 1)}
    for words, day in cases.items():
        assert google.day_from_words(words, friday) == day, words
    assert google.day_from_words("크리스마스", friday) is None
