"""The gateway's rules, one test per rule. Offline: the dummy shop and scripted adapters only."""
import asyncio
import time

import pytest

from agentkit.contracts import DisplaySpec, ToolGateway
from agentkit.dummy.shop import get_shop
from agentkit.tools import AdapterUnavailable, ContractError, Gateway, build_gateway
from agentkit.tools.testing import FakeRecorder, ScriptedAdapter, make_contract, make_ctx

REFUND = {"order_id": "A1001", "reason": "단순 변심"}


@pytest.fixture
def shop():
    shop = get_shop()
    shop.reset()
    shop.inject()
    yield shop
    shop.reset()
    shop.inject()


@pytest.fixture
def recorder():
    return FakeRecorder()


@pytest.fixture
def gateway(shop, recorder):
    return build_gateway(recorder=recorder)


async def verify(gateway, session="s1", order="A1001", last4="1234", turn=1):
    result = await gateway.call(make_ctx(session, turn), "verify_customer", {"order_id": order, "phone_last4": last4})
    assert result.ok, result.text
    return result


def scripted(effect="read", script=("ok",), preview=None, recorder=None, **fields):
    """A gateway with one tool "t" whose adapter follows a script."""
    contract = make_contract("t", effect=effect, **fields)
    adapter = ScriptedAdapter(contract, script, preview)
    return Gateway({"t": contract}, recorder=recorder, adapters={"t": adapter}), adapter


# ---- interface ---------------------------------------------------------------------------------


def test_build_gateway_reads_all_contracts(gateway):
    assert len(gateway.names()) == 49 and {"shop.get_order", "cafe.search_menu", "burger.search_menu",
                                           "common.handoff_to_human", "airline.change_flight"} <= set(gateway.names())
    assert gateway.resolve("search_menu", ["burger", "common"]) == "burger.search_menu"
    for method in ("names", "contract", "specs", "call", "facts", "end_session"):
        assert callable(getattr(gateway, method)), method
    typed: ToolGateway = gateway            # the protocol is what the engine codes against
    assert typed.contract("request_refund").effect == "write"
    with pytest.raises(KeyError):
        gateway.contract("no_such_tool")


def test_specs_are_function_specs(gateway):
    specs = gateway.specs(["verify_customer", "request_refund"])
    assert [s["function"]["name"] for s in specs] == ["verify_customer", "request_refund"]
    assert all(s["type"] == "function" for s in specs)
    assert specs[0]["function"]["parameters"]["required"] == ["order_id", "phone_last4"]
    assert specs[1]["function"]["description"]


# ---- 1. input validation -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_input_is_refused_before_the_adapter(gateway, shop):
    result = await gateway.call(make_ctx(), "verify_customer", {"order_id": "A1001"})
    assert (result.ok, result.error_type) == (False, "invalid_input")
    assert "phone_last4" in result.text
    wrong_type = await gateway.call(make_ctx(), "verify_customer", {"order_id": ["A1001"], "phone_last4": "1234"})
    assert wrong_type.error_type == "invalid_input"
    assert gateway.facts("s1") == set()


@pytest.mark.asyncio
async def test_unknown_tool_is_invalid_input(gateway):
    result = await gateway.call(make_ctx(), "delete_everything", {})
    assert (result.ok, result.error_type) == (False, "invalid_input")
    assert "delete_everything" in result.text


@pytest.mark.asyncio
async def test_scalar_arguments_are_converted_and_json_strings_accepted(gateway):
    result = await gateway.call(make_ctx(), "verify_customer", '{"order_id": "a1001", "phone_last4": 1234}')
    assert result.ok, result.text


# ---- 2. requires / grants ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_requires_missing_is_precondition_failed_and_names_what_comes_first(gateway):
    for name, arguments in (("get_order", {"order_id": "A1001"}), ("track_delivery", {"order_id": "A1001"}),
                            ("request_refund", REFUND)):
        result = await gateway.call(make_ctx(), name, arguments)
        assert (result.ok, result.error_type) == (False, "precondition_failed"), name
        assert "verify_customer" in result.text and "본인 확인" in result.text
        assert result.data is None or "product_name" not in result.data


@pytest.mark.asyncio
async def test_grants_set_the_fact_only_on_success_and_per_session(gateway):
    failed = await gateway.call(make_ctx("s1"), "verify_customer", {"order_id": "A1001", "phone_last4": "0000"})
    assert failed.error_type == "permission_denied"
    assert gateway.facts("s1") == set()
    await verify(gateway, "s1")
    assert gateway.facts("s1") == {"verified"}
    assert gateway.facts("s2") == set()
    other = await gateway.call(make_ctx("s2"), "get_order", {"order_id": "A1001"})
    assert other.error_type == "precondition_failed"
    assert (await gateway.call(make_ctx("s1", 2), "get_order", {"order_id": "A1001"})).ok


@pytest.mark.asyncio
async def test_end_session_forgets_facts_proposals_and_shop_state(gateway, shop):
    await verify(gateway)
    await gateway.call(make_ctx(turn=2), "request_refund", REFUND)
    assert gateway.pending("s1")
    gateway.end_session("s1")
    assert gateway.facts("s1") == set() and gateway.pending("s1") == []
    assert shop.verified_order("s1") is None
    assert (await gateway.call(make_ctx(turn=3), "get_order", {"order_id": "A1001"})).error_type == "precondition_failed"


