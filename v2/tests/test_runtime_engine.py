"""The engine with a scripted LLM, a fake gateway and a fake recorder: fast, no services."""
import asyncio

import pytest

import runtime_fakes  # noqa: F401  (sets AGENTKIT_DATA_DIR before agentkit.config is read)

from agentkit.contracts import ToolResult
from agentkit.runtime import Engine, TextConversation
from runtime_fakes import (SHOP_CONTRACTS, FakeGateway, FakeKnowledge, FakeRecorder, ScriptedLLM, collect,
                           contract, done_text, fixed_now, make_definition, text, tool, types)


def build(scripts=None, *, definition=None, handlers=None, contracts=None, **options):
    llm = options.pop("llm", None) or ScriptedLLM(scripts)
    gateway = FakeGateway(contracts or SHOP_CONTRACTS, handlers)
    recorder = FakeRecorder()
    engine = Engine(definition or make_definition(), gateway, recorder, llm, now=fixed_now, **options)
    return engine, llm, gateway, recorder


# ------------------------------------------------------------------------------------ prompt

async def test_prompt_order_and_content():
    engine, llm, _, _ = build([[text("안녕하세요, 무엇을 도와드릴까요?")]],
                              definition=make_definition(live_clock=True))
    await collect(engine, "안녕하세요")
    system = llm.system()
    marks = ["온라인 쇼핑몰의 주문 상담원", "답변 길이:", "말하기 규칙:", "반드시 지킬 순서:",
             "절대 하지 않는 일:", "상담원에게 넘기는 경우:", "도구 사용 규칙:", "오늘은 2026년 11월 17일 화요일"]
    positions = [system.index(mark) for mark in marks]
    assert positions == sorted(positions), system
    assert system.startswith("당신은 온라인 쇼핑몰의 주문 상담원")   # the persona is the head
    assert "한두 문장" in system                                # reply_length short
    assert "마크다운" in system and "이모지" in system
    assert "- 주문 내용을 말하기 전에 주문번호와 휴대폰 뒤 4자리로 본인을 확인한다" in system     # flow
    assert "- 환불은 상품명과 금액을 읽어 주고 동의를 받은 뒤 접수한다" in system
    assert "- 본인 확인 전에 주문 내용, 주소, 결제 정보를 말하지 않는다" in system              # prohibition
    assert "- 환불 금액이 100000원을 넘는다" in system and "handoff_to_human 도구를 호출" in system
    assert "추측하거나 지어내지 말고" in system
    # the two-phase rule names the write tools that have a confirmation sentence, and only those
    assert "변경 도구(request_refund)는 두 단계로" in system
    assert "같은 인자로 그 도구를 다시 호출" in system and "동의" in system
    # date and calendar in the system prompt; the minute goes in front of the user's words
    assert "11월 18일은 수요일(내일, 이번 주 수요일)" in system and "오후 3시 5분" not in system
    assert llm.last_user() == "[지금 시각: 11월 17일 화요일 오후 3시 5분]\n안녕하세요"
    assert engine.history[0] == {"role": "user", "content": "안녕하세요"}     # the clock is not stored
    # the tools offered are exactly the definition's
    assert [spec["function"]["name"] for spec in llm.calls[0]["tools"]] == make_definition().tools


async def test_prompt_without_optional_parts():
    definition = make_definition(tools=[], flow=[], handoff=[], prohibitions=[], live_clock=False)
    engine, llm, _, _ = build([[text("네, 안녕하세요.")]], definition=definition)
    await collect(engine, "안녕")
    system = llm.system()
    for absent in ("반드시 지킬 순서", "절대 하지 않는 일", "상담원에게 넘기는 경우", "도구 사용 규칙", "오늘은"):
        assert absent not in system
    assert llm.calls[0]["tools"] is None
    assert llm.last_user() == "안녕"


async def test_prompt_only_from_definition():
    """R-DEF-5: another definition, another agent; nothing of the first one is left in the prompt."""
    definition = make_definition(
        id="drive-thru", persona="당신은 드라이브스루 주문을 받는 직원입니다.", tools=["get_menu"],
        flow=[{"id": "repeat", "rule": "주문을 받은 뒤 전체 주문을 다시 읽어 준다"}],
        handoff=[], prohibitions=["메뉴에 없는 상품을 권하지 않는다"])
    engine, llm, _, _ = build([[text("어서 오세요, 주문하시겠어요?")]], definition=definition,
                              contracts=[contract("get_menu", filler="메뉴를 볼게요.")])
    await collect(engine, "안녕하세요")
    system = llm.system()
    assert system.startswith("당신은 드라이브스루 주문을 받는 직원입니다.")
    assert "전체 주문을 다시 읽어 준다" in system and "메뉴에 없는 상품" in system
    for absent in ("쇼핑몰", "환불", "본인", "변경 도구", "handoff_to_human", "상담원"):
        assert absent not in system
    assert [spec["function"]["name"] for spec in llm.calls[0]["tools"]] == ["get_menu"]


async def test_language_en_and_auto():
    english = make_definition(language="en", persona="You are a support agent for an online shop.", live_clock=True)
    engine, llm, _, _ = build([[text("Hello, how can I help?")]], definition=english)
    await collect(engine, "안녕하세요")                     # fixed "en": the user's language does not matter
    system = llm.system()
    for mark in ("Reply length:", "Speaking rules:", "Order you must always follow:", "Things you never do:",
                 "When to hand over to a human agent:", "Rules for using tools:", "Today is Tuesday, November 17"):
        assert mark in system
    assert "IMPORTANT: this conversation is in English" not in system      # the persona is English already
    assert llm.last_user().startswith("[Current time: Tuesday, November 17, 3:05 PM]")

    auto = make_definition(language="auto")
    engine, llm, gateway, _ = build([[text("Hello there.")], [text("네, 안녕하세요.")],
                                     [tool("get_order", order_id="A1001")], [text("Done.")]], definition=auto)
    await collect(engine, "Hi, can you help me?")
    assert "Speaking rules:" in llm.system() and "IMPORTANT: this conversation is in English" in llm.system()
    await collect(engine, "한국어로 할게요")
    assert "말하기 규칙:" in llm.system() and "Speaking rules:" not in llm.system()
    await collect(engine, "check order A1001", language="ko")          # the channel heard Korean: that wins
    assert "말하기 규칙:" in llm.system()
    assert gateway.calls[-1][0].language == "ko"


