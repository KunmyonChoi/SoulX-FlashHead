"""The adapter kinds: python, http (against the dummy shop served on port 7895), replay, and the
v1 lookup tools. Tests that reach the internet or WeKnora are marked `network` and only run with
AGENTKIT_NETWORK_TESTS=1."""
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from agentkit import config
from agentkit.dummy.shop import get_shop
from agentkit.tools import AdapterUnavailable, Gateway, ToolFailure, build_gateway, record_call
from agentkit.tools import v1_lookup
from agentkit.tools.adapters import Adapter, PythonAdapter, ReplayAdapter, Reply
from agentkit.tools.testing import FakeRecorder, make_contract, make_ctx

PORT = 7895
BASE = f"http://127.0.0.1:{PORT}"
SHOP_TOOLS = ["verify_customer", "get_order", "track_delivery", "request_refund", "handoff_to_human"]
network = pytest.mark.skipif(os.environ.get("AGENTKIT_NETWORK_TESTS") != "1",
                             reason="needs the internet / WeKnora: set AGENTKIT_NETWORK_TESTS=1")


@pytest.fixture
def shop():
    shop = get_shop()
    shop.reset()
    shop.inject()
    yield shop
    shop.reset()
    shop.inject()


# ---- python ------------------------------------------------------------------------------------


async def echo(arguments, ctx):
    if arguments.get("key") == "missing":
        raise ToolFailure("not_found", "그런 항목은 없습니다.")
    if arguments.get("key") == "down":
        raise AdapterUnavailable("down")
    if arguments.get("key") == "dict":
        return {"ok": True, "text": "사전으로 답함", "data": {"n": 1}}
    return f"{ctx.session_id}: {arguments.get('key')}"


def sync_preview(arguments, ctx):
    return {"ok": True, "data": {"label": "미리보기 " + arguments["key"]}}


def python_gateway(**fields):
    contract = make_contract("t", **fields)
    adapter = PythonAdapter.__new__(PythonAdapter)
    Adapter.__init__(adapter, contract)     # skip the import by name: these functions live in a test file
    adapter.fn, adapter.preview_fn = echo, sync_preview
    return Gateway({"t": contract}, adapters={"t": adapter})


@pytest.mark.asyncio
async def test_python_adapter_return_conventions():
    gateway = python_gateway(retries=0)
    text = await gateway.call(make_ctx("p1"), "t", {"key": "hello"})
    assert text.ok and text.text == "p1: hello"
    wire = await gateway.call(make_ctx(), "t", {"key": "dict"})
    assert wire.ok and wire.text == "사전으로 답함" and wire.data == {"n": 1}
    failure = await gateway.call(make_ctx(), "t", {"key": "missing"})
    assert (failure.ok, failure.error_type, failure.text) == (False, "not_found", "그런 항목은 없습니다.")
    down = await gateway.call(make_ctx(), "t", {"key": "down"})
    assert down.error_type == "system_error"


@pytest.mark.asyncio
async def test_python_adapter_preview_fills_the_confirmation():
    gateway = python_gateway(effect="write", confirm_template="{label}을 진행할까요?")
    result = await gateway.call(make_ctx(), "t", {"key": "x"})
    assert result.needs_confirmation and result.confirmation_text == "미리보기 x을 진행할까요?"


def test_python_adapter_loads_module_colon_callable():
    contract = make_contract("t", adapter={"kind": "python", "target": "agentkit.tools.v1_lookup:get_weather"})
    assert PythonAdapter(contract).fn is v1_lookup.get_weather


# ---- http, against the dummy shop server ---------------------------------------------------------