# ---- 3. two-phase write execution --------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_call_of_a_write_tool_only_proposes(gateway, shop):
    await verify(gateway)
    result = await gateway.call(make_ctx(turn=2, text="환불해 주세요"), "request_refund", REFUND)
    assert result.ok and result.needs_confirmation and not result.executed
    assert result.confirmation_text == "무선 이어폰 32000원 환불을 접수할까요?"   # product and amount from the preview
    assert result.confirmation_text in result.text
    assert shop.refunds("s1") == []
    assert shop.order("s1", "A1001")["refund"] is None


@pytest.mark.asyncio
async def test_same_turn_repeat_does_not_execute(gateway, shop):
    await verify(gateway)
    # even though the utterance of this turn contains "네" and "해 주세요"
    ctx = make_ctx(turn=2, text="네, 환불해 주세요")
    await gateway.call(ctx, "request_refund", REFUND)
    for _ in range(3):
        again = await gateway.call(ctx, "request_refund", REFUND)
        assert (again.ok, again.error_type, again.executed) == (False, "not_confirmed", False)
        assert again.confirmation_text == "무선 이어폰 32000원 환불을 접수할까요?"
    assert shop.refunds("s1") == []


@pytest.mark.asyncio
async def test_later_turn_with_consent_executes(gateway, shop):
    await verify(gateway)
    await gateway.call(make_ctx(turn=2, text="환불하고 싶어요"), "request_refund", REFUND)
    done = await gateway.call(make_ctx(turn=3, text="네, 접수해 주세요"), "request_refund", REFUND)
    assert done.ok and done.executed and not done.needs_confirmation
    assert "접수되었습니다" in done.text and done.data["refund_id"] == "R1001"
    refunds = shop.refunds("s1")
    assert len(refunds) == 1 and refunds[0]["amount"] == 32000 and refunds[0]["order_id"] == "A1001"


@pytest.mark.asyncio
@pytest.mark.parametrize("utterance", ["아니요, 취소할게요", "잠깐만요", "환불 말고 교환해 주세요", "하지 마세요"])
async def test_later_turn_with_refusal_does_not_execute_and_drops_the_proposal(gateway, shop, utterance):
    await verify(gateway)
    await gateway.call(make_ctx(turn=2), "request_refund", REFUND)
    refused = await gateway.call(make_ctx(turn=3, text=utterance), "request_refund", REFUND)
    assert (refused.ok, refused.error_type, refused.executed) == (False, "not_confirmed", False)
    assert shop.refunds("s1") == []
    assert gateway.pending("s1") == []
    # a "yes" later does not revive the dropped proposal: it starts again with a confirmation
    fresh = await gateway.call(make_ctx(turn=4, text="네"), "request_refund", REFUND)
    assert fresh.needs_confirmation and not fresh.executed
    assert shop.refunds("s1") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("utterance", ["", "얼마라고요?", "네?", "그게 언제 배송된 거였죠", "금액을 다시 말해 주세요"])
async def test_later_turn_without_clear_consent_does_not_execute(gateway, shop, utterance):
    await verify(gateway)
    await gateway.call(make_ctx(turn=2), "request_refund", REFUND)
    unclear = await gateway.call(make_ctx(turn=3, text=utterance), "request_refund", REFUND)
    assert (unclear.ok, unclear.error_type, unclear.executed) == (False, "not_confirmed", False)
    assert unclear.confirmation_text
    assert shop.refunds("s1") == []
    # the question was asked again in turn 3, so consent in turn 4 counts
    done = await gateway.call(make_ctx(turn=4, text="네 맞아요"), "request_refund", REFUND)
    assert done.executed and len(shop.refunds("s1")) == 1


@pytest.mark.asyncio
async def test_stale_proposal_is_asked_again_instead_of_executed(gateway, shop):
    """Consent has to follow the confirmation directly ("다음 말에서"): a "네" two turns later
    may be about something else."""
    await verify(gateway)
    await gateway.call(make_ctx(turn=2), "request_refund", REFUND)
    late = await gateway.call(make_ctx(turn=5, text="네"), "request_refund", REFUND)
    assert late.needs_confirmation and not late.executed
    assert shop.refunds("s1") == []
    done = await gateway.call(make_ctx(turn=6, text="네"), "request_refund", REFUND)
    assert done.executed


@pytest.mark.asyncio
async def test_consent_window_can_be_widened(shop):
    gateway = build_gateway(consent_window=3)
    await verify(gateway)
    await gateway.call(make_ctx(turn=2), "request_refund", REFUND)
    assert (await gateway.call(make_ctx(turn=5, text="네"), "request_refund", REFUND)).executed


@pytest.mark.asyncio
async def test_changed_confirmed_argument_needs_a_new_confirmation():
    gateway, adapter = scripted(
        "write", confirm_template="{amount}원을 보낼까요?", idempotency_fields=["account"],
        input_schema={"type": "object", "properties": {"account": {"type": "string"}, "amount": {"type": "integer"}},
                      "required": ["account", "amount"]})
    first = await gateway.call(make_ctx(turn=1), "t", {"account": "x", "amount": 1000})
    assert first.confirmation_text == "1000원을 보낼까요?"
    changed = await gateway.call(make_ctx(turn=2, text="네"), "t", {"account": "x", "amount": 900000})
    assert changed.needs_confirmation and changed.confirmation_text == "900000원을 보낼까요?"
    assert adapter.calls == []
    done = await gateway.call(make_ctx(turn=3, text="네"), "t", {"account": "x", "amount": 900000})
    assert done.executed and adapter.calls == [{"account": "x", "amount": 900000}]