async def test_reply_length_setting_and_one_turn_override():
    engine, llm, _, _ = build([[text("네.")], [text("네.")], [text("네.")], [text("네.")]],
                              definition=make_definition(reply_length="normal"))
    await collect(engine, "배송은 보통 얼마나 걸려요?")
    assert "서너 문장" in llm.system()
    await collect(engine, "자세히 설명해 주세요")
    assert "예닐곱 문장" in llm.system()
    await collect(engine, "짧게 말해 줘요")
    assert "한두 문장" in llm.system()
    await collect(engine, "그렇군요")                        # back to the setting
    assert "서너 문장" in llm.system()
    engine.set_options(reply_length="explain", language="en")   # channel settings override the definition
    await collect(engine, "and now?")
    assert "ten sentences or more" in llm.system()
    assert llm.calls[-1]["num_ctx"] == 16384 and llm.calls[-1]["num_predict"] == 2500


# ------------------------------------------------------------------------------------ tools

async def test_tool_round_trip():
    def get_order(ctx, arguments):
        return ToolResult(ok=True, text="주문 A1001: 무선 이어폰 30000원, 배송 중.", ms=12.0, call_id="c1")

    engine, llm, gateway, _ = build(
        [[tool("get_order", order_id="A1001")], [text("무선 이어폰 주문은 지금 배송 중입니다.")]],
        handlers={"get_order": get_order}, test=True)
    events = await collect(engine, "주문 A1001 어디쯤 왔어요?")

    ctx, name, arguments = gateway.calls[0]
    assert (name, arguments) == ("get_order", {"order_id": "A1001"})
    assert (ctx.session_id, ctx.agent_id, ctx.version) == (engine.session_id, "test-shop", 3)
    assert ctx.turn == 1 and ctx.last_user_text == "주문 A1001 어디쯤 왔어요?"
    assert ctx.speculative is False and ctx.test is True and ctx.language == "ko"

    kinds = types(events)
    assert kinds.index("filler") < kinds.index("tool")           # something is said before the call
    filler = next(e for e in events if e.type == "filler")
    assert filler.text == "주문을 확인해 볼게요."                    # the contract's own line
    start, done = [e.data for e in events if e.type == "tool"]
    assert start["state"] == "start" and start["name"] == "get_order" and start["arguments"] == {"order_id": "A1001"}
    assert done["state"] == "done" and done["ok"] is True and done["effect"] == "read"
    assert done["executed"] is False and done["needs_confirmation"] is False and done["error_type"] is None
    assert done["result"].startswith("주문 A1001") and done["ms"] == 12.0

    # the second pass sees the call and its result, in that order, after the user's message
    second = llm.calls[1]["messages"]
    assert [m["role"] for m in second] == ["system", "user", "assistant", "tool"]
    assert second[2]["tool_calls"] == [{"function": {"name": "get_order", "arguments": {"order_id": "A1001"}}}]
    assert second[3] == {"role": "tool", "tool_name": "get_order", "content": "주문 A1001: 무선 이어폰 30000원, 배송 중."}
    assert done_text(events) == "무선 이어폰 주문은 지금 배송 중입니다."
    assert [m["role"] for m in engine.history] == ["user", "assistant", "tool", "assistant"]

    await collect(engine, "고마워요")
    assert gateway.calls[0][0].turn == 1 and engine.turn == 2


async def test_default_filler_and_failure_fields():
    def get_order(ctx, arguments):
        return ToolResult(ok=False, text="주문번호 A9999를 찾지 못했습니다. 번호를 다시 확인해 달라고 하세요.",
                          error_type="not_found")

    contracts = [contract("get_order")]          # no filler in the contract
    engine, llm, _, _ = build([[tool("get_order", order_id="A9999")], [text("주문번호를 다시 확인해 주시겠어요?")]],
                              definition=make_definition(tools=["get_order"]), contracts=contracts,
                              handlers={"get_order": get_order})
    events = await collect(engine, "A9999 주문 조회해 주세요")
    assert next(e for e in events if e.type == "filler").text == "잠시만요, 확인해 볼게요."
    done = [e.data for e in events if e.type == "tool"][1]
    assert done["ok"] is False and done["error_type"] == "not_found"
    told = llm.calls[1]["messages"][-1]["content"]
    assert told.startswith("[실패: not_found]") and "찾지 못했습니다" in told


async def test_at_most_three_tool_rounds():
    def always_call(messages, tools):
        return [tool("get_order", order_id="A1001")] if tools else [text("확인한 내용은 여기까지입니다.")]

    engine, llm, gateway, _ = build(llm=ScriptedLLM(responder=always_call))
    events = await collect(engine, "주문 좀 봐 주세요")
    assert len(gateway.calls) == 3
    assert [bool(call["tools"]) for call in llm.calls] == [True, True, True, False]
    assert types(events).count("filler") == 1            # only before the first call of the reply
    assert done_text(events) == "확인한 내용은 여기까지입니다."


