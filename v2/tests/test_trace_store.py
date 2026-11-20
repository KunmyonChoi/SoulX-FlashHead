"""The store: session list, session detail, metrics, documents, retention."""
import time

import pytest

from agentkit import trace
from agentkit.contracts import AnalysisResult, CriterionResult, SessionMeta, Span, TurnRecord


@pytest.fixture
def env(tmp_path):
    trace.reset(tmp_path / "data")
    yield tmp_path / "data"
    trace.reset(None)


def add_session(sid, *, agent="shop-support", version=1, started_at=None, delays=(1.0,), status="completed",
                tools=(), handoff=False, criteria=None, channel="text", test=False, simulated=False, label="",
                llm_s=0.5, audio=False):
    """delays: seconds from the user's end to the agent's start, one turn each (None: no reply).
    tools: (name, ok, extra output fields, extra attrs) per call. criteria: (id, passed, safety)."""
    rec = trace.get_recorder()
    t0 = time.time() - 60 if started_at is None else started_at
    rec.start_session(SessionMeta(session_id=sid, agent_id=agent, version=version, channel=channel,
                                  test=test, simulated=simulated, started_at=t0, label=label))
    for i, delay in enumerate(delays, start=1):
        t = t0 + i * 10
        rec.span(Span(session_id=sid, turn=i, kind="llm", name="gemma4:26b", t_start=t, t_end=t + llm_s,
                      input={"messages": []}, output={"text": "네."}))
        rec.span(Span(session_id=sid, turn=i, kind="tts", t_start=t + llm_s, t_end=t + llm_s + 0.2,
                      input={"chars": 2}))
        rec.turn(TurnRecord(session_id=sid, turn=i, user_text=f"질문 {i}", agent_text="네.", t_user_end=t,
                            t_agent_start=None if delay is None else t + delay,
                            t_agent_end=None if delay is None else t + delay + 1,
                            handoff=handoff and i == len(delays)))
        if audio:
            rec.audio(sid, i, "user", b"\x00\x00" * 160)
    for name, ok, output, attrs in tools:
        rec.span(Span(session_id=sid, turn=1, kind="tool", name=name, t_start=t0 + 1, t_end=t0 + 1.1, ok=ok,
                      input={"arguments": {}}, output={"ok": ok, "text": "결과", **output}, attrs=attrs))
    if handoff:
        rec.span(Span(session_id=sid, turn=len(delays), kind="handoff", t_start=t0 + 50, t_end=t0 + 50,
                      attrs={"rule_id": "large_refund", "reason": "금액 초과"}))
    if status != "active":
        rec.end_session(sid, status)
    assert rec.flush(5.0)
    if criteria is not None:
        result = AnalysisResult(
            session_id=sid, agent_id=agent, version=version, summary="환불 문의",
            criteria=[CriterionResult(id=c, passed=p, safety=s, evidence_turns=[1]) for c, p, s in criteria],
            metrics={"turns": float(len(delays))}, created_at=time.time())
        trace.get_store().put_document("analysis", sid, result.model_dump())


def test_empty_store_answers_without_a_database(env):
    store = trace.get_store()
    assert store.list_sessions() == []
    assert store.get_session("nope") is None
    assert store.get_document("analysis", "nope") is None
    assert store.list_documents("analysis") == []
    assert store.purge(1) == 0
    m = store.metrics()
    assert m["sessions"] == 0 and m["response_ms"] == {"median": None, "p90": None, "count": 0}
    assert m["tools"]["failure_rate"] is None and m["by_version"] == []
    assert not (env / "trace.sqlite3").exists()        # reading does not create anything


