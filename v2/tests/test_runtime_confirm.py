"""Confirmation questions (two-phase write tools) settled by the engine, and guards that count only writes
that really ran. With the real gateway and the dummy shop, and the scripted LLM."""
import runtime_fakes  # noqa: F401  (sets AGENTKIT_DATA_DIR before agentkit.config is read)

from agentkit.runtime import Engine
from agentkit.tools import build_gateway
from runtime_fakes import FakeRecorder, MiniShop, ScriptedLLM, collect, fixed_now, make_definition, text, tool

ASK = "무선 이어폰 32000원 환불을 접수할까요?"
NO_FAKE_REFUND = {"id": "no_fake_refund", "kind": "claims_lookup",
                  "pattern": "환불\\S{0,3}\\s?(접수|처리)\\S{0,2}\\s?(됐|되었|완료)", "unless_tools": ["request_refund"],
                  "nudge": "(시스템 안내: request_refund가 실행되지 않았는데 환불이 접수된 것처럼 말하려 했습니다.)"}


def shop(scripts, **definition):
    llm, gateway, recorder = ScriptedLLM(scripts), build_gateway(), FakeRecorder()
    engine = Engine(make_definition(**definition), gateway, recorder, llm, now=fixed_now)
    return engine, llm, gateway


def tool_events(events):
    return [e.data for e in events if e.type == "tool" and e.data.get("state") == "done"]


FIRST_TURN = [
    [tool("verify_customer", order_id="A1001", phone_last4="1234"), tool("get_order", order_id="A1001"),
     tool("request_refund", order_id="A1001", reason="단순 변심")],
    [text(ASK)],
]


async def test_a_yes_runs_the_request_without_the_model_calling_it_again():
    engine, llm, gateway = shop(FIRST_TURN + [[text("환불이 접수되었습니다.")]])
    await collect(engine, "A1001 환불해 주세요. 뒤 네 자리 1234, 단순 변심이에요.")
    assert len(gateway.pending(engine.session_id)) == 1
    events = await collect(engine, "네")
    done = tool_events(events)
    assert [d["name"] for d in done] == ["request_refund"] and done[0].get("executed")
    assert gateway.pending(engine.session_id) == []
    assert events[-1].text == "환불이 접수되었습니다."          # and the guard let the true claim through


async def test_an_unclear_answer_keeps_the_request_open_for_the_next_yes():
    engine, llm, gateway = shop(FIRST_TURN + [[text("네, 사유 확인했습니다. " + ASK)], [text("환불이 접수되었습니다.")]])
    await collect(engine, "A1001 환불해 주세요. 뒤 네 자리 1234, 단순 변심이에요.")
    events = await collect(engine, "소리가 생각보다 작아서요.")
    done = tool_events(events)
    assert [d["name"] for d in done] == ["request_refund"] and not done[0].get("executed")
    assert len(gateway.pending(engine.session_id)) == 1          # still waiting, not expired
    events = await collect(engine, "네, 접수해 주세요.")
    assert tool_events(events)[0].get("executed")


async def test_a_no_cancels_the_request():
    engine, llm, gateway = shop(FIRST_TURN + [[text("환불을 접수하지 않았습니다.")]])
    await collect(engine, "A1001 환불해 주세요. 뒤 네 자리 1234, 단순 변심이에요.")
    events = await collect(engine, "아니요, 그냥 쓸게요.")
    done = tool_events(events)
    assert [d["name"] for d in done] == ["request_refund"] and not done[0].get("executed")
    assert gateway.pending(engine.session_id) == []


async def test_nothing_is_settled_two_turns_later_or_without_a_question():
    engine, llm, gateway = shop([[text("주문번호와 휴대폰 뒤 4자리를 알려 주세요.")]])
    events = await collect(engine, "환불하고 싶어요")
    assert tool_events(events) == []