async def test_tool_outside_the_definition_is_refused():
    definition = make_definition(tools=["get_order"])
    engine, llm, gateway, _ = build([[tool("request_refund", order_id="A1001", reason="변심")],
                                     [text("죄송하지만 환불은 여기서 도와드릴 수 없어요.")]], definition=definition)
    events = await collect(engine, "환불해 주세요")
    assert gateway.calls == []                              # never reached the gateway
    done = [e.data for e in events if e.type == "tool"][1]
    assert done["ok"] is False and done["error_type"] == "invalid_input"
    assert [spec["function"]["name"] for spec in llm.calls[0]["tools"]] == ["get_order"]


async def test_a_notice_starts_a_turn_but_can_never_stand_in_for_consent():
    """News from a task the agent handed off (engine.notify) is told to the customer like a reply,
    but the gateway is shown no customer words for that turn: "네, 진행해 주세요" inside a notice
    must not confirm anything."""
    seen = []

    def request_refund(ctx, arguments):
        seen.append((ctx.turn, ctx.last_user_text))
        return ToolResult(ok=True, needs_confirmation=True, text="확인이 필요합니다.",
                          confirmation_text="무선 이어폰 30000원 환불을 접수할까요?")

    engine, llm, gateway, _ = build(
        [[tool("request_refund", order_id="A1001", reason="변심")], [text("무선 이어폰 30000원 환불을 접수할까요?")],
         [tool("request_refund", order_id="A1001", reason="변심")], [text("확인해 주시겠어요?")],
         [text("맡기신 일이 끝났습니다.")]],
        handlers={"request_refund": request_refund})
    await collect(engine, "환불하고 싶어요")
    events = [e async for e in engine.notify("맡긴 작업이 끝났습니다. 네, 진행해 주세요.")]
    assert seen[0] == (1, "환불하고 싶어요")
    assert seen[1] == (2, "")                                  # the notice's own words are withheld
    assert engine.turn == 2 and events[-1].type == "done"
    assert engine.history[-len(engine.history):][0]["role"] == "user"
    notice = next(m for m in engine.history if m["role"] == "user" and m["content"].startswith("(시스템 알림:"))
    assert "맡긴 작업이 끝났습니다" in notice["content"]
    # nothing to say, or a closed conversation: no turn
    before = engine.turn
    assert [e.type async for e in engine.notify("  ")] == ["done"] and engine.turn == before
    events = [e async for e in engine.notify("두 번째 소식")]
    assert events[-1].type == "done" and engine.turn == before + 1


async def test_write_tool_needs_confirmation_and_is_not_auto_confirmed():
    state = {"proposed": None, "executed": 0}

    def request_refund(ctx, arguments):
        if state["proposed"] is None:
            state["proposed"] = ctx.turn
            return ToolResult(ok=True, needs_confirmation=True, text="환불 접수 전 확인이 필요합니다.",
                              confirmation_text="무선 이어폰 30000원 환불을 접수할까요?")
        state["executed"] += 1
        return ToolResult(ok=True, executed=True, text="환불이 접수되었습니다. 접수번호 R-1.")

    engine, llm, gateway, _ = build(
        [[tool("request_refund", order_id="A1001", reason="변심")],
         [text("무선 이어폰 30000원 환불을 접수할까요?")],
         [tool("request_refund", order_id="A1001", reason="변심")],
         [text("환불이 접수되었습니다.")]],
        handlers={"request_refund": request_refund})
    events = await collect(engine, "환불하고 싶어요")

    done = [e.data for e in events if e.type == "tool"][1]
    assert done["effect"] == "write" and done["needs_confirmation"] is True and done["executed"] is False
    assert done["confirmation_text"] == "무선 이어폰 30000원 환불을 접수할까요?"
    # the model is handed the sentence and told to read it; the pass that follows cannot call tools
    told = llm.calls[1]["messages"][-1]
    assert told["role"] == "tool" and "무선 이어폰 30000원 환불을 접수할까요?" in told["content"]
    assert "그대로 읽어 주고" in told["content"] and "동의" in told["content"]
    assert llm.calls[1]["tools"] is None
    assert len(gateway.calls) == 1 and state["executed"] == 0          # the engine did not confirm by itself
    assert done_text(events) == "무선 이어폰 30000원 환불을 접수할까요?"

    events = await collect(engine, "네, 접수해 주세요")
    ctx = gateway.calls[1][0]
    assert ctx.turn == 2 and ctx.last_user_text == "네, 접수해 주세요"
    done = [e.data for e in events if e.type == "tool"][1]
    assert done["executed"] is True and state["executed"] == 1
    assert next(e for e in events if e.type == "filler").text == "환불을 접수하고 있어요."


async def test_handoff():
    def handoff(ctx, arguments):
        return ToolResult(ok=True, executed=True, text="상담원 연결을 접수했습니다.")

    engine, llm, gateway, recorder = build(
        [[tool("handoff_to_human", reason="환불 금액이 100000원을 넘는다")],
         [text("상담원에게 연결해 드리겠습니다.")]],
        handlers={"handoff_to_human": handoff})
    events = await collect(engine, "25만 원짜리 주문 환불해 주세요")
    event = next(e for e in events if e.type == "handoff")
    assert event.data == {"rule_id": "large_refund", "reason": "환불 금액이 100000원을 넘는다"}
    assert engine.handed_off is True
    assert llm.calls[1]["tools"] is None                    # after the handover nothing more is called
    assert done_text(events) == "상담원에게 연결해 드리겠습니다."
    span = recorder.of("handoff")[0]
    assert span.attrs == {"rule_id": "large_refund", "reason": "환불 금액이 100000원을 넘는다"} and span.turn == 1
    assert recorder.turns[-1].handoff is True

    calls_before = len(llm.calls)
    events = await collect(engine, "그럼 배송은 언제 와요?")
    assert done_text(events) == "상담원에게 연결되었습니다. 잠시만 기다려 주세요."
    assert types(events) == ["delta", "piece", "done"]
    assert len(llm.calls) == calls_before and len(gateway.calls) == 1     # no model, no tools any more
    assert recorder.turns[-1].turn == 2 and recorder.turns[-1].handoff is True
    await engine.close()
    assert recorder.ended == [(engine.session_id, "handoff")]


