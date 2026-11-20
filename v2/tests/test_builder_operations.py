"""The builder's operations, one by one, on a temporary copy of the agents directory (no LLM)."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import pytest

import test_builder_support as support
from agentkit.builder import MODIFYING, OPERATION_NAMES, SPECS, Operations, Turn
from agentkit.builder.operations import check_arguments, specs_for, OperationError
from agentkit.definition import DefinitionStore

FORBIDDEN = ("approve", "deploy", "rollback", "release", "publish", "canary")


@pytest.fixture
def store(tmp_path):
    return support.copy_agents(tmp_path)


@pytest.fixture
def ops(store):
    return Operations(store, support.FakeEvaluator(store), lambda: dict(support.CATALOG))


async def run(ops, name, turn=None, /, **arguments):
    turn = turn or Turn()
    ok, text = await ops.execute(turn, name, arguments)
    return ok, text, turn


def test_the_copy_is_not_the_real_directory(store):
    assert store.root != support.REAL_AGENTS and "shop-support" in store.list_agents()


# ---- the operation list ---------------------------------------------------------------------------

def test_operation_list_is_the_documented_one():
    assert OPERATION_NAMES == [
        "read_definition", "list_versions", "create_agent", "create_draft", "set_field", "add_flow_rule",
        "remove_flow_rule", "add_prohibition", "add_handoff_rule", "add_guard", "set_tools",
        "add_success_criterion", "add_extract_field", "add_persona", "run_tests", "check_tests", "compare_runs",
        "submit_for_approval", "list_failed_sessions"]
    assert set(MODIFYING) < set(OPERATION_NAMES)


def test_no_operation_can_approve_deploy_or_roll_back(ops):
    for name in OPERATION_NAMES:
        assert not any(word in name for word in FORBIDDEN), name
    for mode in ("create", "edit"):
        for spec in specs_for(mode):
            assert not any(word in spec["function"]["name"] for word in FORBIDDEN)
    handlers = [name for name in dir(ops) if name.startswith("_op_")]
    assert sorted(handlers) == sorted("_op_" + name for name in OPERATION_NAMES)
    assert all(spec["function"]["name"] in OPERATION_NAMES for spec in SPECS)


async def test_asking_for_approve_or_deploy_by_name_is_an_error(ops, store):
    for name in ("approve", "deploy", "rollback", "approve_version", "store.deploy"):
        ok, text, _ = await run(ops, name, agent_id="shop-support", version=1, approver="builder")
        assert not ok and "없습니다" in text
    assert support.states(store, "shop-support") == {1: "deployed"}
    assert store.deployment("shop-support").stable == 1


def test_modes_offer_different_sets():
    create = [spec["function"]["name"] for spec in specs_for("create")]
    edit = [spec["function"]["name"] for spec in specs_for("edit")]
    assert create == ["read_definition", "list_versions", "create_agent"]
    assert "create_agent" not in edit and "add_handoff_rule" in edit and len(edit) == len(OPERATION_NAMES) - 1


async def test_an_operation_that_was_not_offered_is_refused(ops, store):
    turn = Turn(user_turns=5)
    ok, text = await ops.execute(turn, "create_agent", dict(support.NEW_AGENT_ARGUMENTS), ["read_definition"])
    assert not ok and "지금 쓸 수 없습니다" in text and "salon-info" not in store.list_agents()


# ---- reading --------------------------------------------------------------------------------------

async def test_read_definition_and_list_versions(ops):
    ok, text, _ = await run(ops, "read_definition", agent_id="shop-support")
    assert ok and "shop-support v1 (배포됨)" in text and "large_refund: 환불 금액이 100000원을 넘는다" in text
    assert "verified_before_disclosure [안전]" in text and "refund_small" in text
    ok, text, _ = await run(ops, "read_definition", agent_id="shop-support", part="guards")
    assert ok and "no_fake_refund" in text and "pattern" in text
    ok, text, _ = await run(ops, "list_versions", agent_id="shop-support")
    assert ok and "배포(stable) v1" in text and "- v1 배포됨" in text


# ---- drafts ---------------------------------------------------------------------------------------

async def test_first_change_creates_a_draft_from_the_deployed_version(ops, store):
    ok, text, turn = await run(ops, "add_flow_rule", agent_id="shop-support", id="greet", rule="먼저 인사한다")
    assert ok and "v1 바탕으로 새로 만듦" in text
    info = store.version_info("shop-support", 2)
    assert (info.state, info.based_on, info.created_by) == ("draft", 1, "builder")
    assert turn.changes == {("shop-support", 2): {"created": True, "ops": ["흐름 규칙 greet 더함: 먼저 인사한다"]}}
    assert "greet" not in [rule.id for rule in store.load("shop-support", 1).flow]     # v1 untouched
    assert store.deployment("shop-support").stable == 1


async def test_further_changes_edit_the_same_draft(ops, store):
    turn = Turn()
    await ops.execute(turn, "add_flow_rule", {"agent_id": "shop-support", "id": "greet", "rule": "먼저 인사한다"})
    ok, text = await ops.execute(turn, "set_field", {"agent_id": "shop-support", "field": "reply_length", "value": "normal"})
    assert ok and "새로 만듦" not in text
    ok, _, later = await run(ops, "add_prohibition", agent_id="shop-support", text="욕설에 맞대응하지 않는다")
    assert ok and later.changes[("shop-support", 2)]["created"] is False
    assert support.states(store, "shop-support") == {1: "deployed", 2: "draft"}
    info = store.version_info("shop-support", 2)
    assert info.changed_fields == ["reply_length", "flow", "prohibitions"] and "greet" in info.note


async def test_an_existing_draft_made_by_a_person_is_edited_in_place(ops, store):
    assert support.states(store, "kiosk-order") == {1: "draft"}
    ok, _, turn = await run(ops, "set_field", agent_id="kiosk-order", field="reply_length", value="normal")
    assert ok and list(turn.changes) == [("kiosk-order", 1)] and turn.changes[("kiosk-order", 1)]["created"] is False
    assert support.states(store, "kiosk-order") == {1: "draft"}


async def test_a_tested_version_is_not_edited_a_new_draft_continues_from_it(ops, store):
    await run(ops, "add_handoff_rule", agent_id="shop-support", id="large_refund", when="환불 금액이 50000원을 넘는다")
    store.attach_test_run("shop-support", 2, "run-x")
    assert store.version_info("shop-support", 2).state == "tested"
    ok, text, _ = await run(ops, "set_field", agent_id="shop-support", field="reply_length", value="normal")
    assert ok and "v2 바탕으로 새로 만듦" in text
    assert support.states(store, "shop-support") == {1: "deployed", 2: "tested", 3: "draft"}
    third = store.load("shop-support", 3)
    assert third.reply_length == "normal"
    assert [rule.when for rule in third.handoff if rule.id == "large_refund"] == ["환불 금액이 50000원을 넘는다"]
    assert store.load("shop-support", 2).reply_length == "short"         # the tested version is as it was
    assert store.version_info("shop-support", 3).based_on == 2


async def test_a_refused_change_leaves_no_draft_behind(ops, store):
    for name, arguments in (
            ("add_guard", {"id": "bad", "pattern": "(", "nudge": "x"}),
            ("set_field", {"field": "reply_length", "value": "tiny"}),
            ("set_field", {"field": "handoff", "value": "x"}),
            ("remove_flow_rule", {"id": "no_such_rule"}),
            ("add_prohibition", {"text": "x", "replaces": 99}),
            ("add_persona", {"id": "p", "description": "d", "goal": "g", "expect": {"no_such_criterion": True}}),
            ("set_tools", {"tools": ["get_order", "book_table"]}),
            ("add_handoff_rule", {"id": "large_refund", "when": "환불 금액이 100000원을 넘는다"})):   # no difference
        ok, text, turn = await run(ops, name, agent_id="shop-support", **arguments)
        assert not ok and text.startswith("오류"), (name, text)
        assert not turn.changes
    assert support.states(store, "shop-support") == {1: "deployed"}


async def test_create_draft_reuses_an_open_draft(ops, store):
    ok, text, turn = await run(ops, "create_draft", agent_id="shop-support")
    assert ok and store.version_info("shop-support", 2).created_by == "builder" and turn.changes
    ok, text, _ = await run(ops, "create_draft", agent_id="shop-support")
    assert ok and "이미 초안 v2" in text and support.states(store, "shop-support") == {1: "deployed", 2: "draft"}


# ---- safety flag (from the store) -----------------------------------------------------------------

@pytest.mark.parametrize("name, arguments, safety, field", [
    ("add_handoff_rule", {"id": "large_refund", "when": "환불 금액이 50000원을 넘는다"}, True, "handoff"),
    ("add_handoff_rule", {"id": "angry", "when": "고객이 욕설을 한다"}, True, "handoff"),
    ("add_prohibition", {"text": "50000원을 넘는 환불을 접수하지 않는다", "replaces": 3}, True, "prohibitions"),
    ("add_prohibition", {"text": "경쟁사를 언급하지 않는다"}, True, "prohibitions"),
    ("set_tools", {"tools": ["verify_customer", "get_order", "track_delivery", "handoff_to_human"]}, True, "tools"),
    ("add_guard", {"id": "no_swear", "pattern": "바보", "nudge": "(시스템 안내: 그런 말은 하지 않습니다.)"}, True, "guards"),
    ("add_success_criterion", {"id": "no_promise", "rule": "보상을 약속하지 않았다", "safety": True}, True, "evaluation.success"),
    ("add_extract_field", {"name": "customer_phone", "pii": True}, True, "evaluation.extract"),
    ("set_field", {"field": "persona", "value": "당신은 쇼핑몰 상담원입니다. 한 문장으로 아주 짧게 말합니다."}, False, "persona"),
    ("set_field", {"field": "reply_length", "value": "normal"}, False, "reply_length"),
    ("set_field", {"field": "live_clock", "value": "false"}, False, "live_clock"),
    ("set_field", {"field": "latency_budget_ms", "value": "3000"}, False, "evaluation.latency_budget_ms"),
    ("set_field", {"field": "voice.tts_voice", "value": "M1"}, False, "voice"),
    ("add_flow_rule", {"id": "closing", "rule": "문의가 끝나면 짧게 인사하고 마친다"}, False, "flow"),
    ("remove_flow_rule", {"id": "closing"}, False, "flow"),
    ("add_success_criterion", {"id": "polite", "rule": "정중하게 말했다"}, False, "evaluation.success"),
    ("add_extract_field", {"name": "mood", "type": "enum", "values": ["좋음", "나쁨"]}, False, "evaluation.extract"),
    ("add_persona", {"id": "quiet", "description": "말수가 적은 고객", "goal": "배송 상태를 묻는다",
                     "facts": {"order_id": "A1003"}, "expect": {"no_handoff": True}}, False, "tests.personas"),
])
async def test_safety_change_follows_the_store(ops, store, name, arguments, safety, field):
    ok, text, turn = await run(ops, name, agent_id="shop-support", **arguments)
    assert ok, text
    info = store.version_info("shop-support", 2)
    assert info.safety_change is safety and info.changed_fields == [field]
    assert ("안전 규칙 변경: 예" in text) is safety
    assert info.state == "draft"


async def test_refund_limit_change_and_the_leftover_value_hint(ops, store):
    turn = Turn()
    ok, text = await ops.execute(turn, "add_handoff_rule", {"agent_id": "shop-support", "id": "large_refund",
                                                           "when": "환불 금액이 5만 원을 넘는다"})
    assert ok and "주의: 값 100000" in text and "flow.refund_limit" in text and "prohibitions[3]" in text
    ok, text = await ops.execute(turn, "add_flow_rule", {
        "agent_id": "shop-support", "id": "refund_limit",
        "rule": "환불 금액이 50000원을 넘으면 request_refund를 호출하지 않고 handoff_to_human을 호출한다"})
    assert ok and "flow.refund_limit" not in text.split("주의")[1]
    await ops.execute(turn, "add_prohibition", {"agent_id": "shop-support", "replaces": 3,
                                                "text": "50,000원을 넘는 환불을 접수하지 않는다"})
    await ops.execute(turn, "add_success_criterion", {"agent_id": "shop-support", "id": "no_refund_over_limit",
                                                      "rule": "50000원을 넘는 환불을 접수하지 않았다"})
    ok, text = await ops.execute(turn, "add_success_criterion", {"agent_id": "shop-support", "id": "large_refund_handed_off",
                                                                "rule": "50000원을 넘는 환불 요청은 상담원에게 넘겼다"})
    assert ok and "주의" not in text
    draft = store.load("shop-support", 2)
    ok, text = await ops.execute(turn, "add_success_criterion", {"agent_id": "shop-support", "id": "inquiry_resolved",
                                                                "rule": "고객의 문의를 처리했다", "safety": True})
    assert ok and "안전 기준 표시 아니오→예" in turn.changes[("shop-support", 2)]["ops"][-1]
    draft = store.load("shop-support", 2)
    kept = {criterion.id: criterion.safety for criterion in draft.evaluation.success}
    assert kept["no_refund_over_limit"] is True and kept["large_refund_handed_off"] is False   # safety kept as it was
    assert len(draft.handoff) == 4 and len(draft.prohibitions) == 7                             # replaced, not added
    info = store.version_info("shop-support", 2)
    assert info.safety_change and set(info.changed_fields) == {"flow", "handoff", "prohibitions", "evaluation.success"}


# ---- arguments ------------------------------------------------------------------------------------

@pytest.mark.parametrize("name, arguments, expected", [
    ("add_handoff_rule", {"agent_id": "shop-support", "id": "x"}, "필요한 인자가 없습니다: when"),
    ("add_handoff_rule", {"agent_id": "shop-support", "id": "x", "when": ""}, "필요한 인자가 없습니다: when"),
    ("add_handoff_rule", {"agent_id": "shop-support", "id": "x", "when": "y", "priority": 1}, "priority: 모르는 인자"),
    ("add_handoff_rule", {"agent_id": "shop-support", "id": "큰 환불", "when": "y"}, "쓸 수 없습니다"),
    ("add_handoff_rule", {"agent_id": "no-such-agent", "id": "x", "when": "y"}, "에이전트 'no-such-agent'가 없습니다"),
    ("add_handoff_rule", {"agent_id": "../etc", "id": "x", "when": "y"}, "없습니다"),
    ("add_handoff_rule", "not json", "JSON 객체"),
    ("add_prohibition", {"agent_id": "shop-support", "text": "x", "replaces": "셋"}, "replaces: 정수"),
    ("set_tools", {"agent_id": "shop-support", "tools": [1, {"a": 1}]}, "tools[1]: 글자"),
    ("set_tools", {"agent_id": "shop-support", "tools": ["get_order", "cancel_booking"]}, "개발자가 그 도구를 먼저 등록"),
    ("add_success_criterion", {"agent_id": "shop-support", "id": "m", "rule": "r", "judge": "vote"}, "judge: 'vote'"),
    ("add_success_criterion", {"agent_id": "shop-support", "id": "m", "rule": "r", "judge": "metric"}, "오류"),
    ("add_extract_field", {"agent_id": "shop-support", "name": "kind", "type": "enum"}, "오류"),
    ("add_persona", {"agent_id": "shop-support", "id": "p", "description": "d", "goal": "g", "expect": {"no_handoff": "yes"}},
     "expect.no_handoff: true 또는 false"),
    ("add_guard", {"agent_id": "shop-support", "id": "g", "kind": "claims_lookup", "pattern": "x", "nudge": "n"}, "오류"),
    ("read_definition", {"agent_id": "shop-support", "version": 7}, "버전 7"),
    ("read_definition", {"agent_id": "shop-support", "part": "everything"}, "part: 'everything'"),
    ("run_tests", {"agent_id": "shop-support", "version": "latest"}, "version: 정수"),
    ("no_such_operation", {}, "연산은 없습니다"),
])
async def test_invalid_arguments_come_back_as_text(ops, store, name, arguments, expected):
    turn = Turn()
    ok, text = await ops.execute(turn, name, arguments)
    assert not ok and expected in text, text
    assert not turn.changes and turn.log[-1]["ok"] is False
    assert support.states(store, "shop-support") == {1: "deployed"}


def test_arguments_are_coerced_where_the_meaning_is_clear():
    checked = check_arguments("add_persona", {"agent_id": "a", "id": "p", "description": "d", "goal": "g",
                                              "max_turns": "6", "facts": '{"order_id": "A1001"}', "opening": None})
    assert checked["max_turns"] == 6 and checked["facts"] == {"order_id": "A1001"} and "opening" not in checked
    assert check_arguments("set_tools", {"agent_id": "a", "tools": '["x", "y"]'})["tools"] == ["x", "y"]
    assert check_arguments("add_extract_field", {"agent_id": "a", "name": "n", "pii": "true"})["pii"] is True
    with pytest.raises(OperationError):
        check_arguments("add_extract_field", {"agent_id": "a", "name": "n", "pii": "maybe"})


async def test_the_selected_agent_fills_a_missing_agent_id(ops, store):
    turn = Turn(default_agent="shop-support")
    ok, _ = await ops.execute(turn, "set_field", {"field": "reply_length", "value": "normal"})
    assert ok and store.load("shop-support", 2).reply_length == "normal"


# ---- a new agent ----------------------------------------------------------------------------------

async def test_create_agent_is_refused_before_the_person_was_asked(ops, store):
    ok, text, turn = await run(ops, "create_agent", Turn(user_turns=1), **support.NEW_AGENT_ARGUMENTS)
    assert not ok and "먼저 물어보세요" in text and "본인 확인" in text and "사람에게 넘기는 조건" in text
    assert "salon-info" not in store.list_agents() and not turn.changes


async def test_create_agent_makes_version_1_as_a_draft(ops, store):
    ok, text, turn = await run(ops, "create_agent", Turn(user_turns=2), **support.NEW_AGENT_ARGUMENTS)
    assert ok, text
    info = store.version_info("salon-info", 1)
    assert (info.state, info.created_by, info.based_on, info.safety_change) == ("draft", "builder", None, True)
    assert store.deployment("salon-info").stable is None
    definition = store.load("salon-info", 1)
    assert definition.tools == ["handoff_to_human"] and len(definition.tests.personas) == 2
    assert turn.new_agents == ["salon-info"] and turn.changes[("salon-info", 1)]["created"]
    ok, text, _ = await run(ops, "create_agent", Turn(user_turns=3), **support.NEW_AGENT_ARGUMENTS)
    assert not ok and "이미 있는 에이전트" in text


@pytest.mark.parametrize("change, expected", [
    ({"tools": ["handoff_to_human", "book_appointment"]}, "등록되지 않은 도구입니다: book_appointment"),
    ({"extract": [{"name": "주문번호", "pii": True}]}, "담당자에게 묻지 말고 직접 영문 이름을"),
    ({"flow": [{"id": "본인 확인", "rule": "확인한다"}]}, "flow의 id '본인 확인'"),
    ({"handoff": []}, "handoff(사람에게 넘기는 조건)가 비어"),
    ({"prohibitions": []}, "prohibitions(금지 사항)가 비어"),
    ({"flow": []}, "flow(지켜야 할 절차)가 비어"),
    ({"personas": support.NEW_AGENT_ARGUMENTS["personas"][:1]}, "2명 이상"),
    ({"success": [{"id": "resolved", "rule": "문의에 답했다"}, {"id": "polite", "rule": "정중했다"}]}, "안전 기준"),
    ({"extract": []}, "extract(뽑을 정보)가 비어"),
    ({"persona": "상담원"}, "persona가 너무 짧습니다"),
    ({"id": "Salon Info"}, "id 'Salon Info'는 쓸 수 없습니다"),
    ({"id": "shop-support"}, "이미 있는 에이전트"),
    ({"reply_length": "tiny"}, "reply_length"),
    ({"personas": [{"id": "a", "description": "d", "goal": "g", "expect": {"nope": True}},
                   {"id": "b", "description": "d", "goal": "g"}]}, "오류"),
])
async def test_create_agent_needs_a_complete_definition_and_known_tools(ops, store, change, expected):
    before = store.list_agents()
    ok, text, _ = await run(ops, "create_agent", Turn(user_turns=2), **{**support.NEW_AGENT_ARGUMENTS, **change})
    assert not ok and expected in text, text
    assert store.list_agents() == before


async def test_handoff_rules_bring_the_handoff_tool(ops, store):
    ok, text, _ = await run(ops, "create_agent", Turn(user_turns=2), **{**support.NEW_AGENT_ARGUMENTS, "tools": ["get_weather"]})
    assert ok and "handoff_to_human을 더했습니다" in text
    assert store.load("salon-info", 1).tools == ["get_weather", "handoff_to_human"]


async def test_no_handoff_must_be_said_explicitly(ops, store):
    arguments = {**support.NEW_AGENT_ARGUMENTS, "handoff": [], "no_handoff": True, "tools": []}
    ok, text, _ = await run(ops, "create_agent", Turn(user_turns=2), **arguments)
    assert ok and store.load("salon-info", 1).handoff == []


async def test_tools_cannot_be_set_when_the_contracts_cannot_be_read(store):
    ops = Operations(store, support.FakeEvaluator(store), lambda: None)
    ok, text, _ = await run(ops, "set_tools", agent_id="shop-support", tools=["get_order"])
    assert not ok and "도구 목록을 읽지 못해" in text


# ---- tests, comparison, approval request ----------------------------------------------------------

async def test_run_check_compare_submit(ops, store):
    evaluator = ops.evaluator
    ok, text, _ = await run(ops, "submit_for_approval", agent_id="shop-support")
    assert not ok and text.startswith("거절됨") and "승인을 요청할 수 없습니다" in text      # v1 is deployed

    turn = Turn()
    await ops.execute(turn, "add_handoff_rule", {"agent_id": "shop-support", "id": "large_refund",
                                                 "when": "환불 금액이 50000원을 넘는다"})
    ok, text = await ops.execute(turn, "submit_for_approval", {"agent_id": "shop-support"})
    assert not ok and "시험 결과가 없어" in text and not turn.submitted                      # the store's reason, relayed
    assert store.version_info("shop-support", 2).state == "draft"

    ok, text = await ops.execute(turn, "run_tests", {"agent_id": "shop-support"})
    assert ok and "시험을 시작했습니다" in text and "결과를 지어내지" in text
    run_id = turn.runs[0]["run_id"]
    assert turn.runs[0] == {"run_id": run_id, "agent_id": "shop-support", "version": 2, "total": 11}
    ok, text = await ops.execute(turn, "check_tests", {})
    assert ok and "아직 도는 중" in text and "0/11" in text
    ok, text = await ops.execute(turn, "compare_runs", {})
    assert not ok and "아직 도는 중" in text

    evaluator.finish(run_id, {"refund_small": True, "refund_large": False, "delivery_only": True})
    ok, text = await ops.execute(turn, "check_tests", {"run_id": run_id})
    assert ok and "통과 2/3" in text and "안전 기준 실패 1건" in text and "실패 refund_large" in text
    assert "확인 전에 상품명을 말했다" in text and "시험 완료" in text
    ok, text = await ops.execute(turn, "compare_runs", {})           # no base run yet: the base suite is started
    assert ok and "기준 버전 v1의 시험을 지금 시작했습니다" in text and f"compare_runs(new_run_id={run_id})" in text
    base_id = turn.runs[1]["run_id"]
    assert turn.runs[1]["version"] == 1 and turn.last_run_id == run_id
    ok, text = await ops.execute(turn, "compare_runs", {})
    assert not ok and "아직 도는 중" in text and len(turn.runs) == 2   # not started twice
    evaluator.finish(base_id, {"refund_small": False, "refund_large": True, "delivery_only": True})
    assert store.version_info("shop-support", 1).state == "deployed"
    ok, text = await ops.execute(turn, "compare_runs", {"new_run_id": run_id})
    assert ok and f"기준 {base_id}" in text and "좋아진 항목: refund_small" in text and "나빠진 항목: refund_large" in text
    ok, text = await ops.execute(turn, "compare_runs", {"new_run_id": run_id, "base_run_id": "nope"})
    assert not ok and "찾을 수 없습니다" in text
    ok, text = await ops.execute(turn, "check_tests", {"run_id": "nope"})
    assert not ok and "찾을 수 없습니다" in text

    ok, text = await ops.execute(turn, "submit_for_approval", {"agent_id": "shop-support", "version": 2})
    assert ok and "승인 대기" in text and "사람이" in text and turn.submitted == [("shop-support", 2)]
    assert support.states(store, "shop-support") == {1: "deployed", 2: "pending_approval"}
    ok, text = await ops.execute(turn, "submit_for_approval", {"agent_id": "shop-support", "version": 2})
    assert not ok and text.startswith("거절됨")
    assert store.deployment("shop-support").stable == 1 and store.version_info("shop-support", 2).approved_by is None


async def test_run_tests_needs_something_to_run_and_an_evaluator(store):
    ops = Operations(store, None, lambda: dict(support.CATALOG))
    ok, text, _ = await run(ops, "run_tests", agent_id="shop-support")
    assert not ok and "평가 모듈" in text
    ops = Operations(store, support.FakeEvaluator(store), lambda: dict(support.CATALOG))
    definition = store.load("kiosk-order", 1)
    definition.tests.personas, definition.tests.cases = [], []
    store.save_draft(definition)
    ok, text, _ = await run(ops, "run_tests", agent_id="kiosk-order")
    assert not ok and "시험할 것이 없습니다" in text


async def test_every_operation_together_never_approves_or_deploys(ops, store):
    """Runs all operations with plausible arguments and checks the release state afterwards."""
    turn = Turn(user_turns=3)
    deployments = {agent: store.deployment(agent).model_dump() for agent in store.list_agents()}
    await ops.execute(turn, "create_agent", dict(support.NEW_AGENT_ARGUMENTS))
    for name, arguments in (
            ("create_draft", {}), ("set_field", {"field": "reply_length", "value": "normal"}),
            ("add_flow_rule", {"id": "greet", "rule": "인사한다"}), ("remove_flow_rule", {"id": "greet"}),
            ("add_prohibition", {"text": "약속하지 않는다"}), ("add_handoff_rule", {"id": "angry", "when": "화를 낸다"}),
            ("add_guard", {"id": "g", "pattern": "바보", "nudge": "n"}), ("set_tools", {"tools": ["get_order", "handoff_to_human"]}),
            ("add_success_criterion", {"id": "c", "rule": "r", "safety": True}), ("add_extract_field", {"name": "e"}),
            ("add_persona", {"id": "p", "description": "d", "goal": "g"}), ("run_tests", {}), ("check_tests", {}),
            ("read_definition", {}), ("list_versions", {})):
        ok, text = await ops.execute(turn, name, {"agent_id": "shop-support", **arguments})
        assert ok, (name, text)
    ops.evaluator.finish(turn.runs[0]["run_id"], {"refund_small": True})
    for name in ("compare_runs", "submit_for_approval", "submit_for_approval", "approve", "deploy", "rollback"):
        await ops.execute(turn, name, {"agent_id": "shop-support"})
    for agent in store.list_agents():
        for info in store.list_versions(agent):
            if info.created_by == "builder":
                assert info.state in ("draft", "tested", "pending_approval"), info
                assert info.approved_by is None and info.approved_at is None
        if agent in deployments:
            assert store.deployment(agent).model_dump() == deployments[agent]
    assert store.deployment("salon-info").stable is None
    assert support.states(store, "shop-support") == {1: "deployed", 2: "pending_approval"}
    # and a fresh store on the same folder agrees (nothing was held in memory only)
    assert support.states(DefinitionStore(store.root), "shop-support") == {1: "deployed", 2: "pending_approval"}


# ---- failed conversations (R-BLD-6) ---------------------------------------------------------------

async def test_failed_sessions_are_grouped_by_criterion(store):
    trace = support.FakeTraceStore([
        {"session_id": "s1", "agent_id": "shop-support", "version": 1, "label": "skip_verification",
         "failed": {"verified_before_disclosure": "확인 전에 상품명을 말했다", "no_handoff": "넘겼다"}},
        {"session_id": "s2", "agent_id": "shop-support", "version": 2, "failed": {"no_handoff": "넘겼다"}},
        {"session_id": "s3", "agent_id": "shop-support", "version": 1, "failed": {}},
        {"session_id": "s4", "agent_id": "shop-support", "version": 1, "failed": None},       # not analysed
        {"session_id": "s5", "agent_id": "tech-trend-anchor", "version": 1, "failed": {"x": "y"}},
    ])
    ops = Operations(store, None, lambda: dict(support.CATALOG), lambda: trace)
    ok, text, _ = await run(ops, "list_failed_sessions", agent_id="shop-support")
    assert ok and "최근 대화 4개 중 분석된 3개" in text
    lines = text.splitlines()
    assert lines[1].startswith("- verified_before_disclosure [안전]: 1건 (버전 v1)")    # safety criteria first
    assert "s1 skip_verification - 확인 전에 상품명을 말했다" in lines[2]
    assert any(line.startswith("- no_handoff: 2건 (버전 v1, v2)") for line in lines)
    assert "s5" not in text and "s3" not in text
    ok, text, _ = await run(ops, "list_failed_sessions", agent_id="tech-trend-anchor", limit=0)
    assert ok and "x: 1건" in text
    quiet = Operations(store, None, lambda: dict(support.CATALOG), lambda: support.FakeTraceStore([]))
    ok, text, _ = await run(quiet, "list_failed_sessions", agent_id="shop-support")
    assert ok and "실패한 대화는 없습니다" in text
    ok, text, _ = await run(Operations(store, None), "list_failed_sessions", agent_id="shop-support")
    assert not ok and "저장소를 쓸 수 없습니다" in text
