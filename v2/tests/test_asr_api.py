"""ASR quality over REST: turn flags, recognition cases, the bench (fake recognisers), metrics, health.
Everything is written to a temporary data directory."""
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentkit import server, trace
from agentkit.contracts import SessionMeta, Span, TurnRecord
from agentkit.eval import api as eval_api
from agentkit.eval import asr, deps
from agentkit.trace import api as trace_api

PCM_1S = b"\x10\x00" * 16000


@pytest.fixture
def client(tmp_path):
    deps.reset()
    trace.reset(tmp_path / "data")
    app = FastAPI()
    app.include_router(trace_api.router)
    app.include_router(eval_api.router)
    with TestClient(app) as c:
        yield c
    trace.reset(None)


def record(sid, turns, *, engine="whisper", test=True, agent="shop-support", version=1, audio=True,
           status="completed", channel="voice", low=()):
    """turns: [(user, agent)] or [(user, agent, tool name)]. Spoken turns get an stt span."""
    rec = trace.get_recorder()
    t0 = time.time() - 60
    rec.start_session(SessionMeta(session_id=sid, agent_id=agent, version=version, channel=channel, test=test,
                                  started_at=t0))
    for n, row in enumerate(turns, 1):
        user, reply = row[0], row[1]
        t = t0 + n * 5
        if engine:
            rec.span(Span(session_id=sid, turn=n, kind="stt", t_start=t, t_end=t + 0.2, output={"text": user},
                          attrs={"engine": engine, "language": "ko", "avg_logprob": -1.2 if n in low else -0.2,
                                 "no_speech_prob": 0.01, "compression_ratio": 1.0, "audio_s": 1.0, "rms_db": -30.0,
                                 "low_confidence": n in low}))
        if len(row) > 2:
            rec.span(Span(session_id=sid, turn=n, kind="tool", name=row[2], t_start=t + 0.3, t_end=t + 0.5,
                          output={"ok": True}))
        rec.turn(TurnRecord(session_id=sid, turn=n, user_text=user, agent_text=reply, t_user_end=t,
                            t_agent_start=t + 1, t_agent_end=t + 3))
        if audio:
            rec.audio(sid, n, "user", PCM_1S)
    if status != "active":
        rec.end_session(sid, status)
    assert rec.flush(5.0)


CONVERSATION = [
    ("가져서 배달됐어요.", "죄송합니다, 다시 한번 말씀해 주시겠어요?"),
    ("뺏어져서 배달됐다고요.", "확인을 위해 잠시만 기다려 주세요."),
    ("언제까지 기다려요?", "주문을 확인했습니다.", "get_order"),
    ("고맙습니다", "네, 좋은 하루 보내세요."),
]


def test_auto_flags_are_stored_when_the_session_ends_and_shown_on_every_turn(client):
    record("s1", CONVERSATION, low=(1,))
    store = trace.get_store()
    # written by the recorder at end_session, before anybody read the session
    assert {d["turn"]: d["auto"] for d in store.list_documents("turn_flag", "s1/")} == {
        1: ["low_confidence", "reask"], 2: ["repeat", "empty_wait"], 3: ["complaint"]}
    turns = client.get("/api/sessions/s1").json()["turns"]
    assert [t["flags"]["auto"] for t in turns] == [["low_confidence", "reask"], ["repeat", "empty_wait"],
                                                   ["complaint"], []]
    assert turns[0]["flags"]["misheard"] is None and turns[0]["flags"]["session_id"] == "s1"
    assert turns[3]["flags"] == {"auto": [], "misheard": None}
    row = client.get("/api/sessions").json()["sessions"][0]
    assert row["asr"] == {"suspect_turns": 3, "misheard_turns": 0}


def test_auto_flags_of_a_running_session_are_computed_on_read_and_stored_lazily_once_ended(client):
    record("live", CONVERSATION[:2], status="active")
    store = trace.get_store()
    assert store.list_documents("turn_flag", "live/") == []
    turns = client.get("/api/sessions/live").json()["turns"]
    assert turns[1]["flags"] == {"auto": ["repeat", "empty_wait"], "misheard": None}
    assert store.list_documents("turn_flag", "live/") == []            # still running: nothing stored
    # a session that ended without its flags (recorded by older code) gets them on the first read
    trace.get_recorder().end_session("live", "completed")
    assert trace.get_recorder().flush(5.0)
    for doc in store.list_documents("turn_flag", "live/"):
        store.delete_document("turn_flag", doc.key)
    assert client.get("/api/sessions/live").json()["turns"][0]["flags"]["auto"] == ["reask"]
    assert sorted(d["turn"] for d in store.list_documents("turn_flag", "live/")) == [1, 2]


