"""REST rows of WP3 and the live stream. The SSE test runs a throwaway uvicorn server on a free
local port inside the test process and stops it at the end."""
import asyncio
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentkit import trace
from agentkit.contracts import SessionMeta, Span, TurnRecord
from agentkit.trace import api


@pytest.fixture
def env(tmp_path):
    trace.reset(tmp_path / "data")
    yield tmp_path / "data"
    trace.reset(None)


def make_app():
    app = FastAPI()
    app.include_router(api.router)
    return app


def record_session(sid="s1", test=True, status="completed", agent="shop-support"):
    rec = trace.get_recorder()
    t0 = time.time() - 30
    rec.start_session(SessionMeta(session_id=sid, agent_id=agent, version=1, channel="voice", test=test,
                                  started_at=t0, label="refund_small"))
    rec.span(Span(session_id=sid, turn=1, kind="tool", name="get_order", t_start=t0 + 1, t_end=t0 + 1.2,
                  input={"arguments": {"order_id": "A1001"}}, output={"ok": True, "text": "30000원 주문"},
                  attrs={"effect": "read"}))
    rec.turn(TurnRecord(session_id=sid, turn=1, user_text="주문번호는 A1001이에요", agent_text="30000원 주문입니다.",
                        t_user_end=t0 + 1, t_agent_start=t0 + 2.5, t_agent_end=t0 + 4))
    rec.audio(sid, 1, "user", b"\x00\x01" * 800)
    if status != "active":
        rec.end_session(sid, status)
    assert rec.flush(5.0)


def test_rest_rows(env):
    client = TestClient(make_app())
    assert client.get("/api/sessions").json() == {"sessions": []}
    record_session("s1", test=True)
    record_session("s2", test=False, status="active", agent="tech-trend-anchor")
    trace.get_store().put_document("analysis", "s1", {
        "session_id": "s1", "agent_id": "shop-support", "version": 1, "summary": "주문 조회",
        "criteria": [{"id": "fast", "passed": True, "safety": False}]})

    rows = client.get("/api/sessions").json()["sessions"]
    assert [r["session_id"] for r in rows] == ["s2", "s1"] or [r["session_id"] for r in rows] == ["s1", "s2"]
    s1 = next(r for r in rows if r["session_id"] == "s1")
    assert s1["turns"] == 1 and s1["response_ms_median"] == 1500.0 and s1["status"] == "completed"
    assert s1["analysis"]["passed"] == 1 and s1["analysis"]["total"] == 1 and s1["analysis"]["safety_failed"] is False
    assert [r["session_id"] for r in client.get("/api/sessions?agent=shop-support&status=completed&limit=5")
            .json()["sessions"]] == ["s1"]
    assert client.get("/api/sessions?status=active").json()["sessions"][0]["session_id"] == "s2"
    assert client.get("/api/sessions?agent=&status=").status_code == 200         # empty filters mean "all"

    detail = client.get("/api/sessions/s1").json()
    assert detail["meta"]["agent_id"] == "shop-support" and detail["status"] == "completed"
    assert detail["turns"][0]["user_text"] == "주문번호는 [주문번호]이에요"
    assert detail["spans"][0]["input"] == {"arguments": {"order_id": "[주문번호]"}}
    assert detail["audio"] == [{"turn": 1, "role": "user", "bytes": 1644}]
    assert detail["analysis"]["summary"] == "주문 조회"
    assert client.get("/api/sessions/unknown").status_code == 404

    audio = client.get("/api/sessions/s1/audio/1/user")
    assert audio.status_code == 200 and audio.headers["content-type"] == "audio/wav"
    assert audio.content[:4] == b"RIFF" and len(audio.content) == 1644
    assert client.get("/api/sessions/s1/audio/1/agent").status_code == 404
    assert client.get("/api/sessions/s2/audio/1/user").status_code == 404        # not a test session: nothing stored
    assert client.get("/api/sessions/s1/audio/1/nobody").status_code == 404
    assert client.get("/api/sessions/..%2F..%2Fetc/audio/1/user").status_code == 404

    m = client.get("/api/metrics?agent=shop-support&version=1&days=7").json()
    assert m["sessions"] == 1 and m["response_ms"]["median"] == 1500.0 and m["tools"]["failure_rate"] == 0.0
    assert m["criteria"]["pass_rate"] == 1.0 and m["by_version"][0]["version"] == 1
    assert client.get("/api/metrics").json()["sessions"] == 2


