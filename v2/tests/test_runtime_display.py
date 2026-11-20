"""Screens, the agent's opening and touches (docs/DISPLAY.md) with the scripted LLM and a fake cafe."""
import asyncio

import runtime_fakes  # noqa: F401  (sets AGENTKIT_DATA_DIR before agentkit.config is read)

from agentkit.contracts import ToolResult
from agentkit.runtime import Engine, TextConversation, client_features
from agentkit.runtime.engine import HISTORY_MESSAGES
from agentkit.runtime.prompt import build_system_prompt, touch_sentence
from runtime_fakes import (OPENING_WORDS, SHOP_CONTRACTS, FakeGateway, FakeRecorder, MiniCafe, ScriptedLLM, collect,
                           done_text, fixed_now, gather, make_cafe_definition, make_definition, text, tool, types)


def cafe(scripts=None, *, definition=None, **options):
    llm = options.pop("llm", None) or ScriptedLLM(scripts)
    gateway, recorder = MiniCafe(), FakeRecorder()
    engine = Engine(definition or make_cafe_definition(), gateway, recorder, llm, now=fixed_now, **options)
    return engine, llm, gateway, recorder


# ------------------------------------------------------------------------------------ nothing new for old agents

async def test_agent_without_opening_screen_or_actions_is_unchanged():
    llm, gateway, recorder = ScriptedLLM([[text("네, 말씀하세요.")]]), FakeGateway(SHOP_CONTRACTS), FakeRecorder()
    engine = Engine(make_definition(), gateway, recorder, llm, now=fixed_now)
    assert client_features(engine.definition) == {"screen": {"panels": []}, "actions": [], "opening": False}
    events = await gather(engine.open())
    assert types(events) == ["done"] and events[0].text == ""
    assert engine.history == [] and engine.turn == 0 and not recorder.spans and not recorder.turns
    assert not gateway.calls
    await collect(engine, "안녕하세요")
    assert "화면" not in llm.system() and [m["role"] for m in llm.calls[0]["messages"]] == ["system", "user"]
    events = await gather(engine.act("add_item", {"item_id": "americano"}))
    assert types(events) == ["done"] and "unknown action" in events[0].data["error"]
    assert engine.turn == 1 and not gateway.calls


# ------------------------------------------------------------------------------------ opening

async def test_opening_calls_tools_shows_screens_and_says_the_words():
    engine, llm, gateway, recorder = cafe([[text("네, 아메리카노는 4500원입니다.")]])
    events = await gather(engine.open())
    assert types(events) == ["tool", "tool", "display", "tool", "tool", "display", "piece", "done"]
    assert [e.data["state"] for e in events if e.type == "tool"] == ["start", "done", "start", "done"]
    displays = [e.data for e in events if e.type == "display"]
    assert [(d["kind"], d["source"]) for d in displays] == [("menu", "get_menu"), ("cart", "get_cart")]
    assert displays[0]["data"]["categories"][0]["items"][0]["id"] == "americano"
    assert events[-2].text == OPENING_WORDS and events[-1].text == OPENING_WORDS and events[-1].data["turn"] == 0
    # the calls: turn 0, final, in order, through the gateway
    assert [(ctx.turn, ctx.speculative, name) for ctx, name, _ in gateway.calls] == [
        (0, False, "get_menu"), (0, False, "get_cart")]
    assert not llm.calls and engine.turn == 0
    # recorded as turn 0
    spans = recorder.of("display")
    assert [(s.turn, s.name, s.attrs["source"]) for s in spans] == [(0, "menu", "get_menu"), (0, "cart", "get_cart")]
    assert spans[0].output["data"] == displays[0]["data"]
    record = recorder.last_turns()[0]
    assert (record.turn, record.user_text, record.agent_text) == (0, "", OPENING_WORDS)
    engine.mark_output("reply", 1.0)
    assert [(s.turn, s.attrs["phase"]) for s in recorder.of("output")] == [(0, "reply")]

    # the next reply's prompt knows what the opening tools returned and what was said
    await collect(engine, "아메리카노 얼마예요?")
    messages = llm.calls[0]["messages"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "tool", "tool", "assistant", "user"]
    assert [c["function"]["name"] for c in messages[2]["tool_calls"]] == ["get_menu", "get_cart"]
    assert "아메리카노(americano) 4500원" in messages[3]["content"] and messages[3]["tool_name"] == "get_menu"
    assert messages[5]["content"] == OPENING_WORDS and messages[6]["content"] == "아메리카노 얼마예요?"
    assert engine.turn == 1 and [r.turn for r in recorder.last_turns()] == [0, 1]

    # once only
    again = await gather(engine.open())
    assert types(again) == ["done"] and again[0].text == "" and len(gateway.calls) == 2