async def test_a_handoff_rule_can_fix_what_the_customer_hears():
    """definition handoff[].say: the model is told to say exactly that (the emergency number, for one)."""
    def handoff(ctx, arguments):
        return ToolResult(ok=True, executed=True, text="직원 연결을 접수했습니다.")

    say = "응급 상황일 수 있습니다. 지금 바로 119에 전화하세요."
    definition = make_definition(tools=["handoff_to_human"], handoff=[
        {"id": "emergency", "when": "가슴 통증, 호흡 곤란 같은 응급 증상을 말한다", "say": say},
        {"id": "billing", "when": "진료비나 보험 청구를 문의한다"}])
    for reason, expected in (("가슴 통증과 호흡 곤란을 말함", "다음 문장을 그대로 말하세요: \"" + say + "\""),
                             ("진료비 문의", "상담원에게 연결한다고 한 문장으로 알리세요")):
        engine, llm, gateway, recorder = build(
            [[tool("handoff_to_human", reason=reason)], [text("네.")]],
            handlers={"handoff_to_human": handoff}, definition=definition)
        await collect(engine, "도와주세요")
        note = next(m["content"] for m in llm.calls[1]["messages"] if m.get("role") == "tool")
        assert expected in note and ("119" in note) == (reason.startswith("가슴"))
        await engine.close()


# ------------------------------------------------------------------------------------ guards

FORBIDDEN = {"id": "no_guarantee", "kind": "regex", "pattern": "무조건|100퍼센트",
             "nudge": "(시스템 안내: 보장하는 표현을 쓰지 마세요.)"}
CLAIM = {"id": "no_fake_lookup", "kind": "claims_lookup", "pattern": r"(확인|조회|찾아)\S{0,2}\s?(보니|봤더니|본 결과)",
         "unless_tools": ["get_order", "verify_customer"],
         "nudge": "(시스템 안내: 조회하지 않고 조회한 것처럼 말하려 했습니다. 필요하면 도구를 호출하세요.)"}


async def test_guard_regenerates_once_with_the_nudge():
    engine, llm, _, recorder = build(
        [[text("네, 안내해 드리겠습니다. "), text("내일까지 무조건 도착합니다. "), text("걱정하지 마세요.")],
         [text("보통 이틀 안에 도착합니다.")]],
        definition=make_definition(guards=[FORBIDDEN]))
    events = await collect(engine, "언제 와요?")
    pieces = [e.text for e in events if e.type == "piece"]
    assert pieces == ["네, 안내해 드리겠습니다.", "보통 이틀 안에 도착합니다."]
    replace = next(e for e in events if e.type == "replace")
    assert replace.text == "네, 안내해 드리겠습니다. "             # what stays after the withdrawal
    assert types(events).index("piece") < types(events).index("replace")     # the innocent sentence was spoken
    assert replace.data == {"guard_id": "no_guarantee", "action": "regenerated"}
    # the withdrawn sentence was shown as a delta, never cleared for speech
    assert any("무조건" in e.text for e in events if e.type == "delta")
    assert not any("무조건" in piece for piece in pieces)
    assert "걱정하지" not in done_text(events)                   # the rest of the withdrawn pass never ran
    # the regeneration got what was already said, then the nudge
    again = llm.calls[1]["messages"]
    assert again[-2] == {"role": "assistant", "content": "네, 안내해 드리겠습니다."}
    assert again[-1]["role"] == "user" and again[-1]["content"].startswith("(시스템 안내: 보장하는 표현을 쓰지 마세요.)")
    assert done_text(events) == "네, 안내해 드리겠습니다. 보통 이틀 안에 도착합니다."
    assert engine.history[-1]["content"] == done_text(events)
    assert not any("시스템 안내" in m["content"] for m in engine.history)     # the nudge is not kept
    span = recorder.of("guard")[0]
    assert span.input == {"sentence": "내일까지 무조건 도착합니다."}
    assert span.attrs == {"guard_id": "no_guarantee", "action": "regenerated"} and span.name == "no_guarantee"


async def test_guard_drops_the_sentence_the_second_time():
    engine, llm, _, recorder = build(
        [[text("내일까지 무조건 도착합니다. "), text("조금만 기다려 주세요.")],
         [text("배송은 순서대로 나가고 있습니다. "), text("100퍼센트 내일 도착합니다. "), text("운송장 번호를 알려 드릴까요?")]],
        definition=make_definition(guards=[FORBIDDEN]))
    events = await collect(engine, "언제 와요?")
    assert [e.data["action"] for e in events if e.type == "replace"] == ["regenerated", "dropped"]
    assert [e.text for e in events if e.type == "piece"] == ["배송은 순서대로 나가고 있습니다.", "운송장 번호를 알려 드릴까요?"]
    assert done_text(events) == "배송은 순서대로 나가고 있습니다. 운송장 번호를 알려 드릴까요?"
    assert len(llm.calls) == 2                                # one regeneration, not two
    assert llm.calls[1]["messages"][-1]["content"] == "(시스템 안내: 보장하는 표현을 쓰지 마세요.)"
    assert [s.attrs["action"] for s in recorder.of("guard")] == ["regenerated", "dropped"]
    assert recorder.of("guard")[1].input == {"sentence": "100퍼센트 내일 도착합니다."}


async def test_guard_checks_the_last_sentence_too():
    engine, llm, _, recorder = build([[text("무조건 내일 도착해요")], [text("무조건 도착해요")]],
                                     definition=make_definition(guards=[FORBIDDEN]))
    events = await collect(engine, "언제 와요?")
    assert [e.type for e in events if e.type == "piece"] == ["piece"]       # only the fallback line was spoken
    assert "무조건" not in done_text(events)
    assert [s.attrs["action"] for s in recorder.of("guard")] == ["regenerated", "dropped"]