def test_live_generator_yields_one_json_line_per_record_and_unsubscribes(env):
    async def run():
        stream = api.live_events(heartbeat=0.2)
        first = [await stream.__anext__(), await stream.__anext__()]
        assert first[0].startswith("retry:") and json.loads(first[1][6:])["type"] == "hello"
        assert trace.get_bus().subscriber_count() == 1
        await asyncio.get_running_loop().run_in_executor(None, record_session, "s-live")
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: trace.get_store().put_document("analysis", "s-live", {"session_id": "s-live", "criteria": [
                {"id": "fast", "passed": True}]}))
        events = []
        while len(events) < 6:
            line = await asyncio.wait_for(stream.__anext__(), 5)
            if line.startswith(":"):
                continue
            assert line.startswith("data: ") and line.endswith("\n\n") and "\n" not in line[:-2]
            events.append(json.loads(line[6:]))
        ping = await asyncio.wait_for(stream.__anext__(), 5)       # nothing happens: a heartbeat comment
        assert ping == ": ping\n\n"
        await stream.aclose()
        assert trace.get_bus().subscriber_count() == 0
        return events

    events = asyncio.run(run())
    assert [e["type"] for e in events] == ["session", "span", "turn", "session_end", "audio", "analysis"]
    assert all(e["session_id"] == "s-live" for e in events)
    assert events[0]["meta"]["agent_id"] == "shop-support"
    assert events[1]["span"]["kind"] == "tool" and events[1]["span"]["input"] == {"arguments": {"order_id": "[주문번호]"}}
    assert events[1]["span"]["ms"] == 200.0 and events[1]["span"]["id"] >= 1
    assert events[2]["record"]["user_text"] == "주문번호는 [주문번호]이에요" and events[2]["record"]["response_ms"] == 1500.0
    assert events[3]["status"] == "completed"
    assert events[5]["summary"]["passed"] == 1
    assert "A1001" not in json.dumps(events, ensure_ascii=False)


def test_publishing_does_not_block_and_survives_bad_subscribers(env):
    bus = trace.get_bus()
    seen = []

    def broken(event):
        raise RuntimeError("client went away")

    bus.subscribe(broken)
    bus.subscribe(seen.append)
    # a subscriber whose event loop is closed, as left behind by a server that shut down
    loop = asyncio.new_event_loop()
    loop.close()
    bus.subscribe(lambda event: loop.call_soon_threadsafe(print, event))
    assert bus.subscriber_count() == 3
    t = time.perf_counter()
    record_session("s-pub")
    assert time.perf_counter() - t < 2.0
    assert bus.subscriber_count() == 1                      # the two broken ones were dropped
    assert [e["type"] for e in seen] == ["session", "span", "turn", "session_end", "audio"]
    assert trace.get_store().get_session("s-pub")["status"] == "completed"


def test_live_over_http_and_client_disconnect(env, monkeypatch):
    monkeypatch.setattr(api, "HEARTBEAT_S", 0.2)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(make_app(), log_level="warning", lifespan="off"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.02)
        assert server.started
        got = []
        with httpx.Client(timeout=10) as client:
            with client.stream("GET", f"http://127.0.0.1:{port}/api/live") as response:
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                lines = response.iter_lines()
                for line in lines:
                    if line.startswith("data: "):
                        assert json.loads(line[6:])["type"] == "hello"
                        break
                assert trace.get_bus().subscriber_count() == 1
                record_session("s-http", status="active")          # recorded while the client listens
                for line in lines:
                    if line.startswith("data: "):
                        got.append(json.loads(line[6:]))
                        if len(got) == 4:
                            break
            # the client is gone now, in the middle of the stream
        assert [e["type"] for e in got] == ["session", "span", "turn", "audio"]
        deadline = time.time() + 5
        while trace.get_bus().subscriber_count() and time.time() < deadline:
            time.sleep(0.05)
        assert trace.get_bus().subscriber_count() == 0             # the server noticed and unsubscribed
        # the writer is not held up by the departed client
        trace.get_recorder().end_session("s-http", "abandoned")
        assert trace.get_recorder().flush(5.0)
        assert trace.get_store().get_session("s-http")["status"] == "abandoned"
        assert httpx.get(f"http://127.0.0.1:{port}/api/sessions", timeout=5).json()["sessions"][0]["status"] == "abandoned"
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()
    assert not thread.is_alive()