async def test_opening_with_words_only_and_with_tools_only():
    engine, llm, gateway, recorder = cafe(definition=make_cafe_definition(opening={"say": "어서 오세요."}))
    assert types(await gather(engine.open())) == ["piece", "done"] and not gateway.calls
    assert [m["role"] for m in engine.history] == ["user", "assistant"]

    engine, llm, gateway, recorder = cafe(definition=make_cafe_definition(opening={"tools": [{"name": "get_menu"}]}))
    events = await gather(engine.open())
    assert types(events) == ["tool", "tool", "display", "done"] and events[-1].text == ""
    assert client_features(engine.definition)["opening"] is True
    assert [m["role"] for m in engine.history] == ["user", "assistant", "tool"]


async def test_opening_results_survive_the_history_limit():
    engine, llm, gateway, recorder = cafe(llm=ScriptedLLM(responder=lambda m, t: [text("네.")]))
    await gather(engine.open())
    for index in range(HISTORY_MESSAGES):
        await collect(engine, f"{index}번째 말")
    messages = llm.calls[-1]["messages"]
    assert "아메리카노(americano) 4500원" in messages[3]["content"]            # the menu is still there
    assert len(messages) <= 1 + 5 + HISTORY_MESSAGES + 1
    assert messages[6]["role"] == "user" and messages[6]["content"] != "0번째 말"   # the old turns are not


async def test_interrupted_opening_keeps_the_menu_but_not_the_unheard_words():
    engine, llm, gateway, recorder = cafe([[text("네.")]])
    await gather(engine.open())
    engine.truncate_last_reply("")                # the customer spoke before any of it was heard
    assert [m["role"] for m in engine.history] == ["user", "assistant", "tool", "tool"]
    record = recorder.last_turns()[0]
    assert record.turn == 0 and record.interrupted and record.agent_text == ""
    engine.discard_last_turn()                    # never takes the opening away
    assert len(engine.history) == 4
    await collect(engine, "아메리카노 주세요")
    assert "americano" in llm.calls[0]["messages"][3]["content"]


# ------------------------------------------------------------------------------------ display events in a reply

async def test_display_events_follow_the_tool_and_are_recorded():
    engine, llm, gateway, recorder = cafe([[tool("add_to_cart", item_id="americano", quantity=2)],
                                           [text("아메리카노 두 잔 담았어요.")]])
    events = await collect(engine, "아이스 아메리카노 두 잔 주세요")
    kinds = types(events)
    done = next(i for i, e in enumerate(events) if e.type == "tool" and e.data["state"] == "done")
    assert kinds[done + 1] == "display" and kinds.count("display") == 1
    display = events[done + 1]
    assert display.data["kind"] == "cart" and display.data["source"] == "get_cart" and display.text == ""
    assert display.data["data"]["total"] == 9000 and display.data["data"]["lines"][0]["quantity"] == 2
    span = recorder.of("display")[0]
    assert (span.turn, span.name, span.attrs, span.output) == (1, "cart", {"source": "get_cart"},
                                                              {"data": display.data["data"]})
    assert "display" not in [e.type for e in events if e.type in ("piece", "delta")]   # never spoken


async def test_confirm_display_for_a_write_tool_proposal_and_notice_on_failure():
    engine, llm, gateway, recorder = cafe([[tool("request_payment")], [text("합계 0원 결제를 진행할까요?")],
                                           [tool("add_to_cart", item_id="choco_cake")], [text("초코 케이크는 품절이에요.")]])
    events = await collect(engine, "결제할게요")
    display = next(e for e in events if e.type == "display")
    assert display.data["kind"] == "confirm" and display.data["data"]["tool"] == "request_payment"
    events = await collect(engine, "초코 케이크 주세요")
    assert [e.data["kind"] for e in events if e.type == "display"] == ["notice"]
    assert [s.name for s in recorder.of("display")] == ["confirm", "notice"]


