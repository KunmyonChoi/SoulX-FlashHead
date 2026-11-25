"""The server: text channel on /ws, optional modules, pages, end-of-session analysis.

Engines come from a factory with a scripted LLM, so nothing here needs the other modules or Ollama.
The concurrency test runs a real uvicorn on port 7881 and stops it afterwards.
"""
import asyncio
import json
import socket
import sys
import threading
import time
import types as pytypes

import pytest

import runtime_fakes  # noqa: F401  (sets AGENTKIT_DATA_DIR before agentkit.config is read)
from fastapi.testclient import TestClient

from agentkit import config, server
from agentkit.contracts import ToolResult
from agentkit.runtime import Engine
from runtime_fakes import (OPENING_WORDS, SHOP_CONTRACTS, FakeGateway, FakeRecorder, MiniCafe, ScriptedLLM,
                           make_cafe_definition, make_definition, text, tool)

PORT = 7881


def echo(messages, tools):
    users = [m["content"] for m in messages if m["role"] == "user"]
    return [text(f"'{users[-1]}'라고 하셨네요. "), 0.05, text(f"{len(users)}번째 말씀입니다.")]


class Factory:
    """Stands in for runtime.create_engine."""

    def __init__(self, scripts=None, responder=None):
        self.scripts, self.responder = scripts, responder
        self.recorder = FakeRecorder()
        self.gateway = FakeGateway(SHOP_CONTRACTS, {
            "get_order": lambda ctx, a: ToolResult(ok=True, text="주문 A1001: 무선 이어폰 30000원, 배송 중."),
            "handoff_to_human": lambda ctx, a: ToolResult(ok=True, executed=True, text="접수")})
        self.engines = []
        self.options = []

    async def __call__(self, agent_id, **options):
        if agent_id == "missing":
            raise RuntimeError("에이전트 missing이 없습니다")
        self.options.append(options)
        llm = ScriptedLLM(self.scripts, responder=self.responder)
        engine = Engine(make_definition(id=agent_id), self.gateway, self.recorder, llm, channel=options["channel"],
                        test=options["test"], settings=options.get("settings"))
        self.engines.append(engine)
        return engine


class CafeFactory(Factory):
    """Engines for an agent with an opening, a screen and touches (docs/DISPLAY.md)."""

    def __init__(self, scripts=None, responder=None):
        super().__init__(scripts, responder)
        self.gateway = MiniCafe()

    async def __call__(self, agent_id, **options):
        llm = ScriptedLLM(self.scripts, responder=self.responder)
        engine = Engine(make_cafe_definition(id=agent_id), self.gateway, self.recorder, llm,
                        channel=options["channel"], test=options["test"], settings=options.get("settings"))
        self.engines.append(engine)
        return engine


def until(ws, kind):
    """Messages up to and including the first of this type."""
    messages = []
    while True:
        messages.append(ws.receive_json())
        if messages[-1]["type"] == kind:
            return messages