def test_list_sessions_rows(env):
    now = time.time()
    add_session("s1", started_at=now - 300, delays=(1.0, 2.0, 4.0), label="refund_small", simulated=True,
                criteria=[("verified", True, True), ("fast", True, False), ("polite", False, False)])
    add_session("s2", started_at=now - 200, delays=(0.5,), status="handoff", handoff=True, channel="voice",
                test=True, criteria=[("verified", False, True), ("fast", None, False)])
    add_session("s3", started_at=now - 100, agent="tech-trend-anchor", version=2, delays=(None,), status="active")
    store = trace.get_store()

    rows = store.list_sessions()
    assert [r["session_id"] for r in rows] == ["s3", "s2", "s1"]          # newest first
    s3, s2, s1 = rows
    assert s1 == {
        "session_id": "s1", "agent_id": "shop-support", "agent": "shop-support", "version": 1, "channel": "text",
        "label": "refund_small", "test": False, "simulated": True, "started_at": s1["started_at"],
        "ended_at": s1["ended_at"], "status": "completed", "turns": 3, "turn_count": 3,
        "response_ms_median": 2000.0, "response_ms_max": 4000.0, "handoff": False, "interrupted": 0,
        "analysis": {"passed": 2, "failed": 1, "unjudged": 0, "total": 3, "safety_failed": False,
                     "all_passed": False, "failed_ids": ["polite"], "created_at": s1["analysis"]["created_at"]},
        "asr": {"suspect_turns": 0, "misheard_turns": 0},
    }
    assert abs(s1["started_at"] - (now - 300)) < 1e-3 and s1["ended_at"] > s1["started_at"]
    assert s2["status"] == "handoff" and s2["handoff"] is True and s2["test"] is True and s2["channel"] == "voice"
    assert s2["analysis"]["passed"] == 0 and s2["analysis"]["total"] == 2
    assert s2["analysis"]["safety_failed"] is True and s2["analysis"]["unjudged"] == 1
    assert s3["status"] == "active" and s3["analysis"] is None
    assert s3["turns"] == 1 and s3["response_ms_median"] is None

    assert [r["session_id"] for r in store.list_sessions(agent="shop-support")] == ["s2", "s1"]
    assert [r["session_id"] for r in store.list_sessions(status="active")] == ["s3"]
    assert [r["session_id"] for r in store.list_sessions(limit=1)] == ["s3"]
    assert [r["session_id"] for r in store.list_sessions(test=True)] == ["s2", "s1"]
    assert [r["session_id"] for r in store.list_sessions(version=2)] == ["s3"]


def test_get_session_shape(env):
    add_session("s1", delays=(1.0, 2.0), label="case-a", test=True, audio=True,
                tools=[("get_order", True, {"data": {"amount": 30000}}, {"effect": "read"})],
                criteria=[("verified", True, True)])
    session = trace.get_store().get_session("s1")
    assert set(session) == {"meta", "status", "ended_at", "turns", "spans", "audio", "analysis"}
    assert SessionMeta(**session["meta"]).label == "case-a"                 # loads back into the contract types
    turns = [TurnRecord(**{k: v for k, v in t.items() if k != "response_ms"}) for t in session["turns"]]
    assert [t.turn for t in turns] == [1, 2] and turns[0].user_text == "질문 1"
    spans = [Span(**{k: v for k, v in s.items() if k not in ("id", "ms")}) for s in session["spans"]]
    assert [s.kind for s in spans] == ["tool", "llm", "tts", "llm", "tts"]   # ordered by start time
    assert session["spans"][1]["ms"] == 500.0
    assert session["spans"][0]["output"]["data"] == {"amount": 30000}
    assert session["audio"] == [{"turn": 1, "role": "user", "bytes": 364}, {"turn": 2, "role": "user", "bytes": 364}]
    assert AnalysisResult(**session["analysis"]).criteria[0].id == "verified"


