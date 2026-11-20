"""knowledge.mode (before_reply / tool) and the explicit-request rules: which turns search the knowledge base
before the reply. Found in a real v1 conversation (2026-11-20): "자동차 관련 뉴스" was answered from a stored
document, and "자세히 알아봐 줘" was searched as it was."""
import pytest

import runtime_fakes  # noqa: F401  (sets AGENTKIT_DATA_DIR before agentkit.config is read)

from agentkit.contracts import ToolContract
from agentkit.runtime import Engine
from agentkit.runtime.knowledge import knowledge_first
from agentkit.runtime.prompt import build_system_prompt
from runtime_fakes import (FakeGateway, FakeKnowledge, FakeRecorder, ScriptedLLM, collect, contract, fixed_now,
                           make_definition, text)

HIT = [{"title": "LongCat-Video 소개", "content": "LongCat-Video는 영상 생성 모델이다.", "score": 0.7, "kind": "text"}]


@pytest.mark.parametrize("said, mode, first", [
    ("LongCat 영상 모델이 뭐야?", "before_reply", True),
    ("자동차 관련 뉴스", "before_reply", False),          # asks for the news
    ("롱캣 비디오를 인터넷에서 검색해줘", "before_reply", False),
    ("지식베이스에서 롱캣 찾아줘", "before_reply", True),
    ("LongCat 영상 모델이 뭐야?", "tool", False),          # the model decides
    ("자세히 알아봐 줘", "tool", False),
    ("지식베이스에서 롱캣 찾아줘", "tool", True),            # named: searched first anyway
    ("저장된 자료에서 음성 합성 찾아줘", "tool", True),
    ("지식베이스에 오늘 뉴스 있어?", "tool", True),          # names both: the knowledge base wins
])
def test_knowledge_first(said, mode, first):
    assert knowledge_first(said, mode) is first


def engine(mode, said_back="네."):
    definition = make_definition(knowledge={"kind": "weknora", "name": "Tech Trend", "kb_id": "x", "mode": mode})
    knowledge = FakeKnowledge(HIT, name="Tech Trend")
    llm = ScriptedLLM([[text(said_back)]])
    e = Engine(definition, FakeGateway([]), FakeRecorder(), llm, knowledge=knowledge, now=fixed_now)
    return e, knowledge, llm


async def test_news_turn_gets_no_reference_in_before_reply_mode():
    e, knowledge, llm = engine("before_reply")
    events = await collect(e, "자동차 관련 뉴스")
    assert knowledge.queries == []
    assert any(ev.type == "knowledge" and ev.data.get("state") == "skipped" for ev in events)
    assert "참고 자료" not in llm.last_user()


async def test_topic_turn_still_searched_in_before_reply_mode():
    e, knowledge, llm = engine("before_reply")
    await collect(e, "LongCat 영상 모델이 뭐야?")
    assert knowledge.queries == ["LongCat 영상 모델이 뭐야?"]
    assert "LongCat-Video는" in llm.last_user()


async def test_tool_mode_searches_only_when_the_user_names_the_knowledge_base():
    e, knowledge, llm = engine("tool")
    await collect(e, "LongCat 영상 모델이 뭐야?")
    assert knowledge.queries == []
    await collect(e, "지식베이스에서 LongCat 찾아줘")
    assert knowledge.queries == ["지식베이스에서 LongCat 찾아줘"]


def test_tool_mode_prompt_tells_the_model_to_call_search_knowledge():
    tools = {"search_knowledge": contract("search_knowledge", description="지식베이스를 찾는다",
                                          properties={"query": {"type": "string"}}, required=["query"])}
    tool_mode = make_definition(knowledge={"kind": "weknora", "name": "Tech Trend", "kb_id": "x", "mode": "tool"})
    prompt = build_system_prompt(tool_mode, contracts=tools, knowledge_name="Tech Trend", knowledge_mode="tool")
    assert "search_knowledge를 부르세요" in prompt
    old = build_system_prompt(tool_mode, contracts=tools, knowledge_name="Tech Trend")
    assert "search_knowledge를 부르세요" not in old


async def test_search_knowledge_is_not_offered_when_the_user_asks_for_the_internet():
    tools = [contract("search_knowledge", properties={"query": {"type": "string"}}, required=["query"]),
             contract("search_news", properties={"query": {"type": "string"}}, required=["query"])]
    definition = make_definition(tools=["search_knowledge", "search_news"],
                                 knowledge={"kind": "weknora", "name": "Tech Trend", "kb_id": "x", "mode": "tool"})
    llm = ScriptedLLM([[text("네.")], [text("네.")], [text("네.")]])
    e = Engine(definition, FakeGateway(tools), FakeRecorder(), llm, knowledge=FakeKnowledge(HIT), now=fixed_now)
    offered = lambda call: {t["function"]["name"] for t in (llm.calls[call]["tools"] or [])}
    await collect(e, "롱캣 비디오 관련된 내용을 인터넷에서 검색해줘")
    assert offered(-1) == {"search_news"}
    await collect(e, "롱캣 비디오가 뭐야?")
    assert offered(-1) == {"search_knowledge", "search_news"}
    await collect(e, "지식베이스에서 롱캣 뉴스 찾아줘")
    assert "search_knowledge" in offered(-1)