def port_open(port):
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture(scope="module")
def shop_server():
    """python -m agentkit.dummy.shop_server --port 7895, stopped when the module's tests are done."""
    if port_open(PORT):
        pytest.skip(f"port {PORT} is already in use")
    process = subprocess.Popen([sys.executable, "-m", "agentkit.dummy.shop_server", "--port", str(PORT)],
                               cwd=str(config.V2_ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.time() + 20
        while not port_open(PORT):
            if process.poll() is not None or time.time() > deadline:
                raise RuntimeError("shop server did not start: " + process.stderr.read().decode()[-500:])
            time.sleep(0.1)
        yield BASE
    finally:
        process.terminate()
        try:
            process.wait(5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(5)
        assert not port_open(PORT)


def http_overrides(allow_in_test=True):
    return {name: {"kind": "http", "target": f"{BASE}/shop/{name}",
                   "options": {"preview_url": f"{BASE}/shop/{name}/preview", "allow_in_test": allow_in_test}}
            for name in SHOP_TOOLS}


@pytest.mark.asyncio
async def test_http_adapter_runs_the_whole_scenario_against_the_shop_server(shop_server, shop):
    recorder = FakeRecorder()
    gateway = build_gateway(recorder=recorder, adapter_overrides=http_overrides())
    session = f"http-{time.time_ns()}"
    refund = {"order_id": "A1001", "reason": "단순 변심"}

    early = await gateway.call(make_ctx(session, 1), "get_order", {"order_id": "A1001"})
    assert early.error_type == "precondition_failed"
    wrong = await gateway.call(make_ctx(session, 1), "verify_customer", {"order_id": "A1001", "phone_last4": "0000"})
    assert wrong.error_type == "permission_denied"
    assert (await gateway.call(make_ctx(session, 1), "verify_customer", {"order_id": "A9999", "phone_last4": "1234"})).error_type == "not_found"
    assert (await gateway.call(make_ctx(session, 1), "verify_customer", {"order_id": "A1001", "phone_last4": "1234"})).ok
    order = await gateway.call(make_ctx(session, 2), "get_order", {"order_id": "A1001"})
    assert order.ok and order.data["product_name"] == "무선 이어폰" and "32000원" in order.text
    assert (await gateway.call(make_ctx(session, 2), "get_order", {"order_id": "A1002"})).error_type == "permission_denied"
    assert (await gateway.call(make_ctx(session, 2), "track_delivery", {"order_id": "A1001"})).ok

    ask = await gateway.call(make_ctx(session, 3, "환불해 주세요"), "request_refund", refund)
    assert ask.needs_confirmation and ask.confirmation_text == "무선 이어폰 32000원 환불을 접수할까요?"
    same_turn = await gateway.call(make_ctx(session, 3, "환불해 주세요"), "request_refund", refund)
    assert same_turn.error_type == "not_confirmed"
    records = httpx.get(f"{BASE}/shop/records", params={"session_id": session}).json()["records"]
    assert records == []
    done = await gateway.call(make_ctx(session, 4, "네"), "request_refund", refund)
    assert done.executed and done.data["refund_id"] == "R1001"
    again = await gateway.call(make_ctx(session, 5, "네"), "request_refund", refund)
    assert again.ok and not again.executed
    handoff = await gateway.call(make_ctx(session, 5), "handoff_to_human", {"reason": "고객 요청"})
    assert handoff.executed

    records = httpx.get(f"{BASE}/shop/records", params={"session_id": session}).json()["records"]
    assert [r["kind"] for r in records] == ["refund", "handoff"] and records[0]["amount"] == 32000
    # it really went over HTTP: the in-process shop saw nothing
    assert shop.records() == []
    assert {span.attrs["adapter"] for span, _ in recorder.spans} == {"http"}


@pytest.mark.asyncio
async def test_http_large_refund_is_refused_by_the_server(shop_server):
    gateway = build_gateway(adapter_overrides=http_overrides())
    session = f"http-large-{time.time_ns()}"
    assert (await gateway.call(make_ctx(session, 1), "verify_customer", {"order_id": "A1002", "phone_last4": "5678"})).ok
    refused = await gateway.call(make_ctx(session, 2), "request_refund", {"order_id": "A1002", "reason": "변심"})
    assert refused.error_type == "precondition_failed" and "handoff_to_human" in refused.text


@pytest.mark.asyncio
async def test_http_server_outage_and_delay(shop_server):
    gateway = build_gateway(adapter_overrides=http_overrides())
    session = f"http-out-{time.time_ns()}"
    arguments = {"order_id": "A1001", "phone_last4": "1234"}
    try:
        httpx.post(f"{BASE}/shop/inject", json={"fail": "unavailable", "fail_times": 1})
        assert (await gateway.call(make_ctx(session, 1), "verify_customer", arguments)).ok      # 503, then the retry
        httpx.post(f"{BASE}/shop/inject", json={"fail": "unavailable"})
        down = await gateway.call(make_ctx(session, 2), "get_order", {"order_id": "A1001"})
        assert down.error_type == "system_error"
        httpx.post(f"{BASE}/shop/inject", json={"delay_s": 1.0})
        gateway._contracts["shop.get_order"].timeout_s = 0.2
        slow = await gateway.call(make_ctx(session, 3), "get_order", {"order_id": "A1001"})
        assert slow.error_type == "timeout"
    finally:
        httpx.post(f"{BASE}/shop/inject", json={})


@pytest.mark.asyncio
async def test_http_write_is_not_sent_from_a_test_session_unless_allowed(shop_server):
    gateway = build_gateway(adapter_overrides=http_overrides(allow_in_test=False))
    session = f"http-test-{time.time_ns()}"
    blocked = await gateway.call(make_ctx(session, 1, test=True), "handoff_to_human", {"reason": "x"})
    assert (blocked.ok, blocked.executed, blocked.error_type) == (False, False, "system_error")
    assert httpx.get(f"{BASE}/shop/records", params={"session_id": session}).json()["records"] == []
    live = await gateway.call(make_ctx(session + "-live", 1, test=False), "handoff_to_human", {"reason": "x"})
    assert live.executed


@pytest.mark.asyncio
async def test_http_connection_refused_is_system_error_and_a_write_stays_unexecuted():
    assert not port_open(7896)
    target = "http://127.0.0.1:7896/tool"
    read = make_contract("r", retries=1, adapter={"kind": "http", "target": target})
    write = make_contract("w", effect="write", adapter={"kind": "http", "target": target, "options": {"allow_in_test": True}})
    recorder = FakeRecorder()
    gateway = Gateway({"r": read, "w": write}, recorder=recorder)
    result = await gateway.call(make_ctx(), "r", {})
    assert (result.ok, result.error_type) == (False, "system_error")
    assert recorder.spans[-1][0].attrs["attempts"] == 2
    written = await gateway.call(make_ctx(), "w", {"key": "a"})
    assert (written.ok, written.executed, written.error_type) == (False, False, "system_error")
    assert recorder.spans[-1][0].attrs["attempts"] == 1


# ---- replay ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_gives_the_recorded_results_again(tmp_path, shop):
    """Record a conversation against the dummy shop, then answer the same calls from the file."""
    live = build_gateway()
    path = tmp_path / "shop_session.jsonl"
    refund = {"order_id": "A1001", "reason": "단순 변심"}
    steps = [(1, "", "verify_customer", {"order_id": "A1001", "phone_last4": "1234"}),
             (2, "", "get_order", {"order_id": "A1001"}),
             (2, "", "track_delivery", {"order_id": "A1001"}),
             (3, "환불해 주세요", "request_refund", refund),
             (4, "네", "request_refund", refund),
             (5, "", "handoff_to_human", {"reason": "고객 요청"})]
    recorded = []
    for turn, text, name, arguments in steps:
        result = await live.call(make_ctx("rec", turn, text), name, arguments)
        recorded.append(result)
        if result.needs_confirmation:       # first phase: what the system previewed
            record_call(path, name, arguments, {"ok": True, "data": result.data}, phase="preview")
        else:
            record_call(path, name, arguments, result)
    assert len(path.read_text(encoding="utf-8").splitlines()) == 6
    shop.reset()

    replay = build_gateway(adapter_overrides={name: {"kind": "replay", "target": str(path)} for name in SHOP_TOOLS})
    for (turn, text, name, arguments), expected in zip(steps, recorded):
        result = await replay.call(make_ctx("play", turn, text), name, arguments)
        assert (result.ok, result.text, result.data, result.error_type, result.executed, result.needs_confirmation,
                result.confirmation_text) == (expected.ok, expected.text, expected.data, expected.error_type,
                                              expected.executed, expected.needs_confirmation, expected.confirmation_text)
    assert shop.records() == []             # nothing touched the shop
    # the gateway's rules hold for a replayed system as well
    assert replay.facts("play") == {"verified"}
    assert (await replay.call(make_ctx("other", 1), "get_order", {"order_id": "A1001"})).error_type == "precondition_failed"


@pytest.mark.asyncio
async def test_replay_matching(tmp_path):
    path = tmp_path / "r.jsonl"
    record_call(path, "t", {"key": "a"}, Reply(True, "첫 번째"))
    record_call(path, "t", {"key": "a"}, Reply(True, "두 번째"))
    record_call(path, "t", {"key": "b"}, Reply(False, "없습니다.", None, "not_found"))
    record_call(path, "other_tool", {"key": "c"}, Reply(True, "다른 도구"))
    contract = make_contract("t", adapter={"kind": "replay", "target": str(path)})
    gateway = Gateway({"t": contract})
    texts = [(await gateway.call(make_ctx(), "t", {"key": "a"})).text for _ in range(3)]
    assert texts == ["첫 번째", "두 번째", "두 번째"]                      # in order, the last one repeats
    assert (await gateway.call(make_ctx("s2"), "t", {"key": "a"})).text == "첫 번째"     # per session
    assert (await gateway.call(make_ctx(), "t", {"key": "b"})).error_type == "not_found"
    unrecorded = await gateway.call(make_ctx(), "t", {"key": "c"})        # recorded for another tool only
    assert unrecorded.error_type == "system_error" and "기록" in unrecorded.text
    gateway.end_session("s1")
    assert (await gateway.call(make_ctx(), "t", {"key": "a"})).text == "첫 번째"


@pytest.mark.asyncio
async def test_replay_match_fields_and_relative_path(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TOOLS_DIR", tmp_path)
    (tmp_path / "replays").mkdir()
    (tmp_path / "replays" / "r.jsonl").write_text(
        json.dumps({"arguments": {"key": "a", "note": "x"}, "result": {"ok": True, "text": "맞음"}}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    contract = make_contract("t", input_schema={"type": "object", "properties": {"key": {"type": "string"}, "note": {"type": "string"}}},
                             adapter={"kind": "replay", "target": "replays/r.jsonl", "options": {"match_fields": ["key"]}})
    adapter = ReplayAdapter(contract)
    assert (await adapter.call({"key": "a", "note": "다른 말"}, make_ctx())).text == "맞음"
    assert not (await adapter.call({"key": "z"}, make_ctx())).ok


# ---- v1 lookup tools, offline ------------------------------------------------------------------

KNOWLEDGE = [   # WeKnora drops trailing zeros of the fraction: 1, 2 and 5 digits must all parse
    {"created_at": "2026-11-17T01:02:03.5Z", "title": "에이전트 평가 보고서", "description": "평가  방법을\n정리한 글"},
    {"created_at": "2026-11-16T23:30:00.12345+00:00", "file_name": "voice.pdf", "description": ""},
    {"created_at": "2026-11-15T03:00:00.25Z", "title": "오래된 자료", "description": "옛 자료"},
    {"created_at": "not a date", "title": "깨진 항목"},
]
SEOUL = ZoneInfo("Asia/Seoul")


def test_knowledge_listing_matches_v1_wording():
    latest = v1_lookup.describe_knowledge(KNOWLEDGE, "", SEOUL)
    assert latest.startswith("가장 최근에 추가된 자료 3건 (전체 3건 중):")
    assert "1. 제목: 에이전트 평가 보고서\n   추가 시각: 11월 17일 10시 2분\n   요약: 평가 방법을 정리한 글" in latest
    assert "2. 제목: voice.pdf\n   추가 시각: 11월 17일 8시 30분\n   요약: 요약 없음" in latest
    day = v1_lookup.describe_knowledge(KNOWLEDGE, "2026-11-17", SEOUL)
    assert day.startswith("11월 17일 화요일에 추가된 자료는 2건입니다.")       # 23:30 UTC is the 17th in Seoul
    none = v1_lookup.describe_knowledge(KNOWLEDGE, "2026-11-10", SEOUL)
    assert none == "11월 10일 화요일에 추가된 자료는 없습니다. 자료가 추가된 가까운 날짜: 11월 15일 일요일 1건, 11월 17일 화요일 2건."
    assert v1_lookup.describe_knowledge([], "", SEOUL) == "지식베이스에 자료가 없습니다."


@pytest.mark.asyncio
async def test_knowledge_tool_through_the_gateway_with_a_fake_weknora(monkeypatch, tmp_path):
    key_file = tmp_path / "key"
    key_file.write_text("secret-key\n")
    monkeypatch.setattr(config, "WEKNORA_KEY_FILE", str(key_file))
    monkeypatch.setattr(config, "WEKNORA_URL", "http://weknora.test/api/v1")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"data": KNOWLEDGE, "total": len(KNOWLEDGE)})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(v1_lookup.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    gateway = build_gateway()
    result = await gateway.call(make_ctx(), "list_knowledge_by_date", {"date": "2026-11-17"})
    assert result.ok and result.items == 2 and "에이전트 평가 보고서" in result.text
    request = seen[0]
    assert request.url.path == f"/api/v1/knowledge-bases/{v1_lookup.DEFAULT_KB_ID}/knowledge"
    assert request.headers["X-API-Key"] == "secret-key"
    assert dict(request.url.params) == {"page": "1", "page_size": "100", "sort_by": "created_at", "sort_order": "desc"}
    bad = await gateway.call(make_ctx(), "list_knowledge_by_date", {"date": "어제"})
    assert bad.error_type == "invalid_input" and "YYYY-MM-DD" in bad.text


@pytest.mark.asyncio
async def test_knowledge_tool_failures(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "WEKNORA_KEY_FILE", str(tmp_path / "no_such_key"))
    gateway = build_gateway()
    no_key = await gateway.call(make_ctx(), "list_knowledge_by_date", {"date": ""})
    assert no_key.error_type == "system_error"
    key_file = tmp_path / "key"
    key_file.write_text("k")
    monkeypatch.setattr(config, "WEKNORA_KEY_FILE", str(key_file))
    real_client = httpx.AsyncClient
    for status, expected in ((401, "permission_denied"), (404, "not_found"), (500, "system_error")):
        monkeypatch.setattr(v1_lookup.httpx, "AsyncClient", lambda status=status, **kw: real_client(
            transport=httpx.MockTransport(lambda request: httpx.Response(status)), **kw))
        result = await gateway.call(make_ctx(), "list_knowledge_by_date", {"date": ""})
        assert result.error_type == expected, status


@pytest.mark.asyncio
async def test_v1_tools_wrap_the_toolbox_and_classify_its_failures(monkeypatch):
    import xml.etree.ElementTree as ET
    realtime_tools = v1_lookup._v1()
    assert str(config.REPO_ROOT) in realtime_tools.__file__
    calls = []

    class FakeToolbox:
        async def get_weather(self, location, day_offset):
            calls.append(("get_weather", location, day_offset))
            if day_offset > 7:
                raise realtime_tools.ToolError("날씨는 오늘부터 7일 뒤까지만 조회할 수 있습니다.")
            if location == "아틀란티스":
                raise realtime_tools.ToolError("'아틀란티스' 지역을 찾지 못했습니다.")
            return "서울 오늘: 맑음, 최저 영상 3도, 최고 영상 11도."

        async def search_news(self, query, links=None):
            calls.append(("search_news", query))
            if links is not None:
                links.append(("첫 기사", "https://news.example/1"))
            if len([c for c in calls if c[0] == "search_news"]) == 1:
                raise httpx.ConnectError("refused")
            return "1. 첫 기사\n2. 둘째 기사"

        async def get_news_detail(self, topic, links=None):
            raise ET.ParseError("bad feed")

        async def search_geeknews(self, query):
            raise realtime_tools.ToolError("조회 실패: 날씨 서버가 응답하지 않습니다.")

        async def read_geeknews(self, title):
            return "글 요약 " + title

        async def search_wikipedia(self, query, language):
            calls.append(("search_wikipedia", query, language))
            return "이순신은 조선의 무신이다."

    monkeypatch.setattr(v1_lookup, "_toolbox", lambda: FakeToolbox())
    gateway = build_gateway()
    weather = await gateway.call(make_ctx(), "get_weather", {"location": "서울", "day_offset": "0"})
    assert weather.ok and "맑음" in weather.text and calls[0] == ("get_weather", "서울", 0)
    assert (await gateway.call(make_ctx(), "get_weather", {"location": "서울", "day_offset": 9})).error_type == "invalid_input"
    assert (await gateway.call(make_ctx(), "get_weather", {"location": "아틀란티스", "day_offset": 0})).error_type == "not_found"
    assert (await gateway.call(make_ctx(), "get_weather", {"location": "서울"})).error_type == "invalid_input"
    news = await gateway.call(make_ctx(), "search_news", {"query": "반도체"})          # ConnectError, then the retry
    assert news.ok and news.items == 2
    # the addresses go to the screen, not into the text; the model is told that they are shown
    assert news.data == {"links": [{"title": "첫 기사", "url": "https://news.example/1"}]}
    assert "https://" not in news.text.split("[화면에")[0] and "[화면에 링크 1개를 띄웠습니다" in news.text
    assert (await gateway.call(make_ctx(), "get_news_detail", {"topic": "x"})).error_type == "system_error"
    assert (await gateway.call(make_ctx(), "search_geeknews", {"query": ""})).error_type == "system_error"
    assert (await gateway.call(make_ctx(), "read_geeknews", {"title": "Rust"})).text == "글 요약 Rust"
    await gateway.call(make_ctx(language="en"), "search_wikipedia", {"query": "Yi Sun-sin"})
    assert calls[-1] == ("search_wikipedia", "Yi Sun-sin", "en")


# ---- v1 lookup tools, real sources ---------------------------------------------------------------


@network
@pytest.mark.network
@pytest.mark.asyncio
async def test_network_weather_and_wikipedia():
    gateway = build_gateway()
    weather = await gateway.call(make_ctx(), "get_weather", {"location": "서울", "day_offset": 0})
    assert weather.ok and "서울" in weather.text, weather.text
    wiki = await gateway.call(make_ctx(), "search_wikipedia", {"query": "이순신"})
    assert wiki.ok and "이순신" in wiki.text, wiki.text
    nowhere = await gateway.call(make_ctx(), "get_weather", {"location": "쿠쿠루삥뽕마을", "day_offset": 0})
    assert not nowhere.ok and nowhere.error_type in ("not_found", "system_error", "timeout")


@network
@pytest.mark.network
@pytest.mark.asyncio
async def test_network_news_and_geeknews():
    gateway = build_gateway()
    news = await gateway.call(make_ctx(), "search_news", {"query": ""})
    assert news.ok and news.items >= 1, news.text
    geek = await gateway.call(make_ctx(), "search_geeknews", {"query": ""})
    assert geek.ok and geek.items >= 1, geek.text


@network
@pytest.mark.network
@pytest.mark.asyncio
async def test_network_knowledge_listing_from_weknora():
    gateway = build_gateway()
    latest = await gateway.call(make_ctx(), "list_knowledge_by_date", {"date": ""})
    assert latest.ok, latest.text
    assert latest.text.startswith("가장 최근에 추가된 자료") or latest.text == "지식베이스에 자료가 없습니다."
    today = datetime.now(SEOUL).strftime("%Y-%m-%d")
    day = await gateway.call(make_ctx(), "list_knowledge_by_date", {"date": today})
    assert day.ok and "추가된 자료는" in day.text
