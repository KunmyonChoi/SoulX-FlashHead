"""The recorder: returns at once, never raises, masks before anything reaches the disk,
keeps audio only for test or simulated sessions."""
import logging
import shutil
import statistics
import time
import wave

import pytest

from agentkit import trace
from agentkit.contracts import SessionMeta, Span, TurnRecord
from agentkit.trace import TraceRecorder, TraceStore

T0 = 1_800_000_000.0


@pytest.fixture
def env(tmp_path):
    trace.reset(tmp_path / "data")
    yield tmp_path / "data"
    trace.reset(None)


def meta(sid, **kw):
    base = dict(session_id=sid, agent_id="shop-support", version=1, channel="text", started_at=T0)
    base.update(kw)
    return SessionMeta(**base)


def disk_bytes(folder):
    """Everything under the data directory, the SQLite file and its side files included."""
    return b"".join(p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file())


def llm_span(sid, turn, n_messages=20):
    messages = [{"role": "system", "content": "당신은 쇼핑몰 상담원입니다. " * 20}]
    messages += [{"role": "user" if i % 2 else "assistant", "content": f"문의 {i}: 배송이 언제 오나요? " * 3}
                 for i in range(n_messages)]
    return Span(session_id=sid, turn=turn, kind="llm", name="gemma4:26b", t_start=T0, t_end=T0 + 0.8,
                input={"messages": messages}, output={"text": "내일 도착합니다.", "tool_calls": []},
                attrs={"model": "gemma4:26b", "round": 1, "first_token_ms": 310.0})


def test_singletons_follow_the_data_dir(env, tmp_path, monkeypatch):
    assert trace.get_recorder() is trace.get_recorder()
    assert trace.get_store() is trace.get_store()
    assert trace.get_recorder().store is trace.get_store()
    assert trace.get_store().db_path == env / "trace.sqlite3"
    # without an explicit directory the environment variable decides, read at first use
    monkeypatch.setenv("AGENTKIT_DATA_DIR", str(tmp_path / "from_env"))
    trace.reset(None)
    assert trace.get_store().data_dir == tmp_path / "from_env"


def test_recorder_calls_return_in_about_a_millisecond(env):
    rec = trace.get_recorder()
    rec.start_session(meta("s-fast"))
    spans = [llm_span("s-fast", i % 5 + 1) for i in range(1500)]
    took = []
    for i, span in enumerate(spans):
        t = time.perf_counter()
        rec.span(span, ["input.arguments.phone_last4"])
        took.append(time.perf_counter() - t)
        if i % 10 == 0:
            t = time.perf_counter()
            rec.turn(TurnRecord(session_id="s-fast", turn=i % 5 + 1, user_text="배송 언제 와요",
                                agent_text="내일 도착합니다.", t_user_end=T0, t_agent_start=T0 + 1))
            took.append(time.perf_counter() - t)
    took.sort()
    median, p99, worst = statistics.median(took), took[int(len(took) * 0.99)], took[-1]
    print(f"\nrecorder call: median {median * 1e6:.0f} us, p99 {p99 * 1e6:.0f} us, max {worst * 1e6:.0f} us"
          f" over {len(took)} calls (llm span with 21 messages, writer thread busy)")
    assert median < 0.001
    assert p99 < 0.005        # the writer thread holds the interpreter lock for up to a few ms at a time
    assert rec.flush(30.0)
    assert rec.stats()["dropped"] == 0
    assert len(trace.get_store().get_session("s-fast")["spans"]) == 1500


def test_unwritable_database_never_raises(tmp_path, caplog):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the data directory should be")
    rec = TraceRecorder(TraceStore(blocker / "data"), auto_purge=False)
    with caplog.at_level(logging.WARNING, logger="agentkit.trace"):
        t = time.perf_counter()
        rec.start_session(meta("s-broken", test=True))
        rec.span(llm_span("s-broken", 1))
        rec.turn(TurnRecord(session_id="s-broken", turn=1, user_text="a", agent_text="b", t_user_end=T0))
        rec.audio("s-broken", 1, "user", b"\x00\x01" * 1600)
        rec.end_session("s-broken", "completed")
        assert time.perf_counter() - t < 0.05
        assert rec.flush(5.0)
    assert rec.stats()["dropped"] >= 5
    assert any("trace recorder" in r.getMessage() for r in caplog.records)
    rec.close()