async def test_cancelled_speculative_reply_leaves_no_display_span_and_a_committed_one_does():
    slow = [tool("get_cart")]
    engine, llm, gateway, recorder = cafe([slow, [text("주문 내역이"), 5, text(" 비어 있어요.")],
                                           [tool("get_cart")], [text("주문 내역이 비어 있어요.")]])
    seen = []

    async def consume():
        async for event in engine.reply("주문 내역 보여 줘요", speculative=True):
            seen.append(event)

    task = asyncio.create_task(consume())
    while not any(e.type == "delta" for e in seen):
        await asyncio.sleep(0.01)
    assert "display" in types(seen)               # the channel got it (and holds it back)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not recorder.of("display") and engine.turn == 0 and engine.history == []

    events = await collect(engine, "주문 내역 보여 줘요.", speculative=True)   # this one completes: committed
    assert "display" in types(events)
    assert [(s.turn, s.name) for s in recorder.of("display")] == [(1, "cart")]


# ------------------------------------------------------------------------------------ touches

async def test_touch_calls_the_tool_and_the_model_reacts_once_without_tools():
    # the model tries to add it again: the reaction pass has no tools, so that goes nowhere
    engine, llm, gateway, recorder = cafe([[tool("add_to_cart", item_id="vanilla_latte"), text("바닐라 라떼 담았어요.")],
                                           [text("네, 결제할까요?")]])
    await gather(engine.open())
    events = await gather(engine.act("add_item", {"item_id": "vanilla_latte", "quantity": 1}))
    kinds = types(events)
    assert kinds[:3] == ["tool", "tool", "display"] and kinds[-1] == "done" and "filler" not in kinds
    assert events[1].data["executed"] is True and events[1].data["name"] == "add_to_cart"
    assert events[2].data["kind"] == "cart" and events[2].data["data"]["total"] == 5500
    assert done_text(events) == "바닐라 라떼 담았어요." and events[-1].data["turn"] == 1
    # called once, as turn 1, final
    adds = [(ctx.turn, ctx.speculative, arguments) for ctx, name, arguments in gateway.calls if name == "add_to_cart"]
    assert adds == [(1, False, {"item_id": "vanilla_latte", "quantity": 1})]
    assert gateway.carts[engine.session_id] == {"vanilla_latte": 1}
    # one generation, without tools, with the touch and its result in front of it
    assert len(llm.calls) == 1 and llm.calls[0]["tools"] is None
    messages = llm.calls[0]["messages"]
    sentence = "(화면에서 바닐라 라떼 1개를 담았습니다)"
    assert [m["role"] for m in messages[-3:]] == ["user", "assistant", "tool"]
    assert messages[-3]["content"] == sentence
    assert messages[-2]["tool_calls"][0]["function"] == {"name": "add_to_cart",
                                                         "arguments": {"item_id": "vanilla_latte", "quantity": 1}}
    assert "바닐라 라떼 1개를 담았습니다" in messages[-1]["content"] and "다시 호출하지" in messages[-1]["content"]
    # a customer turn: counted and recorded with the touch as what the customer "said"
    assert engine.turn == 1
    record = recorder.last_turns()[-1]
    assert (record.turn, record.user_text, record.agent_text) == (1, sentence, "바닐라 라떼 담았어요.")
    action = recorder.of("action")[0]
    assert (action.turn, action.name, action.input, action.attrs) == (
        1, "add_item", {"arguments": {"item_id": "vanilla_latte", "quantity": 1}}, {"tool": "add_to_cart"})
    assert [(s.turn, s.name) for s in recorder.of("display")][-1] == (1, "cart")
    # the next spoken turn has tools again and sees the touch in the history
    await collect(engine, "네")
    assert llm.calls[1]["tools"] and any(m["content"] == sentence for m in llm.calls[1]["messages"])
    assert sum(1 for _, name, _ in gateway.calls if name == "add_to_cart") == 1


async def test_touch_is_refused_for_unknown_actions_and_for_tools_that_need_consent():
    engine, llm, gateway, recorder = cafe()
    for name, reason in (("nothing", "unknown action"), ("pay", "spoken consent"), ("ghost", "not available")):
        events = await gather(engine.act(name, {}))
        assert types(events) == ["done"] and reason in events[0].data["error"] and events[0].text == ""
    assert engine.turn == 0 and engine.history == [] and not gateway.calls and not llm.calls
    assert not recorder.spans and not recorder.turns