def test_flagging_a_turn_creates_updates_and_removes_its_case(client):
    record("s1", CONVERSATION, low=(1,))
    r = client.post("/api/sessions/s1/turns/2/flag",
                    json={"misheard": True, "actual_text": "떨어진 상태로 배달됐다고요", "note": "두 번째 시도"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["flags"]["misheard"] is True and body["flags"]["actual_text"] == "떨어진 상태로 배달됐다고요"
    assert body["flags"]["auto"] == ["repeat", "empty_wait"] and body["flags"]["note"] == "두 번째 시도"
    assert body["flags"]["turn"] == 2 and body["flags"]["updated_at"] > time.time() - 10
    case = body["case"]
    assert case["id"] == "s1-2" and case["session_id"] == "s1" and case["turn"] == 2
    assert case["agent_id"] == "shop-support" and case["engine"] == "whisper" and case["audio_s"] == 1.0
    assert case["expected"] == "떨어진 상태로 배달됐다고요" and case["heard"] == "뺏어져서 배달됐다고요."
    assert case["audio_url"] == "/api/sessions/s1/audio/2/user"
    audio = client.get(case["audio_url"])
    assert audio.status_code == 200 and audio.headers["content-type"] == "audio/wav"

    assert client.get("/api/sessions/s1").json()["turns"][1]["flags"]["misheard"] is True
    assert client.get("/api/sessions").json()["sessions"][0]["asr"]["misheard_turns"] == 1
    assert [c["id"] for c in client.get("/api/asr/cases").json()["cases"]] == ["s1-2"]
    assert client.get("/api/asr/cases?agent=other").json()["cases"] == []
    assert len(client.get("/api/asr/cases?agent=shop-support").json()["cases"]) == 1

    # the same turn again: one case, new text, same creation time; a flag without text keeps the text
    again = client.post("/api/sessions/s1/turns/2/flag", json={"misheard": True, "actual_text": "깨져서 왔다고요"}).json()
    assert again["case"]["expected"] == "깨져서 왔다고요" and again["case"]["created_at"] == case["created_at"]
    assert again["flags"]["note"] == "두 번째 시도"
    kept = client.post("/api/sessions/s1/turns/2/flag", json={"misheard": True}).json()
    assert kept["case"]["expected"] == "깨져서 왔다고요" and len(client.get("/api/asr/cases").json()["cases"]) == 1

    # taking the mark back removes the case; deleting a case leaves the flag alone
    off = client.post("/api/sessions/s1/turns/2/flag", json={"misheard": False}).json()
    assert off["flags"]["misheard"] is False and off["case"] is None
    assert client.get("/api/asr/cases").json()["cases"] == []
    client.post("/api/sessions/s1/turns/1/flag", json={"misheard": True, "actual_text": "깨져서 배달됐어요"})
    assert client.delete("/api/asr/cases/s1-1").json() == {"ok": True}
    assert client.delete("/api/asr/cases/s1-1").status_code == 404
    assert client.get("/api/sessions/s1").json()["turns"][0]["flags"]["misheard"] is True


def test_flag_errors_and_turns_without_audio(client):
    record("noaudio", CONVERSATION[:1], audio=False)
    assert client.post("/api/sessions/nope/turns/1/flag", json={"misheard": True}).status_code == 404
    assert client.post("/api/sessions/noaudio/turns/9/flag", json={"misheard": True}).status_code == 404
    assert client.post("/api/sessions/noaudio/turns/1/flag", json={}).status_code == 422
    body = client.post("/api/sessions/noaudio/turns/1/flag", json={"misheard": True, "actual_text": "깨져서요"}).json()
    assert body["flags"]["misheard"] is True and body["case"] is None        # flagged, but nothing to replay
    assert client.get("/api/asr/cases").json()["cases"] == []


def test_a_production_session_keeps_no_audio_so_it_cannot_become_a_case(client):
    record("prod", CONVERSATION[:1], test=False)
    body = client.post("/api/sessions/prod/turns/1/flag", json={"misheard": True, "actual_text": "깨져서요"}).json()
    assert body["case"] is None and client.get("/api/sessions/prod").json()["audio"] == []


def test_case_text_is_stored_unmasked_and_the_flag_document_masked(client):
    record("s1", [("전화번호는 공일공 어쩌고", "네.")])
    said = "제 번호는 010-1234-5678이에요"
    body = client.post("/api/sessions/s1/turns/1/flag", json={"misheard": True, "actual_text": said}).json()
    assert body["case"]["expected"] == said
    assert client.get("/api/asr/cases").json()["cases"][0]["expected"] == said
    assert "1234" not in body["flags"]["actual_text"]
    assert "1234" not in client.get("/api/sessions/s1").json()["turns"][0]["flags"]["actual_text"]


def wait_bench(client, run_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        doc = client.get(f"/api/asr/bench/{run_id}").json()
        if doc["status"] != "running":
            return doc
        time.sleep(0.05)
    raise AssertionError("bench did not finish")


def test_bench_runs_the_cases_through_every_available_engine(client, monkeypatch):
    record("s1", CONVERSATION)
    record("s2", [("안녕 하세요", "네, 안녕하세요.")], agent="other-agent")
    client.post("/api/sessions/s1/turns/1/flag", json={"misheard": True, "actual_text": "깨져서 배달됐어요"})
    client.post("/api/sessions/s1/turns/2/flag", json={"misheard": True, "actual_text": "떨어진 상태로 배달됐다고요"})
    client.post("/api/sessions/s1/turns/3/flag", json={"misheard": True})            # no text: cannot be scored
    client.post("/api/sessions/s2/turns/1/flag", json={"misheard": True, "actual_text": "안녕하세요"})

    heard = []

    def fake_recognizers(names):
        def whisper(audio, language):
            heard.append((len(audio), str(audio.dtype), language))
            return ["안녕하세요", "떨어진 상태로 배달됐다구요", "깨져서 배달됐어요."][len(heard) - 1]   # newest case first
        return {"whisper": whisper, "qwen": None}

    monkeypatch.setattr(asr, "recognizers", fake_recognizers)
    started = client.post("/api/asr/bench", json={}).json()
    assert started["status"] == "running" and started["run_id"].startswith("asr-")
    doc = wait_bench(client, started["run_id"])
    assert doc["status"] == "finished" and doc["total"] == 3 and doc["done"] == 3 and doc["skipped"] == ["s1-3"]
    assert heard == [(16000, "float32", "ko")] * 3                    # the stored audio, as the recogniser wants it
    # s1-1 exact, s1-2 one of 12 characters wrong, s2-1 exact -> 1 edit over 8 + 12 + 5 characters
    assert doc["engines"]["whisper"] == {"n": 3, "cer": round(1 / 25, 4), "exact": 2, "exact_rate": round(2 / 3, 4),
                                         "ms_median": doc["engines"]["whisper"]["ms_median"], "available": True}
    assert doc["engines"]["qwen"] == {"n": 0, "cer": None, "exact": 0, "exact_rate": None, "ms_median": None,
                                      "available": False}
    by_case = {item["case_id"]: item for item in doc["items"]}
    assert set(by_case) == {"s1-1", "s1-2", "s2-1"}
    assert by_case["s1-2"]["expected"] == "떨어진 상태로 배달됐다고요"
    assert by_case["s1-2"]["heard_then"] == "뺏어져서 배달됐다고요."
    assert by_case["s1-2"]["results"] == {"whisper": {"text": "떨어진 상태로 배달됐다구요", "cer": round(1 / 12, 4),
                                                       "ms": by_case["s1-2"]["results"]["whisper"]["ms"]}}
    assert by_case["s1-1"]["results"]["whisper"]["cer"] == 0.0

    # one agent, one engine
    heard.clear()
    one = wait_bench(client, client.post("/api/asr/bench", json={"engines": ["whisper"], "agent": "other-agent"}).json()["run_id"])
    assert one["total"] == 1 and list(one["engines"]) == ["whisper"] and one["agent"] == "other-agent"
    runs = client.get("/api/asr/bench").json()["runs"]
    assert [r["run_id"] for r in runs] == [one["run_id"], doc["run_id"]] and "items" not in runs[0]
    assert runs[1]["engines"]["whisper"]["n"] == 3 and runs[1]["status"] == "finished"

    assert client.post("/api/asr/bench", json={"engines": ["parakeet"]}).status_code == 400
    assert client.get("/api/asr/bench/nope").status_code == 404


def test_bench_reports_engine_errors_and_a_run_whose_process_is_gone(client, monkeypatch):
    record("s1", CONVERSATION[:1])
    client.post("/api/sessions/s1/turns/1/flag", json={"misheard": True, "actual_text": "깨져서 배달됐어요"})

    def broken(audio, language):
        raise RuntimeError("model fell over")

    store = trace.get_store()
    doc = asr.run_bench(store, "asr-direct", ["whisper", "qwen"],
                        recognizers_fn=lambda names: {"whisper": broken, "qwen": lambda a, l: "깨져서 배달됐어요"})
    assert doc["status"] == "finished"
    assert "model fell over" in doc["items"][0]["results"]["whisper"]["error"]
    assert doc["engines"]["whisper"]["n"] == 0 and doc["engines"]["whisper"]["available"] is True
    assert doc["engines"]["qwen"]["cer"] == 0.0 and doc["engines"]["qwen"]["exact"] == 1

    store.put_document("asr_bench", "asr-stale", asr.first_document("asr-stale", ["whisper"]), mask=False)
    assert client.get("/api/asr/bench/asr-stale").json()["status"] == "error"


def test_metrics_carry_recognition_quality(client):
    record("w1", CONVERSATION, low=(1,))                               # whisper, 4 spoken turns
    record("q1", [("환불해 주세요", "네, 접수했습니다.", "request_refund"),
                  ("환불 해주세요!", "잘 전달되지 않았습니다.")], engine="qwen", version=2)
    record("t1", [("안녕하세요", "잠시만 기다려 주세요.")], engine=None, channel="text")     # typed: not a voice turn
    client.post("/api/sessions/w1/turns/1/flag", json={"misheard": True, "actual_text": "깨져서 배달됐어요"})
    client.post("/api/sessions/w1/turns/2/flag", json={"misheard": True})
    client.post("/api/sessions/w1/turns/3/flag", json={"misheard": False})

    m = client.get("/api/metrics").json()
    assert m["turns"] == 7
    assert m["asr"] == {"voice_turns": 6, "suspect_turns": 4, "suspect_rate": round(4 / 6, 4),
                        "misheard_turns": 2, "misheard_rate": round(2 / 6, 4), "reask_rate": round(2 / 6, 4),
                        "repeat_rate": round(2 / 6, 4), "low_confidence_rate": round(1 / 6, 4)}
    assert m["empty_wait_turns"] == 2 and m["empty_wait_rate"] == round(2 / 7, 4)      # w1 turn 2 and the typed one
    assert m["by_stt_engine"]["whisper"] == {
        "voice_turns": 4, "suspect_turns": 3, "suspect_rate": 0.75, "misheard_turns": 2, "misheard_rate": 0.5,
        "reask_rate": 0.25, "repeat_rate": 0.25, "low_confidence_rate": 0.25}
    assert m["by_stt_engine"]["qwen"] == {
        "voice_turns": 2, "suspect_turns": 1, "suspect_rate": 0.5, "misheard_turns": 0, "misheard_rate": 0.0,
        "reask_rate": 0.5, "repeat_rate": 0.5, "low_confidence_rate": 0.0}
    versions = {(v["agent_id"], v["version"]): v for v in m["by_version"]}
    v1, v2 = versions[("shop-support", 1)], versions[("shop-support", 2)]
    assert v1["asr"]["voice_turns"] == 4 and v1["empty_wait_turns"] == 2 and v1["empty_wait_rate"] == 0.4
    assert v2["asr"]["voice_turns"] == 2 and v2["empty_wait_turns"] == 0 and list(v2["by_stt_engine"]) == ["qwen"]
    assert m["by_day"][0]["asr"]["voice_turns"] == 6

    empty = client.get("/api/metrics?agent=nobody").json()
    assert empty["asr"]["voice_turns"] == 0 and empty["asr"]["suspect_rate"] is None
    assert empty["by_stt_engine"] == {} and empty["empty_wait_turns"] == 0 and empty["empty_wait_rate"] is None


def test_health_reports_internal_testing(monkeypatch):
    async def factory(agent_id, **options):
        raise RuntimeError("not used")

    monkeypatch.delenv("AGENTKIT_INTERNAL_TESTING", raising=False)
    with TestClient(server.create_app(factory)) as c:
        assert c.get("/api/health").json()["internal_testing"] is True          # the default
        monkeypatch.setenv("AGENTKIT_INTERNAL_TESTING", "0")
        health = c.get("/api/health").json()
        assert health["internal_testing"] is False and health["ok"] is True