def test_text_channel_protocol():
    factory = Factory([[tool("get_order", order_id="A1001")], [text("무선 이어폰은 지금 배송 중입니다.")],
                       [tool("handoff_to_human", reason="고객이 상담원을 바꿔 달라고 요구한다")],
                       [text("상담원에게 연결해 드릴게요.")]])
    with TestClient(server.create_app(factory)) as client:
        with client.websocket_connect("/ws?agent=shop&channel=text&test=1&length=normal") as ws:
            ready = ws.receive_json()
            assert ready["type"] == "ready" and ready["agent"] == "shop" and ready["channel"] == "text"
            # no screen, no touches, no opening: and so nothing is sent before the first chat
            assert (ready["screen"], ready["actions"], ready["opening"]) == ({"panels": []}, [], False)
            assert ready["version"] == 3 and ready["session_id"] == factory.engines[0].session_id
            assert client.get("/api/health").json()["sessions"] == 1

            ws.send_json({"type": "chat", "id": "m1", "text": "주문 A1001 어디쯤이에요?"})
            messages = until(ws, "chat_done")
            kinds = [m["type"] for m in messages]
            assert all(m["id"] == "m1" for m in messages)
            assert kinds.index("filler") < kinds.index("tool") < kinds.index("chat_delta")
            tools = [m for m in messages if m["type"] == "tool"]
            assert [m["state"] for m in tools] == ["start", "done"]
            assert tools[1]["name"] == "get_order" and tools[1]["effect"] == "read" and tools[1]["ok"] is True
            assert {"executed", "needs_confirmation", "error_type"} <= set(tools[1])
            assert [m["state"] for m in messages if m["type"] == "llm"] == ["start", "first_token", "start",
                                                                           "first_token", "done"]
            assert "".join(m["text"] for m in messages if m["type"] == "chat_delta") == "무선 이어폰은 지금 배송 중입니다."
            assert messages[-1]["text"] == "무선 이어폰은 지금 배송 중입니다."

            ws.send_json({"type": "chat", "id": "m2", "text": "상담원 바꿔 주세요"})
            messages = until(ws, "chat_done")
            handoff = next(m for m in messages if m["type"] == "handoff")
            assert handoff["rule_id"] == "angry" and "상담원" in handoff["reason"]
        # the socket is closed: the engine was closed with the right status
        deadline = time.time() + 2
        while not factory.recorder.ended and time.time() < deadline:
            time.sleep(0.01)
        assert factory.recorder.ended == [(factory.engines[0].session_id, "handoff")]
        assert client.get("/api/health").json()["sessions"] == 0
    options = factory.options[0]
    assert options["test"] is True and options["version"] is None and options["settings"] == {"reply_length": "normal"}
    assert [s.attrs["phase"] for s in factory.recorder.of("output")][:2] == ["filler", "reply"]
    assert [r.turn for r in factory.recorder.last_turns()] == [1, 2]


def test_refusals_and_version():
    factory = Factory(responder=echo)
    with TestClient(server.create_app(factory)) as client:
        for url, expected in (("/ws", "agent 값이 필요"), ("/ws?agent=missing", "시작하지 못했습니다"),
                              ("/ws?agent=shop&channel=fax", "알 수 없는 채널"),
                              ("/ws?agent=shop&version=two", "숫자가 아닙니다")):
            with client.websocket_connect(url) as ws:
                message = ws.receive_json()
                assert message["type"] == "error" and expected in message["message"], message
        with client.websocket_connect("/ws?agent=shop&version=2") as ws:
            assert ws.receive_json()["type"] == "ready"
            ws.send_text("not json")
            assert ws.receive_json()["type"] == "error"
            ws.send_json({"type": "chat", "id": "a", "text": "   "})         # empty: ignored
            ws.send_json({"type": "config", "language": "en", "length": "long"})
            ws.send_json({"type": "chat", "id": "b", "text": "hello"})
            messages = until(ws, "chat_done")
            assert messages[-1]["id"] == "b"
            assert "six or seven sentences" in factory.engines[-1].llm.system()
    assert factory.options[-1]["version"] == 2
    # connected and left without a turn / with one
    assert [status for _, status in factory.recorder.ended] == ["completed"]


def test_new_message_interrupts_the_reply_in_progress():
    slow = [[text("첫 번째 답을 길게 말하는 중입니다. "), 3.0, text("끝까지 가지 못할 문장.")], [text("두 번째 답입니다.")]]
    factory = Factory(slow)
    with TestClient(server.create_app(factory)) as client:
        with client.websocket_connect("/ws?agent=shop") as ws:
            ws.receive_json()
            ws.send_json({"type": "chat", "id": "1", "text": "첫 질문"})
            until(ws, "chat_delta")
            ws.send_json({"type": "chat", "id": "2", "text": "아니 다른 질문"})
            messages = until(ws, "chat_done")
            assert messages[-1]["id"] == "2" and messages[-1]["text"] == "두 번째 답입니다."
            assert not any(m["type"] == "chat_done" and m["id"] == "1" for m in messages)
    records = factory.recorder.last_turns()
    assert [(r.turn, r.interrupted) for r in records] == [(1, True), (2, False)]