@pytest.mark.asyncio
async def test_preview_refusal_is_reported_at_once_without_a_proposal(gateway, shop):
    """A1002 costs 289000 won: the shop refuses, and the customer is never asked to confirm."""
    await verify(gateway, order="A1002", last4="5678")
    result = await gateway.call(make_ctx(turn=2, text="환불해 주세요"), "request_refund",
                                {"order_id": "A1002", "reason": "변심"})
    assert (result.ok, result.error_type) == (False, "precondition_failed")
    assert not result.needs_confirmation and "handoff_to_human" in result.text
    assert gateway.pending("s1") == [] and shop.refunds("s1") == []


@pytest.mark.asyncio
async def test_missing_template_value_refuses_instead_of_reading_a_broken_sentence():
    gateway, adapter = scripted("write", confirm_template="{product_name} 환불을 접수할까요?", preview={"ok": True, "data": {}})
    result = await gateway.call(make_ctx(), "t", {"key": "a"})
    assert (result.ok, result.error_type, result.needs_confirmation) == (False, "system_error", False)
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_write_tool_without_confirm_template_executes_at_once(gateway, shop):
    result = await gateway.call(make_ctx(turn=1, text="상담원 바꿔 주세요"), "handoff_to_human", {"reason": "고객 요청"})
    assert result.ok and result.executed and not result.needs_confirmation
    assert len(shop.records("s1", "handoff")) == 1


# ---- 4. speculative ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_speculative_never_executes_a_write_tool(gateway, shop):
    await verify(gateway)
    # no proposal yet: nothing is proposed either, so a discarded speculative turn leaves no trace
    spec = await gateway.call(make_ctx(turn=2, text="네 환불해 주세요", speculative=True), "request_refund", REFUND)
    assert (spec.ok, spec.error_type, spec.executed, spec.needs_confirmation) == (False, "not_confirmed", False, False)
    assert gateway.pending("s1") == []
    # with a proposal waiting and a consenting utterance, still nothing runs while speculative
    await gateway.call(make_ctx(turn=2), "request_refund", REFUND)
    spec = await gateway.call(make_ctx(turn=3, text="네", speculative=True), "request_refund", REFUND)
    assert (spec.error_type, spec.executed) == ("not_confirmed", False)
    assert shop.refunds("s1") == []
    # the proposal survives: once the turn is final, the same call executes
    done = await gateway.call(make_ctx(turn=3, text="네"), "request_refund", REFUND)
    assert done.executed and len(shop.refunds("s1")) == 1


@pytest.mark.asyncio
async def test_speculative_does_not_execute_a_write_tool_without_confirmation(gateway, shop):
    result = await gateway.call(make_ctx(speculative=True), "handoff_to_human", {"reason": "고객 요청"})
    assert (result.ok, result.error_type, result.executed) == (False, "not_confirmed", False)
    assert shop.records("s1", "handoff") == []


@pytest.mark.asyncio
async def test_speculative_read_tools_run(gateway):
    result = await gateway.call(make_ctx(speculative=True), "verify_customer", {"order_id": "A1001", "phone_last4": "1234"})
    assert result.ok


# ---- 5. no second execution --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repeated_executed_request_returns_first_result_with_executed_false(gateway, shop):
    await verify(gateway)
    await gateway.call(make_ctx(turn=2), "request_refund", REFUND)
    first = await gateway.call(make_ctx(turn=3, text="네"), "request_refund", REFUND)
    assert first.executed
    for turn, text, arguments in ((3, "네", REFUND), (4, "네 한 번 더요", REFUND),
                                  (5, "네", {"order_id": " a1001 ", "reason": "다른 사유"})):
        again = await gateway.call(make_ctx(turn=turn, text=text), "request_refund", arguments)
        assert again.ok and not again.executed and not again.needs_confirmation
        assert first.text in again.text and again.data == first.data
    assert len(shop.refunds("s1")) == 1


@pytest.mark.asyncio
async def test_concurrent_identical_writes_execute_once():
    gateway, adapter = scripted("write", script=(0.05,), idempotency_fields=["key"])
    results = await asyncio.gather(*(gateway.call(make_ctx(), "t", {"key": "a"}) for _ in range(5)))
    assert sum(r.executed for r in results) == 1
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_handoff_runs_once_per_conversation_even_with_another_reason(gateway, shop):
    first = await gateway.call(make_ctx(), "handoff_to_human", {"reason": "고액 환불"})
    second = await gateway.call(make_ctx(turn=2), "handoff_to_human", {"reason": "고객 요청"})
    assert first.executed and second.ok and not second.executed
    assert len(shop.records("s1", "handoff")) == 1


