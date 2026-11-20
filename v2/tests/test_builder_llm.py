"""The builder with the real model (Ollama at config.LLM_URL) on a temporary copy of the agents
directory. Slow; marked `llm`.

    python -m pytest tests/test_builder_llm.py -q -m llm -s      (-s prints replies and operations)

No test suite is really run here (that takes minutes): the evaluator is the fake of the fast tests.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import httpx
import pytest

import test_builder_support as support
from agentkit import config
from agentkit.builder import BuilderAgent
from agentkit.builder.agent import BLOCK_HEAD
from agentkit.llm import LLM

pytestmark = pytest.mark.llm


@pytest.fixture(scope="module")
def llm():
    try:
        models = httpx.get(config.LLM_URL + "/api/tags", timeout=3).json().get("models", [])
    except Exception as e:
        pytest.skip(f"Ollama is not reachable at {config.LLM_URL}: {e}")
    if not any(model.get("name") == config.LLM_MODEL for model in models):
        pytest.skip(f"model {config.LLM_MODEL} is not installed")
    return LLM()


@pytest.fixture
def store(tmp_path, monkeypatch):
    store = support.copy_agents(tmp_path)
    monkeypatch.setenv("AGENTKIT_AGENTS_DIR", str(store.root))
    return store


@pytest.fixture
def builder(store, llm):
    # the tool catalog is the real one (v2/tools, read only)
    return BuilderAgent(store=store, llm=llm, evaluator=support.FakeEvaluator(store),
                        trace_store=lambda: support.FakeTraceStore([]))


def show(message, result):
    print(f"\n--- 담당자: {message}\n--- 제작 에이전트 ({result['mode']}):\n{result['reply']}")
    for op in result["operations"]:
        print(f"    연산 {op['name']} {json.dumps(op['arguments'], ensure_ascii=False)[:300]}"
              f" -> {'성공' if op['ok'] else '실패'}: {op['result'][:160]!r}")


async def test_llm_refund_limit_change_is_a_safety_change(builder, store):
    message = "shop-support에서 환불 금액이 5만 원을 넘으면 상담원에게 넘기도록 바꿔 줘"
    result = await builder.chat(message)            # no agent selected: it is named in the message
    show(message, result)
    assert support.states(store, "shop-support") == {1: "deployed", 2: "draft"}
    info = store.version_info("shop-support", 2)
    assert (info.created_by, info.based_on) == ("builder", 1)
    before = {rule.id: rule.when for rule in store.load("shop-support", 1).handoff}
    after = {rule.id: rule.when for rule in store.load("shop-support", 2).handoff}
    assert after != before and "handoff" in info.changed_fields
    assert any("50000" in when.replace(",", "") or "5만" in when for when in after.values()), after
    assert info.safety_change is True
    assert (result["agent_id"], result["version"], result["safety_change"]) == ("shop-support", 2, True)
    assert result["changed_fields"] == info.changed_fields
    assert BLOCK_HEAD in result["reply"] and "안전 규칙 변경: 예" in result["reply"]
    assert store.deployment("shop-support").stable == 1
    print("    changed_fields:", info.changed_fields, "| stale:", result["changes"][0]["stale_values"])


async def test_llm_tone_change_is_not_a_safety_change(builder, store):
    message = "말투를 더 짧고 간결하게 해 줘"
    result = await builder.chat(message, agent_id="shop-support")
    show(message, result)
    assert support.states(store, "shop-support") == {1: "deployed", 2: "draft"}
    info = store.version_info("shop-support", 2)
    assert info.safety_change is False and result["safety_change"] is False
    assert info.changed_fields and set(info.changed_fields) <= {"persona", "reply_length"}, info.changed_fields
    assert result["version"] == 2 and "안전 규칙 변경: 아니오" in result["reply"]
    print("    persona:", store.load("shop-support", 2).persona.replace("\n", " / "))


async def test_llm_new_agent_request_is_met_with_questions(builder, store):
    before = store.list_agents()
    message = "병원 예약 받는 에이전트 하나 만들어 줘"
    result = await builder.chat(message)
    show(message, result)
    assert store.list_agents() == before, "an agent was created from a one-line request"
    assert result["version"] is None and not any(op["ok"] and op["name"] == "create_agent" for op in result["operations"])
    asked = sum(word in result["reply"] for word in ("하지 않는", "본인", "넘기", "동작", "조회"))
    assert asked >= 2, "the reply does not ask for the missing decisions"


async def test_llm_new_agent_is_created_after_the_answers(builder, store):
    """S1 to the end: questions first, then a complete draft that uses registered tools only."""
    first = await builder.chat("쇼핑몰 배송 문의만 받는 에이전트 하나 만들어 줘")
    show("쇼핑몰 배송 문의만 받는 에이전트 하나 만들어 줘", first)
    before = store.list_agents()
    answers = ("배송 상태 조회만 해. 환불이나 주문 변경은 하지 않고 다른 창구를 안내해. 외부 동작은 본인 확인이랑 배송 조회 두 가지고 "
               "둘 다 조회야, 바꾸는 동작은 없어. 본인 확인은 주문번호와 휴대폰 뒤 4자리로 해. 고객이 사람을 찾거나 본인 확인에 "
               "세 번 실패하면 상담원에게 넘겨. id는 delivery-desk로 해 줘.")
    result = await builder.chat(answers, conversation_id=first["conversation_id"])
    show(answers, result)
    if "delivery-desk" not in store.list_agents():       # one more push is allowed
        result = await builder.chat("그대로 만들어 줘.", conversation_id=first["conversation_id"])
        show("그대로 만들어 줘.", result)
    created = [agent for agent in store.list_agents() if agent not in before]
    assert created, "no agent was created after the decisions were given"
    info = store.version_info(created[0], 1)
    definition = store.load(created[0], 1)
    assert (info.state, info.created_by) == ("draft", "builder") and store.deployment(created[0]).stable is None
    registered = set(builder.tool_catalog())
    assert definition.tools and set(definition.tools) <= registered
    assert "request_refund" not in definition.tools
    assert definition.flow and definition.handoff and definition.prohibitions and len(definition.tests.personas) >= 2
    assert any(criterion.safety for criterion in definition.evaluation.success)
    assert result["agent_id"] == created[0] and result["version"] == 1 and result["safety_change"] is True
    print("    tools:", definition.tools, "| flow:", [r.id for r in definition.flow], "| handoff:", [r.when for r in definition.handoff])
