"""The note on where the conversation stands (runtime/situation.py)."""
import runtime_fakes  # noqa: F401

from agentkit.runtime import Engine
from agentkit.runtime.situation import situation
from runtime_fakes import FakeGateway, FakeRecorder, ScriptedLLM, collect, contract, fixed_now, make_definition, text, tool

NEWS = "자동차 관련 뉴스 3건입니다.\n1. 현대차, 새 전기차 공개 (연합뉴스)\n2. 자율주행 택시 시범 운행 (한겨레)\n3. 車 수출 증가 (KBS)"


def test_nothing_before_the_first_lookup_or_question():
    assert situation([]) == ""
    assert situation([{"role": "user", "content": "안녕"}, {"role": "assistant", "content": "안녕하세요."}]) == ""


def test_last_lookup_with_items_and_the_question_asked():
    history = [
        {"role": "user", "content": "자동차 뉴스 찾아줘"},
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "search_news", "arguments": {"query": "자동차"}}}]},
        {"role": "tool", "tool_name": "search_news", "content": NEWS},
        {"role": "assistant", "content": "자동차 뉴스 세 건이 있어요. 첫 번째 기사를 자세히 알려 드릴까요?"},
    ]
    note = situation(history)
    assert 'search_news(query="자동차")' in note and "현대차, 새 전기차 공개" in note
    assert '"첫 번째 기사를 자세히 알려 드릴까요?"' in note


def test_old_lookups_fade_and_the_opening_is_not_the_flow():
    opening = [{"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "get_menu", "arguments": {}}}]},
               {"role": "tool", "tool_name": "get_menu", "content": "메뉴"}, {"role": "assistant", "content": "어서 오세요."}]
    assert situation(opening + [{"role": "user", "content": "아메리카노 얼마예요?"}]) == ""
    chatter = []
    for i in range(5):
        chatter += [{"role": "user", "content": f"말 {i}"}, {"role": "assistant", "content": "네."}]
    history = [{"role": "user", "content": "자동차 뉴스"},
               {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "search_news", "arguments": {"query": "자동차"}}}]},
               {"role": "tool", "content": NEWS}, {"role": "assistant", "content": "세 건이에요."}] + chatter
    assert situation(history) == ""


async def test_the_note_goes_in_front_of_the_users_words():
    llm = ScriptedLLM([[tool("search_news", query="자동차")], [text("자동차 뉴스 세 건이에요. 첫 번째를 자세히 볼까요?")],
                       [text("네, 첫 번째 기사입니다.")]])
    gateway = FakeGateway([contract("search_news", properties={"query": {"type": "string"}}, required=["query"])],
                          handlers={"search_news": lambda ctx, a: __import__("agentkit.contracts", fromlist=["ToolResult"]).ToolResult(ok=True, text=NEWS)})
    engine = Engine(make_definition(tools=["search_news"]), gateway, FakeRecorder(), llm, now=fixed_now)
    await collect(engine, "자동차 뉴스 찾아줘")
    assert "지금 대화의 흐름" not in llm.last_user(0)          # nothing had happened yet
    await collect(engine, "자세히 알아봐 줘")
    sent = llm.last_user()
    assert sent.startswith("(지금 대화의 흐름") and sent.endswith("자세히 알아봐 줘") and "search_news" in sent
    assert engine.history[-2]["content"] == "자세히 알아봐 줘"   # the history keeps the user's own words