def test_store_deleted_in_mid_session_is_survived(env):
    rec = trace.get_recorder()
    rec.start_session(meta("s-del"))
    rec.span(llm_span("s-del", 1))
    assert rec.flush(5.0)
    shutil.rmtree(env)
    rec.span(llm_span("s-del", 2))
    rec.turn(TurnRecord(session_id="s-del", turn=2, user_text="a", agent_text="b", t_user_end=T0))
    rec.end_session("s-del", "completed")
    assert rec.flush(5.0)
    # the database was created again; the session row is gone but later records were kept
    assert trace.get_store().get_session("s-del") is None
    with trace.get_store().connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM spans WHERE session_id='s-del'").fetchone()[0] == 1


def test_full_queue_drops_and_warns(tmp_path, caplog):
    rec = TraceRecorder(TraceStore(tmp_path / "data"), max_queue=5, start=False)   # nothing drains the queue
    with caplog.at_level(logging.WARNING, logger="agentkit.trace"):
        for i in range(100):
            rec.span(llm_span("s-full", 1, n_messages=2))
    assert rec.stats() == {"queued": 5, "pending": 5, "written": 0, "dropped": 95}
    assert any("queue full" in r.getMessage() for r in caplog.records)
    assert rec.flush(0.05) is False


def test_bad_arguments_do_not_raise(env):
    rec = trace.get_recorder()
    rec.start_session(None)
    rec.span(None)
    rec.turn("not a turn")
    rec.audio(None, "x", "user", None)
    rec.end_session(None, "completed")
    rec.span(Span(session_id="s-odd", turn=1, kind="tool", name="t", t_start=T0, t_end=T0,
                  input={"arguments": {"when": object()}}), pii_paths=["", "..", "input.arguments.nothing.here"])
    assert rec.flush(5.0)