# ---- 6. error types ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_types_from_the_system(gateway, shop):
    not_found = await gateway.call(make_ctx(), "verify_customer", {"order_id": "A9999", "phone_last4": "1234"})
    assert not_found.error_type == "not_found" and "A9999" in not_found.text
    denied = await gateway.call(make_ctx(), "verify_customer", {"order_id": "A1001", "phone_last4": "9999"})
    assert denied.error_type == "permission_denied"
    await verify(gateway)
    other_order = await gateway.call(make_ctx(turn=2), "get_order", {"order_id": "A1002"})
    assert other_order.error_type == "permission_denied" and other_order.data.get("product_name") is None
    shop.inject(fail="system_error", fail_methods=["get_order"])
    broken = await gateway.call(make_ctx(turn=2), "get_order", {"order_id": "A1001"})
    assert broken.error_type == "system_error"
    for result in (not_found, denied, other_order, broken):
        assert not result.ok and len(result.text) > 10


@pytest.mark.asyncio
async def test_every_error_type_reaches_the_model_with_a_korean_sentence():
    import re
    for error_type in ("not_found", "permission_denied", "invalid_input", "precondition_failed",
                       "not_confirmed", "system_error", "timeout"):
        gateway, _ = scripted(script=({"ok": False, "error_type": error_type},))
        result = await gateway.call(make_ctx(), "t", {})
        assert (result.ok, result.error_type) == (False, error_type)
        assert re.search("[가-힣]", result.text), error_type
    gateway, _ = scripted(script=({"ok": False, "error_type": "weird"},))
    assert (await gateway.call(make_ctx(), "t", {})).error_type == "system_error"


@pytest.mark.asyncio
async def test_adapter_bug_becomes_system_error_not_an_exception():
    gateway, _ = scripted(script=(ValueError("boom"),))
    result = await gateway.call(make_ctx(), "t", {})
    assert (result.ok, result.error_type) == (False, "system_error")
    assert "boom" not in result.text


# ---- 7. timeouts and retries -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout(recorder):
    gateway, adapter = scripted(script=(5,), timeout_s=0.1, retries=2, recorder=recorder)
    started = time.perf_counter()
    result = await gateway.call(make_ctx(), "t", {})
    assert (result.ok, result.error_type) == (False, "timeout")
    assert time.perf_counter() - started < 1.0
    assert len(adapter.calls) == 1           # a timeout uses up the budget: not tried again
    assert 90 <= result.ms < 1000


@pytest.mark.asyncio
async def test_shop_delay_injection_runs_into_the_timeout(shop):
    gateway = build_gateway(adapter_overrides={"verify_customer": {
        "kind": "dummy", "target": "shop:verify_customer", "options": {"delay_s": 0.5}}})
    gateway._contracts["shop.verify_customer"].timeout_s = 0.1
    result = await gateway.call(make_ctx(), "verify_customer", {"order_id": "A1001", "phone_last4": "1234"})
    assert result.error_type == "timeout" and gateway.facts("s1") == set()


@pytest.mark.asyncio
async def test_read_tool_is_retried_when_the_system_is_not_reached(recorder):
    gateway, adapter = scripted(script=(AdapterUnavailable("down"), AdapterUnavailable("down"), "이제 됩니다"),
                                retries=2, recorder=recorder)
    result = await gateway.call(make_ctx(), "t", {})
    assert result.ok and result.text == "이제 됩니다" and len(adapter.calls) == 3
    assert recorder.spans[-1][0].attrs["attempts"] == 3


@pytest.mark.asyncio
async def test_read_tool_gives_up_after_retries_with_system_error():
    gateway, adapter = scripted(script=(AdapterUnavailable("down"),), retries=1)
    result = await gateway.call(make_ctx(), "t", {})
    assert (result.ok, result.error_type) == (False, "system_error") and len(adapter.calls) == 2


@pytest.mark.asyncio
async def test_a_refusal_by_the_system_is_not_retried():
    gateway, adapter = scripted(script=({"ok": False, "error_type": "not_found", "text": "없습니다."}, "ok"), retries=3)
    assert (await gateway.call(make_ctx(), "t", {})).error_type == "not_found"
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_shop_outage_injection_is_retried_for_read_tools(gateway, shop):
    shop.inject(fail="unavailable", fail_times=1)
    assert (await verify(gateway)).ok        # first try fails, the retry (retries: 1) goes through
    shop.inject(fail="unavailable")
    down = await gateway.call(make_ctx(turn=2), "get_order", {"order_id": "A1001"})
    assert down.error_type == "system_error"


@pytest.mark.asyncio
async def test_write_tool_is_never_retried_even_if_the_contract_asks():
    gateway, adapter = scripted("write", script=(AdapterUnavailable("down"), "ok"), retries=3)
    result = await gateway.call(make_ctx(), "t", {"key": "a"})
    assert (result.ok, result.error_type, result.executed) == (False, "system_error", False)
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_write_with_unknown_outcome_is_not_offered_again():
    """A write that timed out may have gone through: the same request is not sent a second time."""
    gateway, adapter = scripted("write", script=(5, "ok"), timeout_s=0.1, idempotency_fields=["key"])
    first = await gateway.call(make_ctx(turn=1), "t", {"key": "a"})
    assert (first.ok, first.error_type, first.executed) == (False, "timeout", False)
    again = await gateway.call(make_ctx(turn=2, text="네"), "t", {"key": "a"})
    assert (again.ok, again.executed) == (False, False) and "상담원" in again.text
    assert len(adapter.calls) == 1
    other = await gateway.call(make_ctx(turn=2), "t", {"key": "b"})     # another request is unaffected
    assert other.executed