async def test_claims_lookup_guard():
    # no lookup in this reply: "조회해 보니" is a claim
    engine, llm, gateway, recorder = build(
        [[text("조회해 보니 배송 중입니다. "), text("내일 도착할 예정이에요.")],
         [tool("get_order", order_id="A1001")],
         [text("조회해 보니 배송 중입니다. "), text("내일 도착할 예정이에요.")]],
        definition=make_definition(guards=[CLAIM]))
    events = await collect(engine, "A1001 어디쯤이에요?")
    assert [s.attrs["action"] for s in recorder.of("guard")] == ["regenerated"]
    assert "필요하면 도구를 호출하세요" in llm.calls[1]["messages"][-1]["content"]
    assert len(gateway.calls) == 1
    # after the real lookup the same sentence is true and goes through
    assert done_text(events) == "조회해 보니 배송 중입니다. 내일 도착할 예정이에요."

    # a tool that is not in unless_tools does not excuse the claim
    engine, llm, gateway, recorder = build(
        [[tool("handoff_to_human", reason="고객 요청")], [text("확인해 보니 상담원이 대기 중입니다. "), text("곧 연결됩니다.")],
         [text("상담원에게 연결해 드릴게요.")]],
        definition=make_definition(guards=[CLAIM]),
        handlers={"handoff_to_human": lambda ctx, a: ToolResult(ok=True, executed=True, text="접수")})
    events = await collect(engine, "상담원 바꿔 주세요")
    assert [s.attrs["guard_id"] for s in recorder.of("guard")] == ["no_fake_lookup"]
    assert done_text(events) == "상담원에게 연결해 드릴게요."


async def test_bad_guard_pattern_is_skipped():
    broken = {"id": "broken", "kind": "regex", "pattern": "(", "nudge": "x"}
    engine, _, _, _ = build([[text("무조건 됩니다.")]], definition=make_definition(guards=[broken]))
    assert done_text(await collect(engine, "돼요?")) == "무조건 됩니다."


# ------------------------------------------------------------------------------------ cancelling

async def test_cancelled_speculative_reply_leaves_nothing():
    engine, llm, gateway, recorder = build(
        [[tool("get_order", order_id="A1001")], [text("주문은 지금 배송 중입니다. "), 5.0, text("내일 도착합니다.")]])
    await collect_first_turn(engine)
    history_before = [dict(m) for m in engine.history]
    turns_before, spans_before = len(recorder.turns), len(recorder.spans)

    seen = []

    async def consume():
        async for event in engine.reply("그 주문 어디쯤", speculative=True):
            seen.append(event)

    task = asyncio.create_task(consume())
    while not any(e.type == "piece" for e in seen):
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert gateway.calls[-1][0].speculative is True and gateway.calls[-1][0].turn == 2
    assert engine.history == history_before                    # user message, tool exchange, reply: all gone
    assert engine.turn == 1
    assert len(recorder.turns) == turns_before                  # no turn record
    assert len(recorder.spans) == spans_before                  # and none of the engine's spans
    # the conversation goes on as if it had not happened
    llm.scripts.append([text("네, 말씀하세요.")])
    events = await collect(engine, "그 주문 어디쯤 왔는지 알려 주세요")
    assert engine.turn == 2 and recorder.turns[-1].turn == 2
    assert [m["role"] for m in llm.calls[-1]["messages"]] == ["system", "user", "assistant", "user"]
    assert done_text(events) == "네, 말씀하세요."


async def collect_first_turn(engine):
    engine.llm.scripts.insert(0, [text("안녕하세요, 무엇을 도와드릴까요?")])
    await collect(engine, "안녕하세요")


async def test_speculative_reply_dropped_by_aclose_and_by_the_next_turn():
    engine, llm, _, recorder = build([[text("첫 번째 문장을 말하고 있습니다. "), text("두 번째 문장입니다.")]])
    iterator = engine.reply("여보세요", speculative=True)
    async for event in iterator:
        if event.type == "piece":
            break
    await iterator.aclose()
    assert engine.history == [] and engine.turn == 0 and recorder.turns == [] and recorder.spans == []

    # the consumer just stops listening: the next turn clears the abandoned one away
    llm.scripts[:] = [[text("첫 번째 문장을 말하고 있습니다. "), text("두 번째 문장입니다.")], [text("네, 듣고 있어요.")]]
    abandoned = engine.reply("여보세요", speculative=True)
    async for event in abandoned:
        if event.type == "piece":
            break
    events = await collect(engine, "여보세요, 들리세요?")
    assert engine.turn == 1 and [m["content"] for m in engine.history] == ["여보세요, 들리세요?", "네, 듣고 있어요."]
    assert [r.user_text for r in recorder.turns] == ["여보세요, 들리세요?"]
    assert done_text(events) == "네, 듣고 있어요."
    await abandoned.aclose()
    assert engine.turn == 1 and len(engine.history) == 2       # closing it late changes nothing


async def test_completed_speculative_reply_is_kept():
    engine, llm, gateway, recorder = build(
        [[tool("get_order", order_id="A1001")], [text("주문은 지금 배송 중입니다.")]])
    events = await collect(engine, "주문 어디쯤이에요", speculative=True)
    assert done_text(events) == "주문은 지금 배송 중입니다."
    assert engine.turn == 1 and len(engine.history) == 4
    assert len(recorder.turns) == 1 and recorder.kinds().count("llm") == 2     # spans released on completion
    assert gateway.calls[0][0].speculative is True