async def test_touch_that_fails_is_explained_not_retried():
    engine, llm, gateway, recorder = cafe([[text("초코 케이크는 지금 품절이에요.")]])
    events = await gather(engine.act("add_item", {"item_id": "choco_cake"}))
    tool_done = events[1]
    assert tool_done.data["ok"] is False and tool_done.data["error_type"] == "precondition_failed"
    assert [e.data["kind"] for e in events if e.type == "display"] == ["notice"]
    told = llm.calls[0]["messages"][-1]["content"]
    assert "[실패: precondition_failed]" in told and "처리되지 않았습니다" in told
    assert llm.calls[0]["tools"] is None and done_text(events) == "초코 케이크는 지금 품절이에요."
    assert recorder.of("action")[0].attrs == {"tool": "add_to_cart"}


async def test_touch_sentence():
    definition = make_cafe_definition()
    add, pay = definition.actions[0], definition.actions[1]
    assert touch_sentence(add, {"quantity": 2}, "ko", {"name": "아메리카노"}) == "(화면에서 아메리카노 2개를 담았습니다)"
    assert touch_sentence(add, {"quantity": 2, "name": "라떼"}, "ko", {"name": "x"}) == "(화면에서 라떼 2개를 담았습니다)"
    assert touch_sentence(add, {}, "ko") == "(화면에서 개를 담았습니다)"          # missing fields are left out
    assert touch_sentence(pay, {}, "ko") == "(화면에서 pay 동작을 했습니다)"      # no `say`: a plain default
    assert touch_sentence(pay, {}, "en") == "(did pay on the screen)"


# ------------------------------------------------------------------------------------ prompt

async def test_screen_instruction_in_the_prompt():
    definition = make_cafe_definition()
    korean = build_system_prompt(definition, language="ko")
    assert "화면: 고객은 화면으로 메뉴와 주문 내역을 보고 있습니다." in korean and "전체를 말로 읽지 말고" in korean
    assert "화면을 눌러" in korean
    assert korean.index("말하기 규칙") < korean.index("화면: ")
    english = build_system_prompt(definition, language="en")
    assert "looking at the menu and their order on a screen" in english and "touching the screen" in english
    no_touch = build_system_prompt(make_cafe_definition(actions=[], screen={"panels": ["menu"]}), language="ko")
    assert "화면으로 메뉴을 보고" in no_touch and "화면을 눌러" not in no_touch
    assert "화면" not in build_system_prompt(make_cafe_definition(screen={"panels": []}), language="ko")
    engine, llm, gateway, recorder = cafe([[text("네.")]])
    await collect(engine, "안녕하세요")
    assert "화면: 고객은" in llm.system()


# ------------------------------------------------------------------------------------ driver

async def test_driver_opening_act_and_displays():
    engine, llm, gateway, recorder = cafe([[tool("add_to_cart", item_id="americano", quantity=2)],
                                           [text("아메리카노 두 잔 담았어요.")], [text("바닐라 라떼도 담았어요.")]])
    driver = TextConversation(engine)
    first = await driver.opening()
    assert first.text == OPENING_WORDS and [d["kind"] for d in first.displays] == ["menu", "cart"]
    assert [c["name"] for c in first.tool_calls] == ["get_menu", "get_cart"] and first.handoff is False
    turn = await driver.say("아메리카노 두 잔 주세요")
    assert [d["kind"] for d in turn.displays] == ["cart"] and turn.displays[0]["data"]["total"] == 9000
    assert set(turn.displays[0]) == {"kind", "data", "source"}
    touch = await driver.act("add_item", {"item_id": "vanilla_latte", "quantity": 1})
    assert touch.text == "바닐라 라떼도 담았어요." and touch.displays[0]["data"]["total"] == 14500
    assert touch.tool_calls[0]["name"] == "add_to_cart" and touch.tool_calls[0]["executed"] is True
    refused = await driver.act("pay")
    assert refused.text == "" and not refused.displays and "consent" in driver.events[-1].data["error"]
    assert [s.attrs["phase"] for s in recorder.of("output") if s.turn == 0] == ["reply"]
    await driver.close()

    plain = TextConversation(Engine(make_definition(), FakeGateway(SHOP_CONTRACTS), FakeRecorder(), ScriptedLLM()))
    empty = await plain.opening()
    assert empty.text == "" and not empty.displays and not empty.tool_calls