@pytest.mark.asyncio
async def test_write_broken_off_midway_is_not_offered_again():
    """The connection broke after the request was sent, or the call was cancelled while running."""
    gateway, adapter = scripted("write", script=(AdapterUnavailable("reset", uncertain=True), "ok"), idempotency_fields=["key"])
    first = await gateway.call(make_ctx(turn=1), "t", {"key": "a"})
    assert (first.ok, first.executed, first.error_type) == (False, False, "system_error")
    assert not (await gateway.call(make_ctx(turn=2), "t", {"key": "a"})).ok and len(adapter.calls) == 1

    gateway, adapter = scripted("write", script=(5, "ok"), timeout_s=10, idempotency_fields=["key"])
    task = asyncio.ensure_future(gateway.call(make_ctx(turn=1), "t", {"key": "a"}))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    again = await gateway.call(make_ctx(turn=2), "t", {"key": "a"})
    assert (again.ok, again.executed) == (False, False) and len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_system_refusal_at_execution_changes_nothing_and_grants_nothing():
    gateway, adapter = scripted("write", script=({"ok": False, "error_type": "precondition_failed", "text": "안 됩니다."}, "ok"),
                                grants=["done"], idempotency_fields=["key"])
    refused = await gateway.call(make_ctx(), "t", {"key": "a"})
    assert (refused.ok, refused.executed, refused.error_type) == (False, False, "precondition_failed")
    assert gateway.facts("s1") == set()
    # a refusal is a known outcome, so the request may be made again
    assert (await gateway.call(make_ctx(turn=2), "t", {"key": "a"})).executed and gateway.facts("s1") == {"done"}


# ---- 8. recording ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_call_is_recorded_as_a_tool_span(gateway, recorder):
    await gateway.call(make_ctx(turn=1), "get_order", {})                               # invalid_input
    await gateway.call(make_ctx(turn=1), "get_order", {"order_id": "A1001"})            # precondition_failed
    await verify(gateway, turn=2)
    await gateway.call(make_ctx(turn=3), "request_refund", REFUND)                      # proposed
    await gateway.call(make_ctx(turn=4, text="네"), "request_refund", REFUND)            # executed
    await gateway.call(make_ctx(turn=5, text="네"), "request_refund", REFUND)            # duplicate
    await gateway.call(make_ctx(turn=5), "nope", {})                                    # unknown tool
    spans = [span for span, _ in recorder.spans]
    assert len(spans) == 7
    assert all(s.kind == "tool" and s.session_id == "s1" and s.t_end >= s.t_start for s in spans)
    assert [s.turn for s in spans] == [1, 1, 2, 3, 4, 5, 5]
    assert [s.name for s in spans] == ["get_order", "get_order", "verify_customer", "request_refund",
                                       "request_refund", "request_refund", "nope"]
    assert [s.ok for s in spans] == [False, False, True, True, True, True, False]
    assert [s.attrs["error_type"] for s in spans] == ["invalid_input", "precondition_failed", None, None, None, None,
                                                      "invalid_input"]
    assert [s.attrs["phase"] for s in spans[3:6]] == ["proposed", "executed", "duplicate"]
    assert [s.attrs["executed"] for s in spans[3:6]] == [False, True, False]
    assert spans[4].attrs["consent"] == "yes" and spans[4].attrs["proposed_turn"] == 3
    assert spans[3].attrs["effect"] == "write" and spans[3].attrs["needs_confirmation"] is True
    assert spans[3].input == {"arguments": REFUND}
    assert spans[4].output["executed"] is True and spans[4].output["data"]["refund_id"] == "R1001"
    assert spans[4].output["call_id"] and spans[4].output["call_id"] == spans[4].attrs["call_id"]


@pytest.mark.asyncio
async def test_span_carries_pii_paths_and_no_marked_value_in_its_sentences(gateway, recorder):
    await verify(gateway)
    await gateway.call(make_ctx(turn=2), "get_order", {"order_id": "A1001"})
    (verify_span, verify_paths), (order_span, order_paths) = recorder.spans
    assert verify_paths == ["input.arguments.phone_last4", "output.data.customer_name"]
    assert order_paths == ["output.data.customer_name", "output.data.address"]
    # the result handed to the model names the customer; the recorded sentence does not
    assert "김민수" not in verify_span.output["text"] and "[가려짐]" in verify_span.output["text"]
    assert "테헤란로" not in order_span.output["text"] and "김민수" not in order_span.output["text"]
    # the structured fields are left for the recorder to replace by path
    assert order_span.output["data"]["address"].startswith("서울특별시")


@pytest.mark.asyncio
async def test_result_text_for_the_model_is_not_masked(gateway):
    result = await verify(gateway)
    assert "김민수" in result.text
    order = await gateway.call(make_ctx(turn=2), "get_order", {"order_id": "A1001"})
    assert "테헤란로" in order.text


@pytest.mark.asyncio
async def test_broken_or_missing_recorder_does_not_disturb_the_call(shop):
    for recorder in (None, FakeRecorder(fail=True)):
        gateway = build_gateway(recorder=recorder)
        assert (await verify(gateway, session=f"r{id(recorder)}")).ok