async def test_a_committed_speculative_reply_calls_its_later_tools_for_real():
    """The voice channel commits a prepared reply once the pause proves to be the end of the turn.
    A tool the reply calls after that runs on final words: it must not be refused as speculative
    (the model then told the customer an order was placed that never was)."""
    engine, llm, gateway, recorder = build(
        [[tool("get_order", order_id="A1001")], [0.3, tool("request_refund", order_id="A1001")],
         [text("환불을 접수할까요?")]])
    seen = []

    async def consume():
        async for event in engine.reply("A1001 환불해 주세요", speculative=True):
            seen.append(event)

    task = asyncio.create_task(consume())
    while len(gateway.calls) < 1:
        await asyncio.sleep(0.01)
    spans_before = len(recorder.spans)
    assert engine.commit_speculation() is True
    assert len(recorder.spans) > spans_before                   # the held-back spans went out
    await task
    assert [(name, ctx.speculative) for ctx, name, _ in gateway.calls] == [("get_order", True), ("request_refund", False)]
    assert engine.commit_speculation() is False                 # nothing speculative is running any more
    assert done_text(seen) == "환불을 접수할까요?" and engine.turn == 1


async def test_cancelled_normal_reply_keeps_what_was_generated():
    engine, llm, _, recorder = build([[text("첫 번째 문장을 말하고 있습니다. "), 5.0, text("두 번째 문장입니다.")]])
    seen = []

    async def consume():
        async for event in engine.reply("설명해 주세요"):
            seen.append(event)

    task = asyncio.create_task(consume())
    while not any(e.type == "piece" for e in seen):
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [m["content"] for m in engine.history] == ["설명해 주세요", "첫 번째 문장을 말하고 있습니다. "]
    record = recorder.turns[-1]
    assert record.interrupted is True and record.agent_text == "첫 번째 문장을 말하고 있습니다." and record.turn == 1
    assert engine.turn == 1


# ------------------------------------------------------------------------------------ truncation

async def test_truncate_last_reply():
    engine, llm, _, recorder = build(
        [[tool("get_order", order_id="A1001")],
         [text("주문하신 무선 이어폰은 배송 중입니다. "), text("내일 오후에 도착할 예정이에요.")],
         [tool("get_order", order_id="A1001")], [text("환불은 배송이 끝난 뒤에 가능합니다.")]])
    await collect(engine, "주문 어디쯤이에요?")
    engine.truncate_last_reply("주문하신 무선 이어폰은")
    assert engine.history[-1] == {"role": "assistant", "content": "주문하신 무선 이어폰은"}
    assert [m["role"] for m in engine.history] == ["user", "assistant", "tool", "assistant"]   # the lookup stays
    record = recorder.turns[-1]
    assert record.turn == 1 and record.agent_text == "주문하신 무선 이어폰은" and record.interrupted is True
    assert recorder.of("interrupt")[0].attrs == {"heard_chars": len("주문하신 무선 이어폰은")}

    await collect(engine, "환불은요?")
    assert len(engine.history) == 8
    engine.truncate_last_reply("")                          # nothing was heard
    # the reply and its tool exchange are gone; the words wait to go in front of the next ones (test_runtime_carry)
    assert [m["role"] for m in engine.history] == ["user", "assistant", "tool", "assistant"]
    record = recorder.turns[-1]
    assert record.turn == 2 and record.agent_text == "" and record.interrupted is True
    assert len(recorder.last_turns()) == 2                   # still one record per turn

    # the next reply does not see the removed exchange
    llm.scripts.append([text("네, 말씀하세요.")])
    await collect(engine, "여보세요?")
    assert [m["role"] for m in llm.calls[-1]["messages"]] == ["system", "user", "assistant", "tool", "assistant", "user"]
    assert llm.calls[-1]["messages"][-1]["content"].endswith("환불은요? 여보세요?")


async def test_truncate_with_everything_heard_changes_nothing():
    engine, _, _, recorder = build([[text("네, 알겠습니다.")]])
    await collect(engine, "안녕")
    engine.truncate_last_reply("네, 알겠습니다.")
    assert engine.history[-1]["content"] == "네, 알겠습니다." and recorder.turns[-1].interrupted is False
    Engine(make_definition(), None, None, ScriptedLLM()).truncate_last_reply("x")     # no turn yet: no error


# ------------------------------------------------------------------------------------ records