def test_voice_channel_without_and_with_the_voice_module(monkeypatch):
    import agentkit.runtime as runtime
    factory = Factory(responder=echo)
    app = server.create_app(factory)

    monkeypatch.delattr(runtime, "voice", raising=False)
    monkeypatch.setitem(sys.modules, "agentkit.runtime.voice", None)        # importing it fails
    with TestClient(app) as client:
        with client.websocket_connect("/ws?agent=shop&channel=voice") as ws:
            message = ws.receive_json()
            assert message == {"type": "error", "message": "음성 채널이 설치되지 않았습니다."}
    assert factory.recorder.ended[-1][1] == "abandoned"

    served = []

    async def serve_voice(ws, engine, channel):
        served.append((engine.session_id, channel))
        await ws.send_json({"type": "ready", "session_id": engine.session_id, "channel": channel, "sample_rate": 16000})
        await ws.receive_text()

    fake = pytypes.ModuleType("agentkit.runtime.voice")
    fake.serve_voice = serve_voice
    monkeypatch.setitem(sys.modules, "agentkit.runtime.voice", fake)
    with TestClient(app) as client:
        with client.websocket_connect("/ws?agent=shop&channel=avatar") as ws:
            assert ws.receive_json()["sample_rate"] == 16000
            ws.send_text("bye")
    assert served == [(factory.engines[-1].session_id, "avatar")]
    assert factory.engines[-1].closed is True

    with_settings = []

    async def serve_voice4(ws, engine, channel, settings):       # PLAN.md's four-argument form
        with_settings.append(settings)
        await ws.send_json({"type": "ready"})

    fake.serve_voice = serve_voice4
    with TestClient(app) as client:
        with client.websocket_connect("/ws?agent=shop&channel=voice&stt=qwen") as ws:
            ws.receive_json()
    assert with_settings[0]["stt"] == "qwen" and with_settings[0]["agent"] == "shop"


def test_analysis_runs_in_the_background_when_the_session_ends(monkeypatch):
    analysed = []
    fake = pytypes.ModuleType("agentkit.eval")

    async def analyze_session(session_id):
        analysed.append(session_id)

    fake.analyze_session = analyze_session
    monkeypatch.setitem(sys.modules, "agentkit.eval", fake)
    factory = Factory(responder=echo)
    with TestClient(server.create_app(factory)) as client:
        with client.websocket_connect("/ws?agent=shop") as ws:
            ws.receive_json()
            ws.send_json({"type": "chat", "id": "1", "text": "안녕하세요"})
            until(ws, "chat_done")
        with client.websocket_connect("/ws?agent=shop") as ws:       # no turn: nothing to analyse
            ws.receive_json()
        deadline = time.time() + 3
        while not analysed and time.time() < deadline:
            time.sleep(0.02)
    assert analysed == [factory.engines[0].session_id]
    assert [status for _, status in factory.recorder.ended] == ["completed", "abandoned"]

    def broken(session_id):                                         # a failing, synchronous analysis is survived
        analysed.append("sync:" + session_id)
        raise RuntimeError("no model")

    fake.analyze_session = broken
    with TestClient(server.create_app(factory)) as client:
        with client.websocket_connect("/ws?agent=shop") as ws:
            ws.receive_json()
            ws.send_json({"type": "chat", "id": "1", "text": "안녕하세요"})
            until(ws, "chat_done")
        deadline = time.time() + 3
        while len(analysed) < 2 and time.time() < deadline:
            time.sleep(0.02)
        assert client.get("/api/health").status_code == 200
    assert analysed[-1] == "sync:" + factory.engines[-1].session_id


def test_server_starts_without_the_other_modules_and_mounts_those_present(monkeypatch):
    for name in server.ROUTER_MODULES:
        monkeypatch.setitem(sys.modules, name, None)                # none of them can be imported
    with TestClient(server.create_app(Factory(responder=echo))) as client:
        health = client.get("/api/health").json()
        assert health["ok"] and health["routers"] == [] and set(health["missing"]) == set(server.ROUTER_MODULES)
        with client.websocket_connect("/ws?agent=shop") as ws:
            assert ws.receive_json()["type"] == "ready"

    from fastapi import APIRouter
    router = APIRouter()

    @router.get("/api/tools")
    async def tools():
        return [{"name": "get_order"}]

    present = pytypes.ModuleType("agentkit.tools.api")
    present.router = router
    monkeypatch.setitem(sys.modules, "agentkit.tools.api", present)
    with TestClient(server.create_app(Factory(responder=echo))) as client:
        assert client.get("/api/tools").json() == [{"name": "get_order"}]
        assert client.get("/api/health").json()["routers"] == ["agentkit.tools.api"]