@pytest.mark.asyncio
async def test_cancelled_call_is_recorded_and_stays_cancelled(recorder):
    gateway, _ = scripted(script=(5,), timeout_s=10, recorder=recorder)
    task = asyncio.ensure_future(gateway.call(make_ctx(), "t", {}))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert recorder.spans[-1][0].attrs["phase"] == "cancelled"


@pytest.mark.asyncio
async def test_result_carries_ms_call_id_and_item_count():
    listing = "최근 글 2건:\n1. 첫 글\n2. 둘째 글"
    gateway, _ = scripted(script=(listing,))
    result = await gateway.call(make_ctx(), "t", {})
    assert result.items == 2 and result.call_id and result.ms >= 0


# ---- the whole scenario (S11) ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s11_small_refund_and_large_refund_handoff(gateway, shop, recorder):
    # small refund: verify -> track -> propose -> consent -> executed
    assert (await gateway.call(make_ctx("small", 1, "A1001이고 뒤 네 자리는 1234예요"), "verify_customer",
                               {"order_id": "A1001", "phone_last4": "1234"})).ok
    track = await gateway.call(make_ctx("small", 2, "배송 어디까지 왔어요"), "track_delivery", {"order_id": "A1001"})
    assert track.ok and "배송이 완료" in track.text
    ask = await gateway.call(make_ctx("small", 3, "환불하고 싶어요"), "request_refund", REFUND)
    assert ask.needs_confirmation
    done = await gateway.call(make_ctx("small", 4, "네 맞아요"), "request_refund", REFUND)
    assert done.executed
    # large refund: refused by the shop's rule, then handed off
    assert (await gateway.call(make_ctx("large", 1), "verify_customer", {"order_id": "A1002", "phone_last4": "5678"})).ok
    refused = await gateway.call(make_ctx("large", 2, "환불해 주세요"), "request_refund", {"order_id": "A1002", "reason": "변심"})
    assert refused.error_type == "precondition_failed" and refused.data["handoff_recommended"]
    handoff = await gateway.call(make_ctx("large", 2, "환불해 주세요"), "handoff_to_human", {"reason": "환불 금액이 10만 원을 넘음"})
    assert handoff.executed
    assert [r["order_id"] for r in shop.refunds()] == ["A1001"]
    assert len(shop.records("large", "handoff")) == 1
    executed = [s for s, _ in recorder.spans if s.attrs["executed"]]
    assert [s.name for s in executed] == ["request_refund", "handoff_to_human"]


# ---- screens (docs/DISPLAY.md) -----------------------------------------------------------------

CART = {"lines": [{"line_id": "L1", "name": "아메리카노", "quantity": 1}], "total": 4500}


def screens(tools, recorder=None):
    """A gateway of scripted tools: {name: (effect, display or None, script, preview, more fields)}."""
    contracts, adapters = {}, {}
    for name, (effect, display, script, preview, fields) in tools.items():
        contracts[name] = make_contract(name, effect=effect, display=display,
                                        input_schema={"type": "object", "properties": {"key": {"type": "string"}}},
                                        **fields)
        adapters[name] = ScriptedAdapter(contracts[name], script, preview)
    return Gateway(contracts, recorder=recorder, adapters=adapters), adapters


def ok(data, text="ok"):
    return {"ok": True, "text": text, "data": data}


@pytest.mark.asyncio
async def test_display_of_a_success_is_the_data_or_the_value_at_the_path():
    gateway, _ = screens({
        "whole": ("read", DisplaySpec(kind="cart"), [ok(CART)], None, {}),
        "part": ("read", DisplaySpec(kind="items", path="found.list"), [ok({"found": {"list": {"items": [1]}}, "n": 1})], None, {}),
        "absent": ("read", DisplaySpec(kind="items", path="found.nope"), [ok({"found": {}})], None, {}),
        "no_data": ("read", DisplaySpec(kind="menu"), ["just a sentence"], None, {}),
        "plain": ("read", None, [ok(CART)], None, {}),
    })
    whole = await gateway.call(make_ctx(), "whole", {})
    assert whole.display == [{"kind": "cart", "data": CART, "source": "whole"}]
    whole.display[0]["data"]["lines"].clear()                   # the screen's copy is its own
    assert whole.data["lines"]
    assert (await gateway.call(make_ctx(), "part", {})).display == [
        {"kind": "items", "data": {"items": [1]}, "source": "part"}]
    for name in ("absent", "no_data", "plain"):                 # nothing to show is not an error
        result = await gateway.call(make_ctx(), name, {})
        assert result.ok and result.display == [], name