async def test_spans_turn_records_and_session():
    knowledge = FakeKnowledge([{"score": 0.8, "title": "배송 안내", "content": "배송은 이틀 걸립니다.", "kind": "text"}])
    definition = make_definition(guards=[FORBIDDEN], knowledge={"kind": "weknora", "name": "시험 지식", "kb_id": "kb1"})
    engine, llm, gateway, recorder = build(
        [[tool("get_order", order_id="A1001")], [text("무조건 내일 도착합니다. "), text("기다려 주세요.")],
         [text("보통 이틀 정도 걸립니다.")],
         [tool("handoff_to_human", reason="고객이 상담원을 바꿔 달라고 요구한다")], [text("상담원에게 연결해 드릴게요.")]],
        definition=definition, knowledge=knowledge, channel="voice", test=True, label="case-1",
        handlers={"handoff_to_human": lambda ctx, a: ToolResult(ok=True, executed=True, text="접수")})

    meta = recorder.sessions[0]
    assert (meta.session_id, meta.agent_id, meta.version, meta.channel) == (engine.session_id, "test-shop", 3, "voice")
    assert meta.test is True and meta.simulated is False and meta.label == "case-1" and meta.started_at > 0
    assert meta.settings["model"] == "gemma4:26b"

    t0 = 1000.0
    async for event in engine.reply("배송 얼마나 걸려요?", t_user_end=t0):
        if event.type == "filler":
            engine.mark_output("filler", t0 + 0.4)
        if event.type == "piece":
            engine.mark_output("reply", t0 + 1.5)
            engine.mark_output("reply", t0 + 9.9)             # only the first counts
    # an "llm" span is written when its pass ends, so the filler's output comes before the first one
    assert recorder.kinds() == ["retrieval", "output", "llm", "llm", "guard", "llm", "output"]
    assert all(s.session_id == engine.session_id and s.turn == 1 for s in recorder.spans)
    retrieval = recorder.of("retrieval")[0]
    assert retrieval.input == {"query": "배송 얼마나 걸려요?"}
    assert retrieval.output == {"sources": ["배송 안내"], "top_score": 0.8} and retrieval.attrs["used"] is True
    first, second, third = recorder.of("llm")
    assert first.attrs["round"] == 1 and first.attrs["model"] == "gemma4:26b" and first.name == "gemma4:26b"
    assert first.input["messages"][0]["role"] == "system"
    assert first.output == {"text": "", "tool_calls": [{"name": "get_order", "arguments": {"order_id": "A1001"}}]}
    assert first.attrs["first_token_ms"] is not None and first.t_end >= first.t_start
    assert third.output["text"] == "보통 이틀 정도 걸립니다." and third.attrs["round"] == 3
    assert [s.attrs["phase"] for s in recorder.of("output")] == ["filler", "reply"]
    assert recorder.of("output")[1].t_start == t0 + 1.5

    assert len(recorder.turns) == 1
    record = recorder.turns[0]
    assert (record.turn, record.user_text, record.agent_text) == (1, "배송 얼마나 걸려요?", "보통 이틀 정도 걸립니다.")
    assert record.t_user_end == t0 and record.t_agent_start == t0 + 1.5 and record.t_agent_end is not None
    assert record.interrupted is False and record.handoff is False

    await collect(engine, "상담원 바꿔 주세요")
    assert recorder.kinds()[7:] == ["retrieval", "llm", "handoff", "llm"]
    assert recorder.of("handoff")[0].attrs["rule_id"] == "angry"
    assert [r.turn for r in recorder.turns] == [1, 2] and recorder.turns[1].handoff is True
    assert recorder.turns[1].t_agent_start is not None           # the engine's own clock when nobody reports

    # the first sound of a spoken reply may come after "done": the record is sent again with it
    engine.mark_output("reply", 2000.0)
    assert len(recorder.turns) == 3 and recorder.turns[2].turn == 2 and recorder.turns[2].t_agent_start == 2000.0
    assert len(recorder.last_turns()) == 2

    await engine.close()
    await engine.close()                                         # closing twice is harmless
    assert recorder.ended == [(engine.session_id, "handoff")]
    assert gateway.ended == [engine.session_id] and knowledge.closed is True


async def test_close_status_and_broken_recorder():
    class Broken(FakeRecorder):
        def span(self, span, pii_paths=None):
            raise RuntimeError("disk full")

        def turn(self, turn):
            raise RuntimeError("disk full")

    recorder = Broken()
    engine = Engine(make_definition(tools=[]), None, recorder, ScriptedLLM([[text("네, 안녕하세요.")]]))
    assert done_text(await collect(engine, "안녕")) == "네, 안녕하세요."        # recording never breaks the reply
    await engine.close("abandoned")
    assert recorder.ended == [(engine.session_id, "abandoned")]


# ------------------------------------------------------------------------------------ knowledge

async def test_knowledge_goes_before_the_question_and_is_reused_for_back_references():
    hits = [{"score": 0.72, "title": "환불 규정", "content": "환불은  배송 완료 후\n7일 이내에 가능합니다.", "kind": "text"},
            {"score": 0.2, "title": "무관한 문서", "content": "다른 이야기", "kind": "text"}]
    knowledge = FakeKnowledge(hits)
    definition = make_definition(tools=[], knowledge={"kind": "weknora", "name": "시험 지식", "kb_id": "kb1", "threshold": 0.4})
    engine, llm, _, recorder = build([[text("7일 이내에 가능합니다.")], [text("배송 완료일부터 셉니다.")],
                                      [text("죄송해요, 그건 자료에 없어요.")]],
                                     definition=definition, knowledge=knowledge)
    events = await collect(engine, "환불은 언제까지 돼요?")
    states = [e.data["state"] for e in events if e.type == "knowledge"]
    assert states == ["start", "done"]
    done = [e.data for e in events if e.type == "knowledge"][1]
    assert done["ok"] and done["sources"] == ["환불 규정"] and done["top"] == 0.72 and done["found"] == 2
    asked = llm.last_user()
    assert asked.startswith("[참고 자료 시작: '시험 지식' 지식베이스에서")
    assert "(1) 문서: 환불 규정\n환불은 배송 완료 후 7일 이내에 가능합니다." in asked and "무관한 문서" not in asked
    assert asked.endswith("[참고 자료 끝]\n질문: 환불은 언제까지 돼요?")
    assert "'시험 지식' 지식베이스" in llm.system()
    assert engine.history[0]["content"] == "환불은 언제까지 돼요?"       # the material is not kept in the history

    events = await collect(engine, "그건 언제부터 세요?")              # points back: no new search
    assert [e.data["state"] for e in events if e.type == "knowledge"] == ["reused"]
    assert knowledge.queries == ["환불은 언제까지 돼요?"]
    assert "환불 규정" in llm.last_user() and llm.last_user().endswith("질문: 그건 언제부터 세요?")
    assert "참고 자료" not in llm.calls[-1]["messages"][1]["content"]     # the earlier turn is plain again

    knowledge.hits = [{"score": 0.1, "title": "무관", "content": "x", "kind": "text"}]
    await collect(engine, "주차는 어디에 해요?")
    assert llm.last_user() == "주차는 어디에 해요?"                      # below the threshold: nothing attached
    assert recorder.of("retrieval")[-1].attrs["used"] is False