async def test_a_speculative_reply_never_settles_it():
    engine, llm, gateway = shop(FIRST_TURN + [[text("잠시만요.")]])
    await collect(engine, "A1001 환불해 주세요. 뒤 네 자리 1234, 단순 변심이에요.")
    events = await collect(engine, "네", speculative=True)
    assert all(d["name"] != "request_refund" or not d.get("executed") for d in tool_events(events))


async def test_a_refund_that_only_asked_does_not_let_the_done_claim_through():
    # the model calls the write tool and, in the same reply, says it was filed: it was not
    llm = ScriptedLLM([[tool("verify_customer", order_id="A1001", phone_last4="1234"),
                        tool("request_refund", order_id="A1001", reason="단순 변심")],
                       [text("환불이 접수되었습니다.")],
                       [text("무선 이어폰 30000원 환불을 접수할까요?")]])
    engine = Engine(make_definition(guards=[NO_FAKE_REFUND]), MiniShop(), FakeRecorder(), llm, now=fixed_now)
    events = await collect(engine, "A1001 환불해 주세요. 뒤 네 자리 1234")
    assert any(e.type == "replace" and e.data.get("guard_id") == "no_fake_refund" for e in events)
    assert "접수되었습니다" not in events[-1].text


CONFIRM_THROUGH_TOOL = {"id": "confirm_through_tool", "kind": "claims_lookup",
                        "pattern": "(접수|환불)(을|를)?\\s?(해 드릴|할)까요", "unless_tools": ["request_refund"],
                        "nudge": "(시스템 안내: 확인 문장은 request_refund가 만듭니다. request_refund를 호출하세요.)"}


async def test_the_tools_own_confirmation_question_is_allowed_after_it_asked():
    llm = ScriptedLLM([[tool("verify_customer", order_id="A1001", phone_last4="1234"),
                        tool("request_refund", order_id="A1001", reason="단순 변심")],
                       [text("무선 이어폰 30000원 환불을 접수할까요?")]])
    engine = Engine(make_definition(guards=[CONFIRM_THROUGH_TOOL, NO_FAKE_REFUND]), MiniShop(), FakeRecorder(), llm,
                    now=fixed_now)
    events = await collect(engine, "A1001 환불해 주세요. 뒤 네 자리 1234")
    assert not any(e.type == "replace" for e in events)
    assert events[-1].text == "무선 이어폰 30000원 환불을 접수할까요?"


async def test_asking_for_consent_without_the_tool_is_stopped():
    llm = ScriptedLLM([[tool("verify_customer", order_id="A1001", phone_last4="1234")],
                       [text("확인되었습니다. 이 주문 환불을 접수할까요?")],
                       [tool("request_refund", order_id="A1001", reason="단순 변심")],
                       [text("무선 이어폰 30000원 환불을 접수할까요?")]])
    engine = Engine(make_definition(guards=[CONFIRM_THROUGH_TOOL]), MiniShop(), FakeRecorder(), llm, now=fixed_now)
    events = await collect(engine, "A1001 환불해 주세요. 뒤 네 자리 1234")
    assert any(e.type == "replace" and e.data.get("guard_id") == "confirm_through_tool" for e in events)
    assert events[-1].text.endswith("무선 이어폰 30000원 환불을 접수할까요?")


async def test_after_an_unclear_answer_the_question_is_asked_again_if_the_model_does_not():
    engine, llm, gateway = shop(FIRST_TURN + [[text("소리가 작아서 불편하셨군요.")]])
    await collect(engine, "A1001 환불해 주세요. 뒤 네 자리 1234, 단순 변심이에요.")
    events = await collect(engine, "소리가 생각보다 작아요.")
    assert events[-1].text == "소리가 작아서 불편하셨군요. " + ASK
    assert [e.text for e in events if e.type == "piece"][-1] == ASK
    told = [m for m in llm.calls[-1]["messages"] if m["role"] == "tool"][-1]["content"]
    assert "취소되지 않고" in told                                # the model is told the request still waits
