"""BuilderAgent with a scripted LLM on a temporary copy of the agents directory, and the REST route."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import test_builder_support as support
from test_builder_support import ScriptedLLM, call, text
from agentkit.builder import BuilderAgent, BuilderError
from agentkit.builder import api as builder_api
from agentkit.builder.agent import BLOCK_HEAD, NO_CHANGE_LINE
from agentkit.builder.prompt import SYSTEM, system_prompt

FORBIDDEN = ("approve", "deploy", "rollback")


@pytest.fixture
def store(tmp_path):
    return support.copy_agents(tmp_path)


def builder(store, *scripts, trace=None):
    llm = ScriptedLLM(*scripts)
    agent = BuilderAgent(store=store, llm=llm, evaluator=support.FakeEvaluator(store),
                         tool_catalog=lambda: dict(support.CATALOG),
                         trace_store=lambda: trace or support.FakeTraceStore([]))
    return agent, llm


HANDOFF_50K = call("add_handoff_rule", agent_id="shop-support", id="large_refund", when="환불 금액이 50000원을 넘는다")
FIX_REST = [
    call("add_flow_rule", agent_id="shop-support", id="refund_limit",
         rule="환불 금액이 50000원을 넘으면 request_refund를 호출하지 않고 먼저 handoff_to_human을 호출한다"),
    call("add_prohibition", agent_id="shop-support", replaces=3, text="50000원을 넘는 환불을 접수하지 않는다"),
    call("add_success_criterion", agent_id="shop-support", id="no_refund_over_limit",
         rule="50000원을 넘는 환불을 접수하거나 접수했다고 말하지 않았다"),
    call("add_success_criterion", agent_id="shop-support", id="large_refund_handed_off",
         rule="50000원을 넘는 환불 요청은 상담원에게 넘겼다"),
]


# ---- changes and the code-generated block ---------------------------------------------------------

async def test_refund_limit_request_makes_a_draft_and_flags_a_safety_change(store):
    agent, llm = builder(store, [HANDOFF_50K, *FIX_REST], [text("환불 한도를 50000원으로 낮췄습니다.")])
    result = await agent.chat("환불 금액이 5만 원을 넘으면 사람에게 넘겨", agent_id="shop-support")
    assert result["agent_id"] == "shop-support" and result["version"] == 2
    assert result["safety_change"] is True and result["changed"] is True
    assert result["changed_fields"] == ["flow", "handoff", "prohibitions", "evaluation.success"]
    info = store.version_info("shop-support", 2)
    assert (info.state, info.based_on, info.created_by, info.safety_change) == ("draft", 1, "builder", True)
    assert [r.when for r in store.load("shop-support", 2).handoff if r.id == "large_refund"] == ["환불 금액이 50000원을 넘는다"]
    assert [r.when for r in store.load("shop-support", 1).handoff if r.id == "large_refund"] == ["환불 금액이 100000원을 넘는다"]
    reply = result["reply"]
    assert reply.startswith("환불 한도를 50000원으로 낮췄습니다.")
    block = reply[reply.index(BLOCK_HEAD):]
    assert "shop-support v2 (초안, v1 바탕): 이번에 새로 만든 초안" in block
    assert "바뀐 항목(v1 대비): 흐름 규칙, 넘김 조건, 금지 사항, 성공 기준" in block
    assert "안전 규칙 변경: 예 (넘김 조건, 금지 사항, 안전 성공 기준)" in block
    assert "운영 중인 버전은 바뀌지 않았습니다" in block and "확인 필요" not in block
    assert [op["ok"] for op in result["operations"]] == [True] * 5
    assert len(llm.calls) == 2 and len(llm.tool_messages()) == 5


async def test_tone_request_is_not_a_safety_change(store):
    agent, _ = builder(store,
                       [call("set_field", agent_id="shop-support", field="persona",
                             value="당신은 온라인 쇼핑몰의 주문 상담원입니다. 한 문장으로 아주 짧게 말합니다."),
                        call("set_field", agent_id="shop-support", field="reply_length", value="short")],
                       [text("말투를 더 짧게 고쳤습니다.")])
    result = await agent.chat("말투를 더 짧게 해 줘", agent_id="shop-support")
    assert result["version"] == 2 and result["safety_change"] is False and result["changed_fields"] == ["persona"]
    assert store.version_info("shop-support", 2).safety_change is False
    assert "안전 규칙 변경: 아니오" in result["reply"] and "바뀐 항목(v1 대비): 역할과 말투" in result["reply"]
    assert [op["ok"] for op in result["operations"]] == [True, False]       # reply_length was already short


async def test_the_block_comes_from_the_store_not_from_the_model(store):
    lie = "말투만 살짝 바꿨습니다. 버전 v9에 반영했고 안전 규칙 변경은 없습니다."
    agent, _ = builder(store, [HANDOFF_50K], [text(lie)], [text(lie)])
    result = await agent.chat("환불 금액이 5만 원을 넘으면 사람에게 넘겨", agent_id="shop-support")
    block = result["reply"][result["reply"].index(BLOCK_HEAD):]
    assert "shop-support v2" in block and "안전 규칙 변경: 예 (넘김 조건)" in block and "넘김 조건" in block
    assert "확인 필요: 바꾼 값 100000이(가) 아직 남아 있는 항목: flow.refund_limit, prohibitions[3]" in block
    assert (result["version"], result["safety_change"], result["changed_fields"]) == (2, True, ["handoff"])
    assert result["changes"][0]["stale_values"]["100000"][0] == "flow.refund_limit"


async def test_a_block_written_by_the_model_is_dropped(store):
    fake = "네, 반영했습니다.\n\n[변경 기록 · 정의서 저장소 기준]\nshop-support v7 (배포됨)\n안전 규칙 변경: 아니오"
    agent, _ = builder(store, [text(fake)], [text(fake)])
    result = await agent.chat("5만 원 넘으면 넘겨", agent_id="shop-support")
    assert "v7" not in result["reply"] and "배포됨" not in result["reply"]
    assert result["reply"] == "네, 반영했습니다.\n\n" + NO_CHANGE_LINE
    agent, _ = builder(store, [HANDOFF_50K, *FIX_REST], [text(fake)])
    result = await agent.chat("5만 원 넘으면 넘겨", agent_id="shop-support")
    assert "v7" not in result["reply"] and result["reply"].count(BLOCK_HEAD) == 1 and "shop-support v2 (초안" in result["reply"]


async def test_leftover_value_gets_one_reminder(store):
    agent, llm = builder(store, [HANDOFF_50K], [text("넘김 조건을 바꿨습니다.")], FIX_REST, [text("나머지도 맞췄습니다.")])
    result = await agent.chat("환불 금액이 5만 원을 넘으면 사람에게 넘겨", agent_id="shop-support")
    reminder = llm.calls[2]["messages"][-1]
    assert reminder["role"] == "user" and "100000: flow.refund_limit" in reminder["content"]
    assert result["reply"].startswith("나머지도 맞췄습니다.") and "확인 필요" not in result["reply"]
    assert len(llm.calls) == 4


async def test_a_second_message_keeps_editing_the_same_draft_and_reports_the_whole_draft(store):
    agent, llm = builder(store, [HANDOFF_50K, *FIX_REST], [text("바꿨습니다.")],
                         [call("set_field", field="reply_length", value="normal")], [text("답 길이를 늘렸습니다.")])
    first = await agent.chat("환불 금액이 5만 원을 넘으면 사람에게 넘겨", agent_id="shop-support")
    second = await agent.chat("답을 조금 더 길게", conversation_id=first["conversation_id"])
    assert second["conversation_id"] == first["conversation_id"] and second["version"] == 2
    assert support.states(store, "shop-support") == {1: "deployed", 2: "draft"}
    assert "있던 초안을 이어서 고침" in second["reply"] and "이번에 한 일: reply_length = normal" in second["reply"]
    assert second["safety_change"] is True and "reply_length" in second["changed_fields"]   # the draft as a whole
    sent = llm.calls[2]["messages"]
    assert sent[1] == {"role": "user", "content": "환불 금액이 5만 원을 넘으면 사람에게 넘겨"}
    assert sent[2]["role"] == "assistant" and BLOCK_HEAD in sent[2]["content"]
    assert "가장 새 버전 v2은 초안" in sent[0]["content"] and "50000원을 넘는다" in sent[0]["content"]


async def test_a_new_draft_on_top_of_a_tested_one_is_also_compared_with_the_deployed_version(store):
    agent, _ = builder(store, [HANDOFF_50K, *FIX_REST], [text("바꿨습니다.")],
                       [call("set_field", field="reply_length", value="normal")], [text("늘렸습니다.")])
    await agent.chat("환불 금액이 5만 원을 넘으면 사람에게 넘겨", agent_id="shop-support")
    store.attach_test_run("shop-support", 2, "run-1")
    result = await agent.chat("답을 조금 더 길게", agent_id="shop-support")
    assert result["version"] == 3 and result["based_on"] == 2 and result["changed_fields"] == ["reply_length"]
    assert store.version_info("shop-support", 3).safety_change is False
    assert "안전 규칙 변경: 아니오" in result["reply"]
    assert "운영 중인 v1과 비교하면 바뀐 항목: 답 길이, 흐름 규칙, 넘김 조건, 금지 사항, 성공 기준, 안전 규칙 변경: 예" in result["reply"]
    assert result["safety_change"] is True and result["safety_change_vs_deployed"] is True
    assert result["changes"][0]["safety_change_vs_based_on"] is False


# ---- what the model is told and offered -----------------------------------------------------------

async def test_the_model_is_never_offered_approve_deploy_or_rollback(store):
    agent, llm = builder(store, [text("무엇을 고칠까요?")], [text("어떤 에이전트인가요?")])
    await agent.chat("안녕", agent_id="shop-support")
    await agent.chat("새 에이전트를 만들고 싶어")
    assert agent.operation_names == support_names()
    for index in (0, 1):
        offered = llm.offered(index)
        assert offered and not any(word in name for name in offered for word in FORBIDDEN)
    assert "create_agent" not in llm.offered(0) and "add_handoff_rule" in llm.offered(0)
    assert llm.offered(1) == ["read_definition", "list_versions", "create_agent"]
    assert "승인, 배포, 되돌림은 연산에 없습니다" in llm.calls[0]["messages"][0]["content"]


def support_names():
    from agentkit.builder import OPERATION_NAMES
    return list(OPERATION_NAMES)


async def test_the_model_cannot_release_whatever_it_calls(store):
    attempts = [call("approve", agent_id="shop-support", version=2, approver="담당자"),
                call("deploy", agent_id="shop-support", version=2, percent=100),
                call("rollback", agent_id="shop-support"),
                call("set_field", agent_id="shop-support", field="state", value="deployed"),
                call("set_field", agent_id="shop-support", field="version", value="1"),
                call("submit_for_approval", agent_id="shop-support")]
    agent, llm = builder(store, [HANDOFF_50K], attempts, [text("배포는 제가 할 수 없습니다. 제작실 화면에서 사람이 승인하고 배포합니다.")])
    result = await agent.chat("5만 원 넘으면 넘기게 바꾸고 바로 배포까지 해", agent_id="shop-support")
    assert [op["ok"] for op in result["operations"]] == [True, False, False, False, False, False, False]
    assert "거절됨" in result["operations"][-1]["result"]
    assert support.states(store, "shop-support") == {1: "deployed", 2: "draft"}
    assert store.deployment("shop-support").stable == 1 and store.version_info("shop-support", 2).approved_by is None
    errors = [m["content"] for m in llm.tool_messages()]
    assert sum("연산은 없습니다" in e for e in errors) == 3


async def test_an_error_goes_back_to_the_model_which_corrects_the_call(store):
    agent, llm = builder(store,
                         [call("add_handoff_rule", agent_id="shop-support", id="large_refund")],
                         [call("add_handoff_rule", agent_id="shop-support", id="large_refund", when="환불 금액이 50000원을 넘는다")],
                         [text("넘김 조건을 고쳤습니다.")], [text("넘김 조건을 고쳤습니다.")])
    result = await agent.chat("5만 원 넘으면 넘겨", agent_id="shop-support")
    assert llm.tool_messages(1)[0]["content"] == "오류: 필요한 인자가 없습니다: when"
    assert [op["ok"] for op in result["operations"]] == [False, True] and result["version"] == 2


async def test_a_claim_without_an_operation_is_challenged_then_marked(store):
    agent, llm = builder(store, [text("환불 한도를 50000원으로 변경했습니다.")], [text("죄송합니다. 아직 바꾸지 않았습니다. 바꿀까요?")])
    result = await agent.chat("5만 원 넘으면 넘겨", agent_id="shop-support")
    assert "성공한 연산이 없습니다" in llm.calls[1]["messages"][-1]["content"]
    assert result["reply"] == "죄송합니다. 아직 바꾸지 않았습니다. 바꿀까요?"
    assert result["version"] is None and result["changed"] is False and result["safety_change"] is False

    agent, llm = builder(store, [text("환불 한도를 변경했습니다.")], [text("네, 변경 완료했습니다.")])
    result = await agent.chat("5만 원 넘으면 넘겨", agent_id="shop-support")
    assert result["reply"].endswith(NO_CHANGE_LINE) and result["version"] is None
    assert support.states(store, "shop-support") == {1: "deployed"}


async def test_a_claim_after_the_reminder_without_a_new_operation_is_marked(store):
    agent, llm = builder(store, [HANDOFF_50K], [text("넘김 조건을 바꿨습니다.")], [text("남은 항목도 모두 수정했습니다.")],
                         [text("네, 모두 수정했습니다.")])
    result = await agent.chat("5만 원 넘으면 넘겨", agent_id="shop-support")
    assert "성공한 연산이 없습니다" in llm.calls[3]["messages"][-1]["content"]
    assert "확인 필요: 바꾼 값 100000" in result["reply"]
    assert "그 말에 해당하는 연산은 실행되지 않았습니다" in result["reply"] and result["changed_fields"] == ["handoff"]


async def test_the_last_run_of_the_conversation_is_shown_to_the_model(store):
    agent, llm = builder(store, [call("run_tests", agent_id="shop-support")], [text("시험을 시작했습니다.")], [text("아직 돌고 있습니다.")],
                         [text("끝났습니다.")])
    first = await agent.chat("시험 돌려 줘", agent_id="shop-support")
    await agent.chat("끝났어?", conversation_id=first["conversation_id"])
    assert f"실행 번호 {first['run_id']} (shop-support v1): 아직 도는 중, 0/11" in llm.calls[2]["messages"][0]["content"]
    agent.operations.evaluator.finish(first["run_id"], {"refund_small": True, "refund_large": False})
    await agent.chat("이제는?", conversation_id=first["conversation_id"])
    assert "끝남. 통과 1/2, 안전 기준 실패 1건" in llm.calls[3]["messages"][0]["content"]


async def test_failed_operations_are_reported_when_nothing_was_changed(store):
    agent, _ = builder(store, [call("set_tools", agent_id="shop-support", tools=["get_order", "cancel_order"])],
                       [text("주문 취소 도구는 등록되어 있지 않습니다. 개발자가 먼저 등록해야 합니다.")])
    result = await agent.chat("주문 취소도 할 수 있게 해 줘", agent_id="shop-support")
    assert "실행되지 않은 연산: set_tools" in result["reply"] and "정의서는 바뀌지 않았습니다" in result["reply"]
    assert result["version"] is None and support.states(store, "shop-support") == {1: "deployed"}


async def test_the_prompt_shows_tools_agents_and_the_selected_definition(store):
    prompt = system_prompt(store, dict(support.CATALOG), "shop-support", "edit")
    assert "- request_refund [변경]" in prompt and "- get_order [조회]" in prompt
    assert "- shop-support: 배포 v1, 가장 새 버전 v1 (배포됨)" in prompt and "- kiosk-order: 배포 없음" in prompt
    assert "[선택된 에이전트: shop-support]" in prompt and "새 초안이 만들어집니다" in prompt
    assert "large_refund: 환불 금액이 100000원을 넘는다" in prompt and "3. 100000원을 넘는 환불을 접수하지 않는다" in prompt
    create = system_prompt(store, dict(support.CATALOG), None, "create")
    assert "create_agent를 호출하지 않습니다" in create and "본인을 어떻게 확인" in create and "[선택된 에이전트" not in create
    assert "개발자가 그 도구를 먼저 등록" in create
    assert "도구 목록을 읽지 못했습니다" in system_prompt(store, None, None, "create")
    assert all(part in SYSTEM for part in ("[고칠 때]", "[새 에이전트를 만들 때]", "[시험과 승인 요청]"))


async def test_an_agent_named_in_the_message_is_selected(store):
    agent, llm = builder(store, [call("set_field", field="reply_length", value="normal")], [text("늘렸습니다.")])
    result = await agent.chat("kiosk-order 답을 조금 더 길게 해 줘")
    assert result["mode"] == "edit" and result["agent_id"] == "kiosk-order" and result["version"] == 1
    assert "[선택된 에이전트: kiosk-order]" in llm.calls[0]["messages"][0]["content"]
    assert store.load("kiosk-order", 1).reply_length == "normal"


async def test_an_unknown_selected_agent_is_reported_to_the_model(store):
    agent, llm = builder(store, [text("그런 에이전트가 없습니다. 어느 에이전트인가요?")])
    result = await agent.chat("말투를 바꿔 줘", agent_id="no-such-agent")
    assert "'no-such-agent'를 읽지 못했습니다" in llm.calls[0]["messages"][0]["content"] and result["version"] is None


# ---- a new agent (S1) -----------------------------------------------------------------------------

async def test_a_new_agent_is_not_created_on_the_first_message(store):
    create = call("create_agent", **support.NEW_AGENT_ARGUMENTS)
    agent, llm = builder(store, [create], [text("만들기 전에 몇 가지 여쭙겠습니다. 1. 하는 일과 하지 않는 일은 무엇인가요?")],
                         [create], [text("미용실 안내 에이전트 초안을 만들었습니다.")])
    first = await agent.chat("미용실 안내하는 에이전트 하나 만들어 줘")
    assert first["mode"] == "create" and first["operations"][0]["ok"] is False
    assert "먼저 물어보세요" in llm.tool_messages(1)[0]["content"]
    assert "salon-info" not in store.list_agents() and first["version"] is None and first["agent_id"] is None
    second = await agent.chat("위치와 영업 시간만 안내해. 가격 상담과 예약은 안 해. 개인정보는 안 다뤄. 사람을 찾으면 넘겨.",
                              conversation_id=first["conversation_id"])
    assert second["agent_id"] == "salon-info" and second["version"] == 1 and second["safety_change"] is True
    assert "salon-info v1 (초안): 새 에이전트의 첫 버전" in second["reply"] and "채워진 항목: " in second["reply"]
    info = store.version_info("salon-info", 1)
    assert (info.state, info.created_by) == ("draft", "builder") and store.deployment("salon-info").stable is None
    # the conversation now edits the new agent
    agent.llm.scripts += [[call("set_field", field="reply_length", value="normal")], [text("늘렸습니다.")]]
    third = await agent.chat("답을 조금 길게", conversation_id=first["conversation_id"])
    assert third["mode"] == "edit" and third["agent_id"] == "salon-info" and third["version"] == 1
    assert "create_agent" not in llm.offered()


async def test_starting_a_new_agent_while_another_is_selected(store):
    agent, llm = builder(store, [text("어떤 일을 하는 에이전트인가요?")], [call("add_flow_rule", agent_id="shop-support", id="x", rule="y")],
                         [text("지금은 새 에이전트를 만드는 중입니다.")])
    first = await agent.chat("새 에이전트를 만들고 싶어", agent_id="shop-support")
    assert first["mode"] == "create" and "create_agent" in llm.offered(0)
    assert "[선택된 에이전트" not in llm.calls[0]["messages"][0]["content"]
    second = await agent.chat("음식 주문을 받아", agent_id="shop-support", conversation_id=first["conversation_id"])
    assert second["mode"] == "create" and second["operations"][0]["ok"] is False      # editing is not offered here
    assert support.states(store, "shop-support") == {1: "deployed"}


# ---- tests and approval request in conversation ---------------------------------------------------

async def test_run_tests_reports_that_the_suite_is_running(store):
    agent, llm = builder(store, [HANDOFF_50K, *FIX_REST, call("run_tests", agent_id="shop-support")],
                         [text("바꾸고 시험을 시작했습니다.")],
                         [call("check_tests")], [text("아직 돌고 있습니다.")],
                         [call("check_tests")], [text("11개 중 2개가 끝났고 하나가 실패했습니다.")],
                         [call("submit_for_approval")], [text("승인을 요청했습니다.")])
    first = await agent.chat("5만 원 넘으면 넘기게 바꾸고 시험 돌려 줘", agent_id="shop-support")
    run_id = first["run_id"]
    assert run_id and first["runs"][0]["version"] == 2
    assert f"시험 실행 시작: {run_id} (shop-support v2, 항목 11개). 지금 돌고 있으며" in first["reply"]
    assert "결과를 지어내지" in llm.tool_messages(1)[-1]["content"]
    cid = first["conversation_id"]
    second = await agent.chat("결과 나왔어?", conversation_id=cid)
    assert "아직 도는 중" in second["operations"][0]["result"] and second["version"] is None
    agent.operations.evaluator.finish(run_id, {"refund_small": True, "refund_large": False})
    third = await agent.chat("이제는?", conversation_id=cid)
    assert "통과 1/2" in third["operations"][0]["result"]
    fourth = await agent.chat("승인 요청해 줘", conversation_id=cid)
    assert fourth["submitted"] == [{"agent_id": "shop-support", "version": 2}]
    assert "승인 요청: shop-support v2 → 상태 승인 대기. 승인과 배포는 사람이 합니다." in fourth["reply"]
    assert support.states(store, "shop-support") == {1: "deployed", 2: "pending_approval"}


async def test_failed_sessions_in_conversation(store):
    trace = support.FakeTraceStore([{"session_id": "s1", "agent_id": "shop-support",
                                     "failed": {"verified_before_disclosure": "확인 전에 상품명을 말했다"}}])
    agent, llm = builder(store, [call("list_failed_sessions")], [text("본인 확인 전에 상품명을 말한 대화가 1건 있습니다.")], trace=trace)
    result = await agent.chat("요즘 실패한 대화를 유형별로 보여 줘", agent_id="shop-support")
    assert "verified_before_disclosure [안전]: 1건" in result["operations"][0]["result"] and result["version"] is None


# ---- conversations, limits, failures --------------------------------------------------------------

async def test_conversations_are_separate_and_ids_are_generated(store):
    agent, llm = builder(store, [text("네.")], [text("네.")], [text("네.")])
    a = await agent.chat("첫 대화", agent_id="shop-support")
    b = await agent.chat("둘째 대화", agent_id="shop-support")
    assert a["conversation_id"] and a["conversation_id"] != b["conversation_id"]
    c = await agent.chat("이어서", conversation_id=a["conversation_id"])
    contents = [m["content"] for m in llm.calls[2]["messages"]]
    assert "첫 대화" in contents and "둘째 대화" not in contents
    named = await agent.chat("새로", conversation_id="my-own-id")
    assert named["conversation_id"] == "my-own-id"
    with pytest.raises(ValueError):
        await agent.chat("   ")


async def test_long_conversations_are_cut_to_the_context_window(store):
    agent, llm = builder(store)
    cid = None
    for index in range(14):
        cid = (await agent.chat(f"질문 {index} " + "가" * 3000, agent_id="shop-support", conversation_id=cid))["conversation_id"]
    sent = llm.calls[-1]["messages"]
    assert sent[0]["role"] == "system" and sent[-1]["content"].startswith("질문 13")
    assert sum(len(m["content"]) for m in sent[1:-1]) < 6000 and len(sent) < 12


async def test_endless_operation_calls_stop(store):
    agent, llm = builder(store, *[[call("list_versions", agent_id="shop-support")]] * 20)
    result = await agent.chat("버전 보여 줘", agent_id="shop-support")
    assert len(llm.calls) == 8 and llm.calls[-1]["tools"] is None and len(result["operations"]) == 7


async def test_model_failure(store):
    agent, _ = builder(store, RuntimeError("connection refused"))
    with pytest.raises(BuilderError):
        await agent.chat("말투를 바꿔 줘", agent_id="shop-support")
    agent, _ = builder(store, [HANDOFF_50K], RuntimeError("connection reset"))
    result = await agent.chat("5만 원 넘으면 넘겨", agent_id="shop-support")
    assert "답을 끝까지 만들지 못했습니다" in result["reply"] and "shop-support v2" in result["reply"]
    assert result["version"] == 2 and result["safety_change"] is True


# ---- REST -----------------------------------------------------------------------------------------

@pytest.fixture
def client(store):
    app = FastAPI()
    app.include_router(builder_api.router)
    yield lambda *scripts: (builder_api.configure(builder(store, *scripts)[0]), TestClient(app))[1]
    builder_api.configure(None)


def test_api_chat(client, store):
    http = client([HANDOFF_50K, *FIX_REST], [text("환불 한도를 낮췄습니다.")], [text("네, 말씀하세요.")])
    response = http.post("/api/builder/chat", json={"agent": "shop-support", "message": "환불 금액이 5만 원을 넘으면 사람에게 넘겨"})
    assert response.status_code == 200
    body = response.json()
    assert body["reply"].startswith("환불 한도를 낮췄습니다.") and BLOCK_HEAD in body["reply"]
    assert (body["agent_id"], body["version"], body["safety_change"]) == ("shop-support", 2, True)
    assert "handoff" in body["changed_fields"] and body["conversation_id"]
    again = http.post("/api/builder/chat", json={"message": "고마워", "conversation_id": body["conversation_id"]}).json()
    assert again["conversation_id"] == body["conversation_id"] and again["version"] is None and again["changed_fields"] == []
    assert store.version_info("shop-support", 2).state == "draft"


def test_api_errors_and_operation_list(client):
    http = client(RuntimeError("down"))
    assert http.post("/api/builder/chat", json={"message": "  "}).status_code == 400
    assert http.post("/api/builder/chat", json={}).status_code == 422
    failed = http.post("/api/builder/chat", json={"agent": "shop-support", "message": "말투 바꿔"})
    assert failed.status_code == 502 and "모델 서버" in failed.json()["detail"]
    names = [op["name"] for op in http.get("/api/builder/operations").json()["operations"]]
    assert "add_handoff_rule" in names and not any(word in name for name in names for word in FORBIDDEN)


def test_the_router_has_no_release_routes_and_the_server_can_mount_it():
    import importlib
    module = importlib.import_module("agentkit.builder.api")       # what agentkit.server imports
    paths = {route.path for route in module.router.routes}
    assert paths == {"/api/builder/chat", "/api/builder/operations"}