def test_nothing_unmasked_reaches_the_disk(env):
    """A shop conversation with dummy personal data; afterwards the raw values are searched for
    in the bytes of every file of the data directory."""
    secrets = {
        "order": "A1001", "phone": "010-9876-5432", "phone_plain": "01098765432", "last4": "7391",
        "email": "minsu.kim@example.com", "rrn": "900101-1234567", "card": "4321-8765-2109-6543",
        "name": "김민수", "address_road": "테헤란로 152", "memo": "현관 비밀번호 공동현관",
    }
    sid = "s-shop"
    rec = trace.get_recorder()
    rec.start_session(meta(sid, test=True, label="refund_small",
                           settings={"note": f"연락처 {secrets['phone']}"}))
    # turn 1: the customer gives the order number and, with no cue at all, the last four digits
    said = f"주문번호는 {secrets['order']}이고요, {secrets['last4']}요."
    rec.span(Span(session_id=sid, turn=1, kind="stt", t_start=T0, t_end=T0 + 0.2,
                  output={"text": said}, attrs={"engine": "whisper", "language": "ko"}))
    rec.span(Span(session_id=sid, turn=1, kind="llm", name="gemma4:26b", t_start=T0 + 0.2, t_end=T0 + 0.9,
                  input={"messages": [{"role": "user", "content": said}]},
                  output={"text": "", "tool_calls": [{"name": "verify_customer", "arguments": {
                      "order_id": secrets["order"], "phone_last4": secrets["last4"]}}]}))
    assert rec.flush(5.0)      # the bare digits are on their way to disk before the tool span names them
    rec.span(Span(session_id=sid, turn=1, kind="tool", name="verify_customer", t_start=T0 + 0.9, t_end=T0 + 1.0,
                  input={"arguments": {"order_id": secrets["order"], "phone_last4": secrets["last4"]}},
                  output={"ok": True, "text": f"{secrets['name']} 고객님 본인 확인이 되었습니다.",
                          "data": {"customer_name": secrets["name"], "memo": secrets["memo"],
                                   "address": f"서울특별시 강남구 {secrets['address_road']}", "amount": 30000}},
                  attrs={"effect": "read", "executed": False}),
             pii_paths=["input.arguments.phone_last4", "output.data.customer_name", "output.data.memo",
                        "output.data.address"])
    rec.span(Span(session_id=sid, turn=1, kind="llm", name="gemma4:26b", t_start=T0 + 1.0, t_end=T0 + 1.6,
                  input={"messages": [
                      {"role": "user", "content": said},
                      {"role": "tool", "content": f'{{"customer_name": "{secrets["name"]}", "memo": '
                                                  f'"{secrets["memo"]}", "amount": 30000}}'}]},
                  output={"text": f"{secrets['name']}님, 확인되었습니다. 메모는 {secrets['memo']}입니다."}))
    rec.turn(TurnRecord(session_id=sid, turn=1, user_text=said,
                        agent_text=f"{secrets['name']}님, 확인되었습니다. 30000원 주문이 2026-11-10에 접수되었습니다.",
                        t_user_end=T0 + 0.2, t_agent_start=T0 + 1.7, t_agent_end=T0 + 4))
    # turn 2: everything else, in free text
    said2 = (f"제 이름은 {secrets['name']}입니다. 전화는 {secrets['phone']} 아니 {secrets['phone_plain']}, "
             f"메일은 {secrets['email']}, 주민번호 {secrets['rrn']}, 카드 {secrets['card']}, "
             f"주소는 서울특별시 강남구 {secrets['address_road']} 3층이에요. 뒤 네 자리는 {secrets['last4']}.")
    rec.span(Span(session_id=sid, turn=2, kind="llm", name="gemma4:26b", t_start=T0 + 5, t_end=T0 + 6,
                  input={"messages": [{"role": "user", "content": said2}]}, output={"text": "네, 알겠습니다."}))
    rec.turn(TurnRecord(session_id=sid, turn=2, user_text=said2, agent_text="네, 알겠습니다. 30000원 환불해 드릴게요.",
                        t_user_end=T0 + 5, t_agent_start=T0 + 6, t_agent_end=T0 + 8))
    rec.end_session(sid, "completed")
    assert rec.flush(5.0)

    trace.get_store().put_document("analysis", sid, {
        "session_id": sid, "agent_id": "shop-support", "version": 1,
        "summary": f"{secrets['name']} 고객님이 {secrets['order']} 주문의 환불을 문의함. 연락처 {secrets['phone']}",
        "extracted": {"customer_name": secrets["name"]}, "criteria": [], "metrics": {}})

    raw = disk_bytes(env)
    assert b"shop-support" in raw and "환불".encode() in raw          # the search does find stored text
    for label, value in secrets.items():
        assert value.encode("utf-8") not in raw, f"{label} is on disk unmasked"
    assert b"9876" not in raw and b"1234567" not in raw

    session = trace.get_store().get_session(sid)
    turn1, turn2 = session["turns"]
    assert "30000원" in turn1["agent_text"] and "2026-11-10" in turn1["agent_text"]     # amounts and dates stay
    assert turn1["user_text"] == "주문번호는 [주문번호]이고요, [가려짐]요."
    for marker in ("[이름]", "[전화번호]", "[이메일]", "[주민번호]", "[카드번호]", "[주소]"):
        assert marker in turn2["user_text"], turn2["user_text"]
    tool = next(s for s in session["spans"] if s["kind"] == "tool")
    assert tool["input"]["arguments"] == {"order_id": "[주문번호]", "phone_last4": "[가려짐]"}
    assert tool["output"]["data"] == {"customer_name": "[이름]", "memo": "[가려짐]", "address": "[가려짐]",
                                      "amount": 30000}
    assert session["meta"]["settings"] == {"note": "연락처 [전화번호]"}
    assert session["analysis"]["session_id"] == sid
    assert session["analysis"]["summary"].startswith("[이름] 고객님이 [주문번호] 주문")


def test_bare_pii_field_names_are_accepted_as_paths(env):
    """A gateway that passes a contract's field names without "input."/"output." still masks."""
    rec = trace.get_recorder()
    rec.start_session(meta("s-bare"))
    rec.span(Span(session_id="s-bare", turn=1, kind="tool", name="lookup", t_start=T0, t_end=T0 + 0.1,
                  input={"arguments": {"secret_word": "무지개떡", "q": "배송"}},
                  output={"ok": True, "text": "ok", "data": {"pet": "달려라하니"}}),
             pii_paths=["secret_word", "data.pet"])
    assert rec.flush(5.0)
    span = trace.get_store().get_session("s-bare")["spans"][0]
    assert span["input"]["arguments"] == {"secret_word": "[가려짐]", "q": "배송"}
    assert span["output"]["data"] == {"pet": "[가려짐]"}
    raw = disk_bytes(env)
    assert "무지개떡".encode() not in raw and "달려라하니".encode() not in raw