def test_pages(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "WEB_DIR", tmp_path)
    (tmp_path / "secret.txt").write_text("x")
    web = tmp_path / "web"
    web.mkdir()
    monkeypatch.setattr(config, "WEB_DIR", web)
    with TestClient(server.create_app(Factory(responder=echo))) as client:
        assert client.get("/").status_code == 404                    # no page yet: the server still runs
        (web / "index.html").write_text("<h1>대화</h1>")
        (web / "monitor.html").write_text("<h1>모니터</h1>")
        (web / "studio.html").write_text("<h1>제작실</h1>")
        (web / "app.js").write_text("console.log(1)")
        assert "대화" in client.get("/").text
        assert "모니터" in client.get("/monitor").text and "제작실" in client.get("/studio").text
        assert client.get("/web/app.js").text == "console.log(1)"
        assert client.get("/web/nothing.js").status_code == 404
        assert client.get("/web/%2e%2e/secret.txt").status_code == 404      # nothing outside web/


# ------------------------------------------------------------------------------------ screen, opening, touch

def test_text_channel_opening_display_and_action():
    factory = CafeFactory([[text("바닐라 라떼 담았어요.")],
                           [tool("add_to_cart", item_id="americano", quantity=2)], [text("아메리카노 두 잔 담았어요.")]])
    with TestClient(server.create_app(factory)) as client:
        with client.websocket_connect("/ws?agent=kiosk&channel=text&test=1") as ws:
            ready = ws.receive_json()
            assert ready["screen"] == {"panels": ["menu", "cart"]} and ready["opening"] is True
            assert ready["actions"][0] == {"name": "add_item", "tool": "add_to_cart", "description": "메뉴를 눌러 담는다"}
            # the opening comes by itself, under the id "opening"
            messages = until(ws, "chat_done")
            assert all(m["id"] == "opening" for m in messages)
            assert [m["type"] for m in messages] == ["tool", "tool", "tool", "display", "tool", "display",
                                                     "chat_delta", "chat_done"]
            menu = messages[3]
            assert set(menu) == {"type", "id", "kind", "data", "source"}
            assert menu["kind"] == "menu" and menu["source"] == "get_menu" and menu["data"]["categories"]
            assert messages[5]["kind"] == "cart" and messages[5]["data"]["lines"] == []
            assert messages[-2]["text"] == OPENING_WORDS and messages[-1]["text"] == OPENING_WORDS
            assert messages[-1]["turn"] == 0

            # a touch: carried out, shown, and answered like a chat turn
            ws.send_json({"type": "action", "id": "a1", "name": "add_item",
                          "arguments": {"item_id": "vanilla_latte", "quantity": 1}})
            messages = until(ws, "chat_done")
            kinds = [m["type"] for m in messages]
            assert all(m["id"] == "a1" for m in messages)
            assert kinds[:3] == ["tool", "tool", "display"] and "chat_delta" in kinds
            assert messages[1]["executed"] is True and messages[2]["kind"] == "cart"
            assert messages[2]["data"]["total"] == 5500
            assert messages[-1]["text"] == "바닐라 라떼 담았어요." and messages[-1]["turn"] == 1

            # touches that are refused: an error on chat_done, nothing else
            for name in ("pay", "nothing"):
                ws.send_json({"type": "action", "id": "a2", "name": name, "arguments": {}})
                refused = ws.receive_json()
                assert refused["type"] == "chat_done" and refused["id"] == "a2" and refused["text"] == ""
                assert refused["error"]

            # a spoken (typed) order shows the cart too
            ws.send_json({"type": "chat", "id": "m1", "text": "아메리카노 두 잔 주세요"})
            messages = until(ws, "chat_done")
            display = next(m for m in messages if m["type"] == "display")
            assert display["kind"] == "cart" and display["data"]["total"] == 14500 and display["id"] == "m1"
    assert [name for _, name, _ in factory.gateway.calls] == ["get_menu", "get_cart", "add_to_cart", "add_to_cart"]
    recorder = factory.recorder
    assert [(s.turn, s.name) for s in recorder.of("action")] == [(1, "add_item")]
    assert [(s.turn, s.name) for s in recorder.of("display")] == [(0, "menu"), (0, "cart"), (1, "cart"), (2, "cart")]
    assert [r.turn for r in recorder.last_turns()] == [0, 1, 2]
    assert [(s.turn, s.attrs["phase"]) for s in recorder.of("output")][0] == (0, "reply")