def test_metrics(env):
    now = time.time()
    confirm = ("request_refund", True, {"needs_confirmation": True, "confirmation_text": "환불할까요?"},
               {"effect": "write", "executed": False})
    done = ("request_refund", True, {"executed": True}, {"effect": "write", "executed": True})
    not_found = ("get_order", False, {"error_type": "not_found"}, {"effect": "read", "error_type": "not_found"})
    timeout = ("get_order", False, {}, {"effect": "read", "error_type": "timeout"})
    found = ("get_order", True, {}, {"effect": "read"})
    # version 1: delays 1,2,3,4 s and 10 s -> median 3000; 5 tool calls, 2 failures; 1 of 2 sessions handed off
    add_session("a1", started_at=now - 3600, delays=(1.0, 2.0, 3.0, 4.0), tools=[found, confirm, done, not_found],
                criteria=[("verified", True, True), ("fast", True, False)])
    add_session("a2", started_at=now - 1800, delays=(10.0, None), status="handoff", handoff=True, tools=[timeout],
                criteria=[("verified", False, True), ("fast", None, False)], llm_s=1.5)
    # version 2: faster, no failures, all passed
    add_session("b1", started_at=now - 600, version=2, delays=(0.5, 0.7), tools=[found, confirm, done], llm_s=0.25,
                criteria=[("verified", True, True), ("fast", True, False)])
    # another agent, and a session outside the window
    add_session("c1", started_at=now - 300, agent="tech-trend-anchor", delays=(2.0,))
    add_session("old", started_at=now - 20 * 86400, delays=(99.0,), tools=[not_found])
    store = trace.get_store()

    v1 = store.metrics(agent="shop-support", version=1, days=7)
    assert v1["sessions"] == 2 and v1["turns"] == 6
    assert v1["response_ms"] == {"median": 3000.0, "p90": 7600.0, "count": 5}
    assert v1["response_ms_median"] == 3000.0 and v1["response_ms_p90"] == 7600.0
    assert v1["span_ms"]["llm"] == {"median": 500.0, "p90": 1500.0, "count": 6}
    assert v1["span_ms"]["tts"]["median"] == 200.0 and v1["span_ms"]["tool"]["median"] == 100.0
    assert v1["span_ms"]["handoff"]["count"] == 1
    tools = v1["tools"]
    assert (tools["calls"], tools["failures"], tools["failure_rate"]) == (5, 2, 0.4)
    assert tools["needs_confirmation"] == 1 and tools["executed"] == 1        # a pending confirmation is no failure
    assert tools["error_types"] == {"not_found": 1, "timeout": 1}
    assert tools["by_name"] == {"get_order": {"calls": 3, "failures": 2, "failure_rate": 0.6667},
                                "request_refund": {"calls": 2, "failures": 0, "failure_rate": 0.0}}
    assert v1["handoff"] == {"sessions": 1, "rate": 0.5} and v1["handoff_rate"] == 0.5
    criteria = v1["criteria"]
    assert (criteria["judged"], criteria["passed"], criteria["pass_rate"]) == (3, 2, 0.6667)   # None is not judged
    assert criteria["sessions_analyzed"] == 2 and criteria["sessions_all_passed"] == 1
    assert criteria["safety_failures"] == 1
    assert criteria["by_id"] == {"verified": {"judged": 2, "passed": 1, "pass_rate": 0.5},
                                 "fast": {"judged": 1, "passed": 1, "pass_rate": 1.0}}
    assert v1["status"] == {"completed": 1, "handoff": 1}

    # two versions side by side
    both = store.metrics(agent="shop-support", days=7)
    assert both["sessions"] == 3
    by = {(g["agent_id"], g["version"]): g for g in both["by_version"]}
    assert set(by) == {("shop-support", 1), ("shop-support", 2)}
    assert by[("shop-support", 1)]["response_ms"] == v1["response_ms"]
    v2 = by[("shop-support", 2)]
    assert v2["response_ms"]["median"] == 600.0 and v2["tools"]["failure_rate"] == 0.0
    assert v2["handoff_rate"] == 0.0 and v2["criteria"]["pass_rate"] == 1.0
    assert v2["span_ms"]["llm"]["median"] == 250.0
    assert sum(d["sessions"] for d in both["by_day"]) == 3

    everything = store.metrics(days=7)
    assert everything["sessions"] == 4 and len(everything["by_version"]) == 3
    assert store.metrics(days=30)["sessions"] == 5
    assert store.metrics(agent="nobody")["sessions"] == 0


def test_a_failed_span_that_only_asks_for_confirmation_is_not_a_failure(env):
    add_session("s1", tools=[("request_refund", False, {"needs_confirmation": True}, {"executed": False})])
    tools = trace.get_store().metrics()["tools"]
    assert (tools["calls"], tools["failures"], tools["needs_confirmation"]) == (1, 0, 1)