@pytest.mark.asyncio
async def test_refresh_calls_the_read_tools_without_arguments_in_the_same_turn(recorder):
    gateway, adapters = screens({
        "change": ("write", DisplaySpec(kind="notice", path="notice", refresh=["cart", "menu"]),
                   [ok({"notice": {"level": "info", "text": "담았습니다"}})], None, {}),
        "cart": ("read", DisplaySpec(kind="cart", refresh=["menu"]), [ok(CART)], None, {}),
        "menu": ("read", DisplaySpec(kind="menu"), [ok({"categories": []})], None, {}),
    }, recorder)
    result = await gateway.call(make_ctx("s9", 4, "하나 주세요"), "change", {"key": "x"})
    assert result.executed
    assert [(d["kind"], d["source"]) for d in result.display] == [("notice", "change"), ("cart", "cart"), ("menu", "menu")]
    assert result.display[1]["data"] == CART
    assert adapters["cart"].calls == [{}] and adapters["menu"].calls == [{}]    # once each: a refresh does not chain
    spans = {s.name: s for s, _ in recorder.spans}
    assert [s.name for s, _ in recorder.spans] == ["cart", "menu", "change"]
    for name in ("cart", "menu"):
        assert (spans[name].session_id, spans[name].turn, spans[name].kind) == ("s9", 4, "tool")
        assert spans[name].attrs["refresh_of"] == "change" and spans[name].attrs["parent_call_id"] == result.call_id
        assert spans[name].input == {"arguments": {}}
    assert "refresh_of" not in spans["change"].attrs
    assert spans["change"].output["display"] == [{"kind": "notice", "source": "change"}, {"kind": "cart", "source": "cart"},
                                                 {"kind": "menu", "source": "menu"}]
    # called by the model itself, the read tool follows its own refresh list
    direct = await gateway.call(make_ctx("s9", 5), "cart", {})
    assert [d["kind"] for d in direct.display] == ["cart", "menu"]


@pytest.mark.asyncio
async def test_refreshed_screen_replaces_the_calls_own_copy_and_a_failed_refresh_is_skipped():
    stale = {"lines": [], "total": 0}
    tools = {
        "change": ("write", DisplaySpec(kind="cart", path="cart", refresh=["cart"]), [ok({"cart": stale})], None, {}),
        "cart": ("read", DisplaySpec(kind="cart"), [ok(CART)], None, {}),
    }
    gateway, _ = screens(tools)
    result = await gateway.call(make_ctx(), "change", {})
    assert result.display == [{"kind": "cart", "data": CART, "source": "cart"}]

    for broken in ([{"ok": False, "text": "없음", "error_type": "not_found", "data": {"display_notice": "숨김"}}],
                   [AdapterUnavailable("down")], [RuntimeError("bug")]):
        tools["cart"] = ("read", DisplaySpec(kind="cart"), broken, None, {"retries": 0})
        gateway, _ = screens(tools)
        result = await gateway.call(make_ctx(), "change", {})
        assert result.ok and result.executed                    # the change itself stands
        assert result.display == [{"kind": "cart", "data": stale, "source": "change"}]


@pytest.mark.asyncio
async def test_refresh_skips_tools_that_need_something_first():
    gateway, adapters = screens({
        "change": ("write", DisplaySpec(kind="receipt", refresh=["cart"]), [ok({"order_no": "K-1"})], None, {}),
        "cart": ("read", DisplaySpec(kind="cart"), [ok(CART)], None, {"requires": ["verified"]}),
    })
    result = await gateway.call(make_ctx(), "change", {})
    assert [d["kind"] for d in result.display] == ["receipt"] and adapters["cart"].calls == []


@pytest.mark.asyncio
async def test_proposal_shows_a_confirm_card_with_the_preview_values_and_execution_shows_the_display(recorder):
    preview = {"ok": True, "data": {"lines": CART["lines"], "total": 4500, "summary": "아메리카노 1잔"}}
    gateway, adapters = screens({
        "pay": ("write", DisplaySpec(kind="receipt", refresh=["cart"]), [ok({"order_no": "K-7", "total": 4500})], preview,
                {"confirm_template": "{summary} 결제할까요?"}),
        "cart": ("read", DisplaySpec(kind="cart"), [ok(CART)], None, {}),
    }, recorder)
    proposal = await gateway.call(make_ctx("s1", 1), "pay", {})
    assert proposal.needs_confirmation and proposal.confirmation_text == "아메리카노 1잔 결제할까요?"
    assert proposal.display == [{"kind": "confirm", "source": "pay", "data": {
        "text": "아메리카노 1잔 결제할까요?", "tool": "pay", "lines": CART["lines"], "total": 4500,
        "summary": "아메리카노 1잔"}}]
    assert adapters["cart"].calls == [] and adapters["pay"].calls == []     # nothing ran, nothing was redrawn
    assert recorder.spans[-1][0].output["display"] == [{"kind": "confirm", "source": "pay"}]

    for turn, text in ((1, "네"), (2, "얼마라고요?")):                        # same turn, then no clear consent
        refused = await gateway.call(make_ctx("s1", turn, text), "pay", {})
        assert refused.error_type == "not_confirmed" and refused.display == []
    done = await gateway.call(make_ctx("s1", 3, "네"), "pay", {})
    assert done.executed and [(d["kind"], d["source"]) for d in done.display] == [("receipt", "pay"), ("cart", "cart")]
    assert done.display[0]["data"] == {"order_no": "K-7", "total": 4500}


@pytest.mark.asyncio
async def test_confirm_card_of_a_tool_without_a_preview_or_a_display(gateway, shop):
    await verify(gateway)
    proposal = await gateway.call(make_ctx(turn=2), "request_refund", REFUND)
    assert proposal.display == [{"kind": "confirm", "source": "request_refund", "data": {
        "text": "무선 이어폰 32000원 환불을 접수할까요?", "tool": "request_refund",
        "order_id": "A1001", "product_name": "무선 이어폰", "amount": 32000}}]
    done = await gateway.call(make_ctx(turn=3, text="네"), "request_refund", REFUND)
    assert done.executed and done.display == []                 # its contract names no screen

    plain, _ = scripted("write", confirm_template="진행할까요?")
    card = (await plain.call(make_ctx(), "t", {"key": "x"})).display
    assert card == [{"kind": "confirm", "source": "t", "data": {"text": "진행할까요?", "tool": "t"}}]


