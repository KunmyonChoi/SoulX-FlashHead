"""Reply templates (contracts.ToolReply): a successful call is answered at once, without a second model pass."""
import pytest

import runtime_fakes  # noqa: F401  (sets AGENTKIT_DATA_DIR before agentkit.config is read)

from agentkit.contracts import ToolReply
from agentkit.runtime import Engine
from agentkit.runtime.templates import Missing, fill
from runtime_fakes import FakeRecorder, MiniCafe, ScriptedLLM, fixed_now, gather, make_cafe_definition, text, tool, types

ADD = ToolReply(say="{name} {quantity|count} 개 담았어요.", closing="더 필요하신 거 있으세요?")


def cafe(scripts, *, reply=ADD, definition=None):
    llm, gateway, recorder = ScriptedLLM(scripts), MiniCafe(), FakeRecorder()
    if reply is not None:
        gateway.contracts["add_to_cart"] = gateway.contracts["add_to_cart"].model_copy(update={"reply": reply})
    engine = Engine(definition or make_cafe_definition(), gateway, recorder, llm, now=fixed_now)
    return engine, llm, gateway, recorder


# ------------------------------------------------------------------------------------ the filler

def test_fill_values_filters_and_missing():
    assert fill("{name} {quantity|count} 잔", {}, {"name": "라떼", "quantity": 2}) == "라떼 두 잔"
    assert fill("{args.item_id} {quantity|count}개", {"item_id": "x"}, {"quantity": 12}) == "x 12개"
    assert fill("{line.name}{line.options|paren}", {}, {"line": {"name": "라떼", "options": ["차갑게", "크게"]}}) == "라떼(차갑게, 크게)"
    assert fill("{line.name}{line.options|paren}", {}, {"line": {"name": "라떼"}}) == "라떼"
    with pytest.raises(Missing):
        fill("{nothing} 담았어요", {}, {})


async def test_successful_call_is_answered_from_the_template_without_a_second_pass():
    engine, llm, gateway, recorder = cafe([[tool("add_to_cart", item_id="americano", quantity=2)]])
    events = await gather(engine.reply("아메리카노 두 잔 주세요"))
    assert len(llm.calls) == 1                       # no second pass
    assert "filler" not in types(events)             # nothing to cover: the answer comes at once
    pieces = [e.text for e in events if e.type == "piece"]
    assert pieces == ["아메리카노 두 개 담았어요.", "더 필요하신 거 있으세요?"]   # separate, so speech starts sooner
    assert events[-1].type == "done" and events[-1].text == " ".join(pieces)
    assert [s.kind for s in recorder.spans].count("template") == 1
    assert engine.history[-1] == {"role": "assistant", "content": " ".join(pieces)}
    assert recorder.turns[-1].agent_text == " ".join(pieces)


async def test_several_calls_make_one_reply_with_one_closing():
    engine, llm, gateway, recorder = cafe([[tool("add_to_cart", item_id="americano", quantity=1),
                                            tool("add_to_cart", item_id="vanilla_latte", quantity=3)]])
    events = await gather(engine.reply("아메리카노 하나랑 바닐라 라떼 세 잔요"))
    assert len(llm.calls) == 1
    assert events[-1].text == "아메리카노 한 개 담았어요. 바닐라 라떼 세 개 담았어요. 더 필요하신 거 있으세요?"


async def test_a_failed_call_goes_back_to_the_model_with_a_filler():
    engine, llm, gateway, recorder = cafe([[tool("add_to_cart", item_id="choco_cake")],
                                           [text("초코 케이크는 품절이에요. 치즈 케이크는 어떠세요?")]])
    events = await gather(engine.reply("초코 케이크 주세요"))
    assert len(llm.calls) == 2
    assert "filler" in types(events)                 # the filler was held back, then said after all
    assert events[-1].text.startswith("초코 케이크는 품절이에요")
    assert not [s for s in recorder.spans if s.kind == "template"]


async def test_a_tool_without_a_template_still_takes_two_passes():
    engine, llm, gateway, recorder = cafe([[tool("add_to_cart", item_id="americano")], [text("담았어요.")]], reply=None)
    events = await gather(engine.reply("아메리카노요"))
    assert len(llm.calls) == 2 and "filler" in types(events)


async def test_text_said_with_the_call_leaves_the_wording_to_the_model():
    engine, llm, gateway, recorder = cafe([[text("네, 아메리카노 담을게요."), tool("add_to_cart", item_id="americano")],
                                           [text("담았어요.")]])
    await gather(engine.reply("아메리카노요"))
    assert len(llm.calls) == 2


async def test_a_missing_value_falls_back_to_the_model():
    engine, llm, gateway, recorder = cafe([[tool("add_to_cart", item_id="americano")], [text("담았어요.")]],
                                          reply=ToolReply(say="{line.name} 담았어요."))
    await gather(engine.reply("아메리카노요"))
    assert len(llm.calls) == 2


async def test_an_english_turn_does_not_use_a_korean_template():
    engine, llm, gateway, recorder = cafe([[tool("add_to_cart", item_id="americano")], [text("Added one americano.")]],
                                          definition=make_cafe_definition(language="en"))
    events = await gather(engine.reply("One americano please"))
    assert len(llm.calls) == 2 and events[-1].text == "Added one americano."


async def test_a_touch_is_answered_from_the_template_without_the_model():
    engine, llm, gateway, recorder = cafe([])
    await gather(engine.open())
    events = await gather(engine.act("add_item", {"item_id": "vanilla_latte", "quantity": 1}))
    assert llm.calls == []                           # the model was not needed at all
    assert events[-1].type == "done" and events[-1].text == "바닐라 라떼 한 개 담았어요. 더 필요하신 거 있으세요?"
    assert "display" in types(events)


async def test_a_confirmation_is_never_templated():
    engine, llm, gateway, recorder = cafe([[tool("request_payment")], [text("합계 0원 결제를 진행할까요?")]])
    await gather(engine.reply("결제할게요"))
    assert len(llm.calls) == 2