def test_documents(env):
    store = trace.get_store()
    store.put_document("test_run", "shop-support/run-2", {"run_id": "run-2", "agent_id": "shop-support", "pass_rate": 0.5})
    store.put_document("test_run", "shop-support/run-1", {"run_id": "run-1", "agent_id": "shop-support", "pass_rate": 1.0})
    store.put_document("test_run", "other/run-9", {"run_id": "run-9"})
    store.put_document("case", "c1", {"id": "c1", "user_turns": ["주문번호는 A1001이에요", "30000원 환불이요"],
                                      "facts": {"order_id": "A1001", "phone_last4": "1234"}})
    doc = store.get_document("test_run", "shop-support/run-1")
    assert doc == {"run_id": "run-1", "agent_id": "shop-support", "pass_rate": 1.0}
    assert doc.key == "shop-support/run-1" and doc.kind == "test_run" and doc.updated_at > 0
    listed = store.list_documents("test_run", prefix="shop-support/")
    assert [d["run_id"] for d in listed] == ["run-1", "run-2"] and [d.key for d in listed][0] == "shop-support/run-1"
    assert len(store.list_documents("test_run")) == 3
    assert store.get_document("test_run", "missing") is None
    # replaced, not duplicated
    store.put_document("test_run", "shop-support/run-1", {"run_id": "run-1", "pass_rate": 0.0})
    assert store.get_document("test_run", "shop-support/run-1")["pass_rate"] == 0.0
    assert len(store.list_documents("test_run")) == 3
    # documents are masked like everything else; identifier keys are left alone
    case = store.get_document("case", "c1")
    assert case == {"id": "c1", "user_turns": ["주문번호는 [주문번호]이에요", "30000원 환불이요"],
                    "facts": {"order_id": "[주문번호]", "phone_last4": "[가려짐]"}}
    store.put_document("case", "raw", {"facts": {"order_id": "A1001"}}, mask=False)      # the caller's decision
    assert store.get_document("case", "raw")["facts"]["order_id"] == "A1001"
    store.put_document("test_run", "ids", {"run_id": "ab1234", "session_id": "s1001", "items": [{"item_id": "p001"}],
                                           "improved": ["p001"]})
    assert store.get_document("test_run", "ids") == {"run_id": "ab1234", "session_id": "s1001",
                                                     "items": [{"item_id": "p001"}], "improved": ["p001"]}
    assert store.delete_document("test_run", "ids") and not store.delete_document("test_run", "ids")


def test_purge_removes_old_sessions_and_everything_of_theirs(env):
    now = time.time()
    add_session("old-ended", started_at=now - 40 * 86400, test=True, audio=True, criteria=[("verified", True, True)])
    add_session("old-active", started_at=now - 35 * 86400, status="active", simulated=True, audio=True)
    add_session("recent", started_at=now - 5 * 86400, test=True, audio=True, criteria=[("verified", True, True)])
    store = trace.get_store()
    # sessions are dated by their end; make the first one end long ago
    with store.connect() as conn:
        conn.execute("UPDATE sessions SET ended_at = started_at + 60 WHERE session_id='old-ended'")
        conn.execute("UPDATE sessions SET ended_at = started_at + 60 WHERE session_id='recent'")
    store.put_document("note", "about-old", {"session_id": "old-ended", "text": "세션에 딸린 문서"})
    store.put_document("test_run", "run-1", {"run_id": "run-1"})
    assert (env / "audio" / "old-ended" / "1-user.wav").exists()
    marker = "질문 1".encode()
    assert marker in (env / "trace.sqlite3").read_bytes()

    assert store.purge(older_than_days=30) == 2

    assert [r["session_id"] for r in store.list_sessions()] == ["recent"]
    assert store.get_session("old-ended") is None and store.get_session("old-active") is None
    with store.connect() as conn:
        for table in ("turns", "spans"):
            left = {r[0] for r in conn.execute(f"SELECT DISTINCT session_id FROM {table}")}
            assert left == {"recent"}, table
    assert store.get_document("analysis", "old-ended") is None
    assert store.get_document("note", "about-old") is None
    assert store.get_document("analysis", "recent") is not None
    assert store.get_document("test_run", "run-1") is not None            # not tied to a session: kept
    assert not (env / "audio" / "old-ended").exists() and not (env / "audio" / "old-active").exists()
    assert (env / "audio" / "recent" / "1-user.wav").exists()
    assert store.purge(older_than_days=30) == 0
    # the default period is config.RETENTION_DAYS (30)
    assert store.purge() == 0
    assert store.purge(older_than_days=1) == 1 and store.list_sessions() == []
    assert marker not in (env / "trace.sqlite3").read_bytes()             # deleted rows are overwritten


def test_the_writer_thread_purges_on_its_own(env):
    now = time.time()
    add_session("ancient", started_at=now - 400 * 86400, status="active")   # never ended: dated by its start
    add_session("fresh", started_at=now - 60)
    rec = trace.get_recorder()
    rec._next_purge = 0.0                      # due now; normally 30 s after start and then every 6 hours
    deadline = time.time() + 5
    while time.time() < deadline and len(trace.get_store().list_sessions()) > 1:
        time.sleep(0.05)
    assert [r["session_id"] for r in trace.get_store().list_sessions()] == ["fresh"]
