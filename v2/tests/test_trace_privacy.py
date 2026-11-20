"""definition.privacy.store_content false: only what was done is recorded (real conversations only)."""
import runtime_fakes  # noqa: F401

from agentkit.contracts import SessionMeta, Span, TurnRecord
from agentkit.trace.recorder import TraceRecorder, without_content
from agentkit.trace.store import TraceStore


def recorder(tmp_path):
    return TraceRecorder(TraceStore(tmp_path), start=False)


def test_without_content_keeps_what_was_done():
    span = Span(session_id="s", turn=1, kind="tool", name="read_mail", t_start=1, t_end=2,
                input={"arguments": {"ref": "2"}}, output={"ok": True, "text": "본문: 계약서 수정본 보냅니다", "items": 1},
                attrs={"phase": "read", "effect": "read", "query": "from:김민수 계약서"}).model_dump()
    kept = without_content(span)
    assert kept["output"]["ok"] is True and kept["output"]["items"] == 1 and kept["output"]["stored"] is False
    assert "계약서" not in str(kept) and kept["attrs"] == {"phase": "read", "effect": "read"}
    assert kept["name"] == "read_mail" and kept["t_end"] == 2


def test_recorder_strips_turns_spans_and_audio(tmp_path):
    rec = recorder(tmp_path)
    rec.start_session(SessionMeta(session_id="p", agent_id="office", version=2, channel="voice", test=True,
                                  started_at=1.0, store_content=False))
    rec.turn(TurnRecord(session_id="p", turn=1, user_text="김민수 메일 읽어 줘", agent_text="계약서 수정본을 보냈대요",
                        t_user_end=1.0))
    rec.span(Span(session_id="p", turn=1, kind="llm", name="m", t_start=1, t_end=2, input={"messages": [{"content": "메일 본문"}]},
                  output={"text": "계약서 수정본"}))
    rec.audio("p", 1, "user", b"\x01\x00" * 100)
    queued = list(rec._queue.queue)
    turn = next(item[1] for item in queued if item[0] == "turn")
    assert turn["user_text"] == "(내용 저장 안 함 · 11자)" and "계약서" not in turn["agent_text"]
    span = next(item[1] for item in queued if item[0] == "span")
    assert "메일 본문" not in str(span) and "계약서" not in str(span)
    assert not any(item[0] == "audio" for item in queued)


def test_simulated_and_default_sessions_keep_everything(tmp_path):
    rec = recorder(tmp_path)
    rec.start_session(SessionMeta(session_id="d", agent_id="shop", version=1, channel="text", started_at=1.0))
    rec.turn(TurnRecord(session_id="d", turn=1, user_text="환불해 주세요", agent_text="네", t_user_end=1.0))
    assert next(i[1] for i in rec._queue.queue if i[0] == "turn")["user_text"] == "환불해 주세요"


def test_withheld_turns_get_no_text_flags():
    from agentkit.trace.flags import auto_signals
    turns = [{"turn": n, "user_text": "(내용 저장 안 함 · 10자)", "agent_text": "(내용 저장 안 함 · 40자)"} for n in (1, 2, 3)]
    spans = [{"turn": 2, "kind": "stt", "attrs": {"low_confidence": True}}]
    assert auto_signals(turns, spans) == {1: [], 2: ["low_confidence"], 3: []}


async def test_private_engine_logs_no_content(caplog):
    import logging
    from agentkit.runtime import Engine
    from runtime_fakes import FakeGateway, FakeRecorder, ScriptedLLM, collect, contract, fixed_now, make_definition, text, tool
    gateway = FakeGateway([contract("search_mail", properties={"query": {"type": "string"}}, required=["query"])])
    definition = make_definition(tools=["search_mail"], privacy={"store_content": False})
    engine = Engine(definition, gateway, FakeRecorder(), ScriptedLLM([[tool("search_mail", query="김민수 계약서")], [text("찾았어요.")]]),
                    now=fixed_now)
    with caplog.at_level(logging.INFO, logger="agentkit.runtime"):
        await collect(engine, "김민수 계약서 메일 찾아줘")
    assert "search_mail" in caplog.text and "김민수" not in caplog.text and "<7자>" in caplog.text