@pytest.mark.asyncio
async def test_failed_call_shows_nothing_unless_the_system_sends_a_notice():
    def failing(data):
        return [{"ok": False, "text": "거절", "error_type": "precondition_failed", "data": data}]
    gateway, adapters = screens({
        "silent": ("write", DisplaySpec(kind="cart", refresh=["cart"]), failing({"cart": CART}), None, {}),
        "sentence": ("write", DisplaySpec(kind="cart", refresh=["cart"]), failing({"display_notice": " 품절입니다. "}), None, {}),
        "full": ("read", DisplaySpec(kind="cart"), failing({"display_notice": {"level": "warn", "text": "점검 중"}}), None, {}),
        "bare": ("read", None, failing({"display_notice": {"text": "안내"}}), None, {}),
        "empty": ("read", None, failing({"display_notice": {"level": "info"}}), None, {}),
        "cart": ("read", DisplaySpec(kind="cart"), [ok(CART)], None, {}),
    })
    assert (await gateway.call(make_ctx(), "silent", {})).display == []
    assert (await gateway.call(make_ctx(), "sentence", {})).display == [
        {"kind": "notice", "data": {"level": "info", "text": "품절입니다."}, "source": "sentence"}]
    assert (await gateway.call(make_ctx(), "full", {})).display == [
        {"kind": "notice", "data": {"level": "warn", "text": "점검 중"}, "source": "full"}]
    assert (await gateway.call(make_ctx(), "bare", {})).display == [
        {"kind": "notice", "data": {"level": "info", "text": "안내"}, "source": "bare"}]
    assert (await gateway.call(make_ctx(), "empty", {})).display == []
    assert adapters["cart"].calls == []                         # a failure redraws nothing
    # failures of the gateway's own rules show nothing either
    assert (await gateway.call(make_ctx(), "nope", {})).display == []
    assert (await gateway.call(make_ctx(), "silent", "not json")).display == []
    assert (await gateway.call(make_ctx(speculative=True), "silent", {})).display == []


@pytest.mark.asyncio
async def test_preview_refusal_can_carry_a_notice():
    gateway, _ = screens({"pay": ("write", DisplaySpec(kind="receipt"), ["ok"],
                                  {"ok": False, "text": "빈 주문", "error_type": "precondition_failed",
                                   "data": {"display_notice": "주문 내역이 비어 있습니다."}},
                                  {"confirm_template": "결제할까요?"})})
    result = await gateway.call(make_ctx(), "pay", {})
    assert not result.ok and not result.needs_confirmation
    assert result.display == [{"kind": "notice", "data": {"level": "info", "text": "주문 내역이 비어 있습니다."}, "source": "pay"}]


@pytest.mark.asyncio
async def test_repeatable_write_runs_every_time_and_others_still_run_once():
    again = {"ok": True, "text": "담았습니다", "data": {"n": 1}, "executed": True, "repeatable": True}
    gateway, adapters = screens({
        "add": ("write", None, [again], None, {}),
        "once": ("write", DisplaySpec(kind="receipt"), [ok({"order_no": "K-1"})], None, {}),
    })
    for turn in (1, 1, 2):
        result = await gateway.call(make_ctx(turn=turn), "add", {"key": "americano"})
        assert result.ok and result.executed
    assert len(adapters["add"].calls) == 3
    first = await gateway.call(make_ctx(), "once", {"key": "a"})
    second = await gateway.call(make_ctx(turn=2), "once", {"key": "a"})
    assert first.executed and not second.executed and len(adapters["once"].calls) == 1
    assert second.display == first.display == [{"kind": "receipt", "data": {"order_no": "K-1"}, "source": "once"}]


def test_gateway_refuses_a_refresh_that_is_not_a_registered_read_tool():
    def build(display, other_effect="read", **other):
        change = make_contract("change", effect="write", display=display)
        target = make_contract("cart", effect=other_effect, display=DisplaySpec(kind="cart"), **other)
        return Gateway({"change": change, "cart": target},
                       adapters={"change": ScriptedAdapter(change), "cart": ScriptedAdapter(target)})
    build(DisplaySpec(kind="cart", refresh=["cart"]))
    for display, effect, more, expected in (
            (DisplaySpec(kind="cart", refresh=["nope"]), "read", {}, "'nope'"),
            (DisplaySpec(kind="cart", refresh=["cart"]), "write", {}, "조회 도구"),
            (DisplaySpec(kind="cart", refresh=["cart"]), "read",
             {"input_schema": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}}, "인자 없이")):
        with pytest.raises(ContractError) as error:
            build(display, effect, **more)
        assert any(p.startswith("change.yaml: display.refresh") and expected in p for p in error.value.problems)


@pytest.mark.asyncio
async def test_broken_display_data_does_not_break_the_call():
    class Unspeakable:
        def __deepcopy__(self, memo):
            raise RuntimeError("cannot be copied")
    gateway, _ = screens({"t": ("read", DisplaySpec(kind="cart"), [ok({"x": Unspeakable()})], None, {})})
    result = await gateway.call(make_ctx(), "t", {})
    assert result.ok and result.display == []