def test_caller_changes_after_the_call_do_not_reach_the_record(env):
    rec = trace.get_recorder()
    rec.start_session(meta("s-copy"))
    messages = [{"role": "user", "content": "안녕하세요"}]
    rec.span(Span(session_id="s-copy", turn=1, kind="llm", t_start=T0, t_end=T0 + 1, input={"messages": messages}))
    messages.append({"role": "assistant", "content": "나중에 붙은 말"})
    messages[0]["content"] = "바뀐 말"
    assert rec.flush(5.0)
    stored = trace.get_store().get_session("s-copy")["spans"][0]["input"]["messages"]
    assert stored == [{"role": "user", "content": "안녕하세요"}]


def test_audio_only_for_test_or_simulated_sessions(env):
    rec, store = trace.get_recorder(), trace.get_store()
    pcm = (b"\x01\x02" * 16000)          # one second
    rec.start_session(meta("s-real", channel="voice"))
    rec.start_session(meta("s-test", channel="voice", test=True))
    rec.start_session(meta("s-sim", channel="voice", simulated=True))
    for sid in ("s-real", "s-test", "s-sim", "s-unknown"):
        rec.audio(sid, 1, "user", pcm)
        rec.audio(sid, 1, "agent", pcm)
        rec.audio(sid, 1, "agent", pcm)      # a second piece of the same reply is appended
    assert rec.flush(5.0)
    assert not (env / "audio" / "s-real").exists()
    assert not (env / "audio" / "s-unknown").exists()
    for sid in ("s-test", "s-sim"):
        assert store.list_audio(sid) == [
            {"turn": 1, "role": "user", "bytes": 44 + 32000}, {"turn": 1, "role": "agent", "bytes": 44 + 64000}]
        with wave.open(str(env / "audio" / sid / "1-user.wav"), "rb") as w:
            assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, 16000, 16000)
            assert w.readframes(16000) == pcm
    assert store.get_session("s-real")["audio"] == []


def test_audio_flag_is_read_from_the_store_by_another_recorder(env):
    """A recorder that did not see start_session (another process) asks the store."""
    rec = trace.get_recorder()
    rec.start_session(meta("s-test2", test=True))
    rec.start_session(meta("s-real2"))
    assert rec.flush(5.0)
    other = TraceRecorder(trace.get_store(), auto_purge=False)
    other.audio("s-test2", 1, "user", b"\x00\x00" * 800)
    other.audio("s-real2", 1, "user", b"\x00\x00" * 800)
    assert other.flush(5.0)
    other.close()
    assert (env / "audio" / "s-test2" / "1-user.wav").exists()
    assert not (env / "audio" / "s-real2").exists()


def test_session_status_and_turn_replacement(env):
    rec, store = trace.get_recorder(), trace.get_store()
    rec.start_session(meta("s-life", channel="voice"))
    rec.turn(TurnRecord(session_id="s-life", turn=1, user_text="안녕", agent_text="안녕하세요, 무엇을 도와드릴까요?",
                        t_user_end=T0, t_agent_start=T0 + 1.2))
    assert rec.flush(5.0)
    assert store.get_session("s-life")["status"] == "active"
    # after an interruption the engine records the turn again, cut to what was heard
    rec.turn(TurnRecord(session_id="s-life", turn=1, user_text="안녕", agent_text="안녕하세요,",
                        t_user_end=T0, t_agent_start=T0 + 1.2, interrupted=True))
    rec.end_session("s-life", "handoff")
    assert rec.flush(5.0)
    session = store.get_session("s-life")
    assert session["status"] == "handoff" and session["ended_at"] is not None
    assert len(session["turns"]) == 1
    assert session["turns"][0]["agent_text"] == "안녕하세요," and session["turns"][0]["interrupted"] is True
    assert session["turns"][0]["response_ms"] == 1200.0