async def test_opening_and_action_over_a_real_socket_on_port_7881():
    import uvicorn
    import websockets

    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", PORT)) == 0:
            pytest.skip(f"port {PORT} is in use")
    factory = CafeFactory(responder=lambda messages, tools: [text("네, 담았어요.")])
    uvicorn_server = uvicorn.Server(uvicorn.Config(server.create_app(factory), host="127.0.0.1", port=PORT,
                                                   log_level="warning"))
    thread = threading.Thread(target=uvicorn_server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 10
        while not uvicorn_server.started and time.time() < deadline:
            await asyncio.sleep(0.05)
        assert uvicorn_server.started

        async def turn(ws, request_id):
            messages = []
            while True:
                messages.append(json.loads(await asyncio.wait_for(ws.recv(), 10)))
                assert messages[-1].get("id") == request_id
                if messages[-1]["type"] == "chat_done":
                    return messages

        async with websockets.connect(f"ws://127.0.0.1:{PORT}/ws?agent=kiosk&channel=text") as ws:
            ready = json.loads(await ws.recv())
            assert ready["opening"] is True and ready["screen"]["panels"] == ["menu", "cart"]
            # sent before the opening has been read: it waits its turn and does not cancel the opening
            await ws.send(json.dumps({"type": "action", "id": "a1", "name": "add_item",
                                      "arguments": {"item_id": "americano", "quantity": 1}}))
            opening = await turn(ws, "opening")
            assert [m["kind"] for m in opening if m["type"] == "display"] == ["menu", "cart"]
            assert opening[-1]["text"] == OPENING_WORDS
            touched = await turn(ws, "a1")
            assert [m["kind"] for m in touched if m["type"] == "display"] == ["cart"]
            assert touched[-1]["text"] == "네, 담았어요." and touched[-1]["turn"] == 1
    finally:
        uvicorn_server.should_exit = True
        thread.join(timeout=10)
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", PORT)) != 0             # the test server is down again


# ------------------------------------------------------------------------------------ a real server

async def test_three_text_sessions_at_once_on_port_7881():
    import uvicorn
    import websockets

    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", PORT)) == 0:
            pytest.skip(f"port {PORT} is in use")
    factory = Factory(responder=echo)
    uvicorn_server = uvicorn.Server(uvicorn.Config(server.create_app(factory), host="127.0.0.1", port=PORT,
                                                   log_level="warning"))
    thread = threading.Thread(target=uvicorn_server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 10
        while not uvicorn_server.started and time.time() < deadline:
            await asyncio.sleep(0.05)
        assert uvicorn_server.started

        active = {"now": 0, "most": 0}

        async def converse(index):
            async with websockets.connect(f"ws://127.0.0.1:{PORT}/ws?agent=agent-{index}&channel=text") as ws:
                ready = json.loads(await ws.recv())
                assert ready["type"] == "ready" and ready["agent"] == f"agent-{index}"
                replies = []
                for turn in range(2):
                    await ws.send(json.dumps({"type": "chat", "id": f"{index}-{turn}", "text": f"{index}번 손님 {turn}"}))
                    active["now"] += 1
                    active["most"] = max(active["most"], active["now"])
                    while True:
                        message = json.loads(await asyncio.wait_for(ws.recv(), 10))
                        assert message.get("id") == f"{index}-{turn}"
                        if message["type"] == "chat_done":
                            replies.append(message["text"])
                            break
                    active["now"] -= 1
                return ready["session_id"], replies

        results = await asyncio.gather(*(converse(i) for i in range(3)))
        assert active["most"] == 3                                    # the three replies overlapped
        assert len({session for session, _ in results}) == 3
        for index, (_, replies) in enumerate(results):
            assert replies == [f"'{index}번 손님 {t}'라고 하셨네요. {t + 1}번째 말씀입니다." for t in range(2)]
        deadline = time.time() + 3
        while len(factory.recorder.ended) < 3 and time.time() < deadline:
            await asyncio.sleep(0.05)
        assert sorted(status for _, status in factory.recorder.ended) == ["completed"] * 3
    finally:
        uvicorn_server.should_exit = True
        thread.join(timeout=10)
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", PORT)) != 0             # the test server is down again
