"""A customer turn cut off before any of its answer was heard is joined to what they say next (2026-11-20)."""
import runtime_fakes  # noqa: F401

from agentkit.runtime import Engine
from agentkit.runtime import engine as engine_module
from runtime_fakes import SHOP_CONTRACTS, FakeGateway, FakeRecorder, ScriptedLLM, collect, fixed_now, make_definition, text


def users(call):
    return [m["content"] for m in call["messages"] if m["role"] == "user"]


def make(scripts):
    llm = ScriptedLLM(scripts)
    return Engine(make_definition(), FakeGateway(SHOP_CONTRACTS), FakeRecorder(), llm, now=fixed_now), llm


async def test_a_turn_cut_before_anything_was_heard_is_joined_to_the_next():
    engine, llm = make([[text("무엇이 문제인가요?")], [text("목록을 말로 알려 드릴게요.")]])
    await collect(engine, "메일 목록이 화면에 안 보이고")
    engine.truncate_last_reply("")                     # the customer went on before hearing a word
    await collect(engine, "기록 창에만 보여")
    said = users(llm.calls[-1])
    assert said[-1].endswith("메일 목록이 화면에 안 보이고 기록 창에만 보여")
    assert not any(s.endswith("메일 목록이 화면에 안 보이고") for s in said[:-1])   # not asked twice


async def test_a_reply_that_was_partly_heard_is_not_joined():
    engine, llm = make([[text("무엇이 문제인가요? 말씀해 주세요.")], [text("네.")]])
    await collect(engine, "메일 목록이 안 보여")
    engine.truncate_last_reply("무엇이 문제인가요?")
    await collect(engine, "기록 창에만 보여")
    assert users(llm.calls[-1])[-1].endswith("기록 창에만 보여") and "안 보여 기록" not in users(llm.calls[-1])[-1]


async def test_the_words_are_not_carried_after_a_long_pause(monkeypatch):
    engine, llm = make([[text("무엇이 문제인가요?")], [text("네.")]])
    await collect(engine, "메일 목록이 안 보여")
    engine.truncate_last_reply("")
    monkeypatch.setattr(engine_module, "CARRY_S", -1.0)
    await collect(engine, "다음 주 일정 알려 줘")
    assert "안 보여" not in users(llm.calls[-1])[-1]


# ---- a reply going round in circles is cut (guards.loop_start) ----------------------------------

LOOP = ("백이십팔을 먼저 이로 나누면 육십사이고, 육십사를 다시 이로 나누면 삼십이, 삼십이를 다시 이로 나누면 십육이 아닌 "
        "십육이 아니라 십육이 아니고 십육... 아, 다시 계산할게요. 정확히는 이의 제곱 곱하기 구가 아니라 이의 제곱 곱하기 구입니다.")


async def test_a_looping_reply_is_cut_before_the_loop():
    from runtime_fakes import done_text
    engine, llm = make([[text(LOOP)]])
    events = await collect(engine, "계산 과정 설명해 줘")
    said = done_text(events) if callable(done_text) else events[-1].text
    assert said.startswith("백이십팔을 먼저 이로 나누면 육십사이고") and "십육이 아닌" not in said
    assert said.endswith("제가 말이 꼬였어요. 다시 물어봐 주시면 정확히 답할게요.")
    assert "십육이 아닌" not in engine.history[-1]["content"]


def test_plain_explanations_and_lists_are_not_loops():
    from agentkit.runtime.guards import loop_start
    for said in ["백이십팔을 이로 나누면 육십사, 육십사를 이로 나누면 삼십이, 삼십이를 이로 나누면 십육, 십육을 이로 나누면 팔입니다.",
                 "오전 9시, 오전 10시, 오전 11시, 오후 1시, 오후 2시가 비어 있어요.",
                 "환불이 아니라 교환을 원하시는 거죠? 교환이 아니라 반품이면 말씀해 주세요."]:
        assert loop_start(said) is None, said


# ---- a pass after a guard's note that answers the note is dropped ---------------------------------

async def test_a_regenerated_reply_that_answers_the_system_note_is_dropped():
    from agentkit.contracts import Guard
    definition = make_definition(guards=[Guard(id="offer", kind="regex", pattern="잡아 드릴까요", unless_tools=[],
                                                nudge="(시스템 안내: 확인은 도구가 합니다.)")])
    llm = ScriptedLLM([[text("금요일 오후가 비어 있어요. 일정을 잡아 드릴까요?")],
                       [text("네, 알겠습니다. 앞으로는 지시하신 대로 도구를 호출하겠습니다. 무엇을 도와드릴까요?")]])
    engine = Engine(definition, FakeGateway(SHOP_CONTRACTS), FakeRecorder(), llm, now=fixed_now)
    events = await collect(engine, "금요일 오후 비어?")
    spoken = " ".join(e.text for e in events if e.type == "piece")
    assert "지시하신" not in spoken and "알겠습니다" not in spoken
    assert engine.history[-1]["content"] == "금요일 오후가 비어 있어요."