async def test_knowledge_failure_does_not_stop_the_reply():
    definition = make_definition(tools=[], knowledge={"kind": "weknora", "name": "시험 지식", "kb_id": "kb1"})
    engine, llm, _, recorder = build([[text("네, 말씀하세요.")]], definition=definition,
                                     knowledge=FakeKnowledge(RuntimeError("HTTP 500")))
    events = await collect(engine, "환불 규정 알려 줘요")
    assert [e.data.get("ok") for e in events if e.type == "knowledge"][1] is False
    assert done_text(events) == "네, 말씀하세요." and recorder.of("retrieval")[0].ok is False


# ------------------------------------------------------------------------------------ odd replies

async def test_empty_reply_after_a_lookup_is_asked_again():
    engine, llm, _, _ = build([[tool("get_order", order_id="A1001")], [], [text("주문은 배송 중입니다.")]])
    events = await collect(engine, "주문 어디쯤이에요?")
    assert done_text(events) == "주문은 배송 중입니다." and len(llm.calls) == 3


async def test_llm_failure_gives_an_apology_and_is_recorded():
    engine, llm, _, recorder = build([RuntimeError("connection refused")])
    events = await collect(engine, "안녕하세요")
    done = next(e for e in events if e.type == "done")
    assert done.text.startswith("죄송합니다") and done.data["fallback"] is True and "connection refused" in done.data["error"]
    assert recorder.of("llm")[0].ok is False and len(recorder.turns) == 1
    assert [e.text for e in events if e.type == "piece"] == [done.text]


async def test_markdown_and_emoji_are_not_spoken():
    engine, _, _, _ = build([[text("**환불**은 `7일` 이내에 가능해요 \U0001F600. "), text("- 더 궁금한 점 있으세요?")]],
                            definition=make_definition(tools=[]))
    events = await collect(engine, "환불 돼요?")
    assert [e.text for e in events if e.type == "piece"] == ["환불은 7일 이내에 가능해요 .", "- 더 궁금한 점 있으세요?"]


async def test_first_piece_is_short_later_ones_longer():
    engine, _, _, _ = build([[text("네. "), text("그럼요, 바로 알려 드릴게요. "), text("짧은 문장. "), text("또 짧은 문장. "),
                              text("이 문장까지 합쳐야 마흔 글자를 넘겨서 한 번에 나갑니다. "), text("끝.")]],
                            definition=make_definition(tools=[]))
    events = await collect(engine, "알려 주세요")
    pieces = [e.text for e in events if e.type == "piece"]
    assert pieces[0] == "네. 그럼요, 바로 알려 드릴게요."           # "네." alone is under 15 characters
    assert pieces[1].startswith("짧은 문장. 또 짧은 문장.") and len(pieces[1]) >= 40
    assert pieces[-1] == "끝."


async def test_model_without_tool_support_runs_without_tools():
    engine, llm, _, _ = build(llm=ScriptedLLM([[text("네, 안녕하세요.")]], tools_supported=False))
    await collect(engine, "안녕")
    assert llm.calls[0]["tools"] is None and "도구 사용 규칙" not in llm.system()


# ------------------------------------------------------------------------------------ many at once

async def test_engines_run_concurrently_without_sharing_state():
    def echo(messages, tools):
        users = [m["content"] for m in messages if m["role"] == "user"]
        return [text(f"손님 말씀은 '{users[-1]}'이고 "), 0.02, text(f"지금까지 {len(users)}번 말씀하셨어요.")]

    recorder = FakeRecorder()
    gateway = FakeGateway(SHOP_CONTRACTS)
    engines = [Engine(make_definition(id=f"agent-{i}"), gateway, recorder, ScriptedLLM(responder=echo, delay=0.01))
               for i in range(6)]

    async def talk(index, engine):
        replies = []
        for turn in range(3):
            replies.append(done_text(await collect(engine, f"{index}번 손님 {turn}번째")))
        return replies

    results = await asyncio.gather(*(talk(i, e) for i, e in enumerate(engines)))
    assert len({e.session_id for e in engines}) == 6
    for index, (engine, replies) in enumerate(zip(engines, results)):
        assert replies == [f"손님 말씀은 '{index}번 손님 {t}번째'이고 지금까지 {t + 1}번 말씀하셨어요." for t in range(3)]
        assert engine.turn == 3 and len(engine.history) == 6
        assert all(f"{index}번 손님" in m["content"] for m in engine.history)
        mine = [r for r in recorder.turns if r.session_id == engine.session_id]
        assert [r.turn for r in mine] == [1, 2, 3] and all(f"{index}번 손님" in r.user_text for r in mine)
    assert len(recorder.sessions) == 6 and {m.agent_id for m in recorder.sessions} == {f"agent-{i}" for i in range(6)}


# ------------------------------------------------------------------------------------ driver

async def test_text_conversation_driver():
    engine, llm, gateway, recorder = build(
        [[tool("get_order", order_id="A1001")], [text("주문은 배송 중입니다.")],
         [tool("handoff_to_human", reason="고객 요청")], [text("상담원에게 연결해 드릴게요.")]],
        handlers={"handoff_to_human": lambda ctx, a: ToolResult(ok=True, executed=True, text="접수")})
    driver = TextConversation(engine)
    assert driver.session_id == engine.session_id
    turn = await driver.say("주문 어디쯤이에요?")
    assert turn.text == "주문은 배송 중입니다." and turn.handoff is False and turn.ms > 0
    assert len(turn.tool_calls) == 1
    call = turn.tool_calls[0]
    assert (call["name"], call["arguments"], call["ok"], call["executed"], call["error_type"]) == (
        "get_order", {"order_id": "A1001"}, True, False, None)
    assert [s.attrs["phase"] for s in recorder.of("output")] == ["filler", "reply"]
    turn = await driver.say("상담원 바꿔 주세요")
    assert turn.handoff is True and turn.tool_calls[0]["executed"] is True
    await driver.close()
    assert recorder.ended == [(engine.session_id, "handoff")]
