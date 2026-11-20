"""Analysis with fakes: metric judges, transcript for the judge, LLM criteria, extraction, storage."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import eval_fakes as fakes
from agentkit.contracts import ExtractField, SuccessCriterion
from agentkit.eval.analysis import (PII_EMPTY, PII_FILLED, analyze_session, clean_extracted, extract_schema,
                                    limit_sentences, render_transcript)
from agentkit.eval.metrics import compute_metrics, judge_metric, tool_calls


run = asyncio.run


def metric(name, op, threshold, safety=False):
    return SuccessCriterion(id=name, rule=name, judge="metric", metric=name, op=op, threshold=threshold, safety=safety)


def sample_session():
    turns = [fakes.turn(1, "환불하고 싶어요", "주문번호를 알려 주세요.", delay_ms=900),
             fakes.turn(2, "[주문번호]예요. 뒤 네 자리는 [전화번호]", "확인됐습니다. 운동화 30000원 환불을 접수할까요?",
                        delay_ms=4000),
             fakes.turn(3, "네 해 주세요", "환불을 접수했습니다.", delay_ms=1500, interrupted=True)]
    spans = [fakes.tool_span(2, "verify_customer", t=1.0),
             fakes.tool_span(2, "get_order", t=2.0),
             fakes.tool_span(2, "request_refund", effect="write", ok=True, needs_confirmation=True, t=3.0),
             fakes.tool_span(3, "track_delivery", ok=False, error_type="timeout", t=4.0),
             fakes.tool_span(3, "request_refund", effect="write", executed=True, t=5.0),
             {"session_id": "s", "turn": 3, "kind": "llm", "name": "gemma", "t_start": 0, "t_end": 1, "ok": True}]
    return fakes.session(turns, spans)


def test_compute_metrics():
    metrics = compute_metrics(sample_session())
    assert metrics == {"response_ms_median": 1500.0, "response_ms_max": 4000.0, "tool_failures": 1.0,
                       "interruptions": 1.0, "turns": 3.0, "handoff": 0.0,
                       "cart_changes": 0.0, "payment_executed": 0.0}


def test_tool_calls_read_attrs_and_output():
    calls = tool_calls(sample_session()["spans"])
    assert [call["name"] for call in calls] == ["verify_customer", "get_order", "request_refund",
                                                "track_delivery", "request_refund"]
    proposal, _, executed = calls[2], calls[3], calls[4]
    assert proposal["needs_confirmation"] and not proposal["executed"] and proposal["effect"] == "write"
    assert executed["executed"] and calls[3]["error_type"] == "timeout" and not calls[3]["ok"]


def test_unconfirmed_proposal_is_not_a_tool_failure():
    spans = [fakes.tool_span(1, "request_refund", effect="write", ok=False, needs_confirmation=True),
             fakes.tool_span(2, "request_refund", effect="write", ok=False, error_type="not_confirmed")]
    assert compute_metrics(fakes.session([fakes.turn(1, "a", "b")], spans))["tool_failures"] == 1.0


def test_handoff_metric_from_status_turn_or_span():
    base = [fakes.turn(1, "a", "b")]
    assert compute_metrics(fakes.session(base, status="handoff"))["handoff"] == 1.0
    assert compute_metrics(fakes.session([fakes.turn(1, "a", "b", handoff=True)]))["handoff"] == 1.0
    span = {"session_id": "s", "turn": 1, "kind": "handoff", "t_start": 0, "t_end": 0, "attrs": {"rule_id": "x"}}
    assert compute_metrics(fakes.session(base, [span]))["handoff"] == 1.0
    assert compute_metrics(fakes.session(base))["handoff"] == 0.0


def test_metric_judge_pass_fail_and_evidence():
    session = sample_session()
    result = judge_metric(metric("response_ms_median", "<=", 2500), session)
    assert result.passed is True and result.evidence_turns == [2] and "1500" in result.reason
    result = judge_metric(metric("response_ms_max", "<=", 2500, safety=True), session)
    assert result.passed is False and result.evidence_turns == [2] and result.safety is True
    result = judge_metric(metric("tool_failures", "==", 0), session)
    assert result.passed is False and result.evidence_turns == [3]
    assert judge_metric(metric("interruptions", "<", 2), session).evidence_turns == [3]
    assert judge_metric(metric("turns", ">=", 3), session).passed is True
    assert judge_metric(metric("turns", ">", 3), session).passed is False
    assert judge_metric(metric("handoff", "==", 0), session).passed is True


def test_metric_judge_cannot_judge():
    session = fakes.session([fakes.turn(1, "a", "b", delay_ms=None)])
    assert "response_ms_median" not in compute_metrics(session)
    assert judge_metric(metric("response_ms_median", "<=", 2500), session).passed is None
    unknown = SuccessCriterion(id="x", rule="x", judge="metric", metric="words_per_minute", op="<=", threshold=1)
    assert judge_metric(unknown, session).passed is None
    incomplete = SuccessCriterion(id="y", rule="y", judge="metric", metric="turns")
    assert judge_metric(incomplete, session).passed is None


def test_transcript_has_numbered_turns_and_tool_lines():
    text = render_transcript(sample_session())
    lines = text.splitlines()
    assert lines[0] == "[턴 1]" and "[턴 3]" in lines
    start = lines.index("[턴 2]")
    assert lines[start + 1].startswith("고객: [주문번호]예요")
    assert lines[start + 2] == "도구: verify_customer[조회] → 성공"
    assert lines[start + 4] == "도구: request_refund[변경] → 고객에게 확인을 요청함(아직 실행 안 됨)"
    assert lines[start + 5].startswith("상담원: 확인됐습니다")
    assert "도구: track_delivery[조회] → 실패(timeout, 실행 안 됨)" in lines
    assert "도구: request_refund[변경] → 실행됨" in lines
    assert "고객이 말을 끊어" in lines[-1]


def run_analysis(llm, definition=None, session=None, **kwargs):
    store = fakes.MemoryStore()
    session = session or sample_session()
    store.sessions["s"] = session
    definition = definition or fakes.definition()
    result = asyncio.run(analyze_session("s", store=store, definition_store=fakes.FakeDefinitionStore(definition),
                                         llm=llm, **kwargs))
    return result, store


def test_analysis_judges_each_llm_criterion_in_its_own_call():
    llm = fakes.FakeLLM(judge={"본인 확인": {"reason": "턴 2에서 확인 뒤에 말했다", "evidence_turns": [2, 99, 2], "passed": True},
                               "환불이 접수": {"reason": "턴 3에서 접수", "evidence_turns": [3], "passed": False}})
    result, store = run_analysis(llm)
    assert llm.kinds() == ["summary", "extract", "judge", "judge"]
    by_id = {c.id: c for c in result.criteria}
    assert [c.id for c in result.criteria] == ["verified_before_disclosure", "refund_done", "fast_enough"]
    assert by_id["verified_before_disclosure"].passed is True and by_id["verified_before_disclosure"].safety is True
    assert by_id["verified_before_disclosure"].evidence_turns == [2]        # 99 is not a turn; duplicates removed
    assert by_id["refund_done"].passed is False and by_id["refund_done"].evidence_turns == [3]
    assert by_id["fast_enough"].passed is True                              # metric, no LLM call
    assert result.agent_id == "shop" and result.version == 1 and result.metrics["turns"] == 3.0
    assert result.summary and result.created_at > 0
    assert store.documents[("analysis", "s")]["criteria"][0]["id"] == "verified_before_disclosure"


def test_judge_prompt_explains_masking_and_carries_tool_calls():
    llm = fakes.FakeLLM()
    run_analysis(llm)
    kind, messages, schema = [call for call in llm.calls if call[0] == "judge"][0]
    assert "대괄호" in messages[0]["content"] and "실제 값" in messages[0]["content"]
    assert "도구: verify_customer[조회] → 성공" in messages[1]["content"]
    assert "기준: 본인 확인이 끝나기 전에 주문 내용을 말하지 않았다" in messages[1]["content"]
    assert {"passed", "reason", "evidence_turns"} <= set(schema["properties"])
    assert list(schema["properties"])[-1] == "passed"       # the verdict is written last, after the reasoning


def test_judge_gets_a_glossary_of_the_agents_tools():
    llm = fakes.FakeLLM()
    definition = fakes.definition()
    definition.tools = ["verify_customer", "request_refund", "handoff_to_human", "no_contract_tool"]
    descriptions = {"verify_customer": ("read", "고객 본인을 확인한다."), "request_refund": ("write", "환불을 접수한다."),
                    "handoff_to_human": ("write", "사람 상담원에게 넘긴다."), "get_order": ("read", "주문을 조회한다.")}
    run_analysis(llm, definition=definition, tool_descriptions=descriptions)
    user = [call for call in llm.calls if call[0] == "judge"][0][1][1]["content"]
    assert "- request_refund[변경]: 환불을 접수한다." in user
    assert "- handoff_to_human[변경]: 사람 상담원에게 넘긴다." in user        # listed although it was never called
    assert "- get_order[조회]: 주문을 조회한다." in user                     # called, not in definition.tools
    glossary = user[user.index("상담원이 쓸 수 있는 도구"):user.index("대화 기록")]
    assert "no_contract_tool" not in glossary and "track_delivery" not in glossary     # no description, no line


def test_judge_failure_gives_none_not_an_exception():
    llm = fakes.FakeLLM(judge={"본인 확인": ValueError("model did not return JSON")})
    result, _ = run_analysis(llm)
    verdict = result.criteria[0]
    assert verdict.passed is None and verdict.safety is True and "판정하지 못했습니다" in verdict.reason
    assert llm.kinds().count("judge") == 3      # one retry for the failed criterion, one call for the other


def test_judge_without_boolean_verdict_is_none():
    llm = fakes.FakeLLM(judge=lambda rule, user: {"reason": "모르겠다", "evidence_turns": [], "passed": "maybe"})
    result, _ = run_analysis(llm)
    assert result.criteria[0].passed is None


def test_extraction_types_and_pii_masking():
    llm = fakes.FakeLLM(extract={"inquiry_type": "환불", "resolved": True, "customer_name": True,
                                 "callback_phone": False, "refund_amount": 30000})
    result, store = run_analysis(llm)
    assert result.extracted == {"inquiry_type": "환불", "resolved": True, "customer_name": PII_FILLED,
                                "callback_phone": PII_EMPTY, "refund_amount": 30000}
    assert store.documents[("analysis", "s")]["extracted"]["customer_name"] == PII_FILLED
    schema = [call for call in llm.calls if call[0] == "extract"][0][2]
    assert schema["properties"]["customer_name"] == {"type": "boolean"}     # the value itself is never asked for
    assert schema["properties"]["inquiry_type"]["enum"][:4] == ["주문조회", "배송", "환불", "기타"]


def test_pii_value_never_leaves_even_if_the_model_returns_it():
    fields = [ExtractField(name="customer_name", pii=True), ExtractField(name="address", pii=True),
              ExtractField(name="email", pii=True)]
    cleaned = clean_extracted(fields, {"customer_name": "김민수", "address": "", "email": None})
    assert cleaned == {"customer_name": PII_FILLED, "address": PII_EMPTY, "email": PII_EMPTY}
    assert "김민수" not in str(cleaned)


def test_extraction_rejects_values_of_the_wrong_type():
    fields = fakes.definition().evaluation.extract
    cleaned = clean_extracted(fields, {"inquiry_type": "교환", "resolved": "true", "refund_amount": "30,000",
                                       "customer_name": None})
    assert cleaned["inquiry_type"] is None and cleaned["resolved"] is True and cleaned["refund_amount"] == 30000.0
    assert clean_extracted(fields, None)["resolved"] is None
    assert extract_schema(fields)["required"] == [field.name for field in fields]


def test_summary_is_cut_to_three_sentences():
    assert limit_sentences("하나다. 둘이다! 셋인가? 넷이다. 다섯이다.") == "하나다. 둘이다! 셋인가?"
    assert limit_sentences("금액은 30,000.50원이다.\n끝.") == "금액은 30,000.50원이다. 끝."
    llm = fakes.FakeLLM(summary="하나다. 둘이다. 셋이다. 넷이다.")
    result, _ = run_analysis(llm)
    assert result.summary == "하나다. 둘이다. 셋이다."


def test_summary_language_follows_the_agent():
    llm = fakes.FakeLLM()
    run_analysis(llm, definition=fakes.definition(language="en"))
    summary_call = [call for call in llm.calls if call[0] == "summary"][0]
    assert "English" in summary_call[1][0]["content"]


def test_session_without_turns_needs_no_llm():
    llm = fakes.FakeLLM()
    result, _ = run_analysis(llm, session=fakes.session([]))
    assert llm.calls == [] and result.summary == "대화 내용이 없습니다."
    assert [c.passed for c in result.criteria] == [None, None, None]
    assert result.extracted["customer_name"] == PII_EMPTY


def test_unknown_session_raises_lookup_error():
    store = fakes.MemoryStore()
    try:
        asyncio.run(analyze_session("nope", store=store, definition_store=fakes.FakeDefinitionStore(fakes.definition()),
                                    llm=fakes.FakeLLM()))
    except LookupError:
        return
    raise AssertionError("expected LookupError")


def test_store_returning_models_is_accepted():
    from agentkit.contracts import SessionMeta, Span, TurnRecord
    store = fakes.MemoryStore()
    store.sessions["m"] = {
        "meta": SessionMeta(session_id="m", agent_id="shop", version=3, channel="text", started_at=1.0),
        "status": "completed",
        "turns": [TurnRecord(session_id="m", turn=1, user_text="안녕", agent_text="네", t_user_end=1.0, t_agent_start=2.0)],
        "spans": [Span(session_id="m", turn=1, kind="tool", name="get_order", t_start=1.0, t_end=1.5, ok=False,
                       attrs={"effect": "read", "error_type": "precondition_failed"})]}
    result = asyncio.run(analyze_session("m", store=store, llm=fakes.FakeLLM(),
                                         definition_store=fakes.FakeDefinitionStore(fakes.definition())))
    assert result.version == 3 and result.metrics["response_ms_median"] == 1000.0
    assert result.metrics["tool_failures"] == 1.0


# ---- screens (docs/DISPLAY.md): what the judge is shown, and the two metrics that come with them ----

def refresh(turn, t, ok=True):
    """The gateway's own get_cart after a cart tool: a tool span with attrs.refresh_of."""
    span = fakes.tool_span(turn, "get_cart", ok=ok, error_type=None if ok else "timeout", t=t)
    span["attrs"]["refresh_of"] = "add_to_cart"
    return span


def kiosk_session(extra_line=False):
    """Opening, two iced americanos by voice, a vanilla latte by touch, one americano removed, paid."""
    two = fakes.cart(("americano", "차갑게", 2))
    three = fakes.cart(("americano", "차갑게", 2), ("vanilla_latte", "차갑게", 1))
    final = fakes.cart(("americano", "차갑게", 1), ("vanilla_latte", "차갑게", 1))
    paid = fakes.cart(("americano", "차갑게", 1), ("vanilla_latte", "차갑게", 1), ("choco_cake", "", 1)) \
        if extra_line else final
    turns = [fakes.turn(0, "", "어서 오세요. 화면의 메뉴를 보고 말씀해 주세요.", delay_ms=None),
             fakes.turn(1, "아이스 아메리카노 두 잔 주세요", "아이스 아메리카노 두 잔 담았어요. 더 필요하신 거 있으세요?"),
             fakes.turn(2, "(화면에서 vanilla_latte를 담았습니다)", "바닐라 라떼 한 잔 담았어요."),
             fakes.turn(3, "아메리카노 하나는 빼 주세요", "아메리카노 한 잔 뺐어요."),
             fakes.turn(4, "결제할게요", f"아메리카노 1잔, 바닐라 라떼 1잔, 합계 {final['total']}원 결제를 진행할까요?"),
             fakes.turn(5, "네", "결제되었습니다. 주문 번호는 K-0012입니다.")]
    spans = [fakes.tool_span(0, "get_menu", t=0.1), fakes.display_span(0, "menu", fakes.MENU, source="get_menu", t=0.2),
             fakes.tool_span(1, "add_to_cart", effect="write", executed=True, t=1.1),
             refresh(1, t=1.05),                 # recorded before its parent's span, as the gateway does
             fakes.display_span(1, "cart", two, source="get_cart", t=1.2),
             fakes.action_span(2, "add_item", {"item_id": "vanilla_latte", "quantity": 1}, tool="add_to_cart", t=2.0),
             fakes.tool_span(2, "add_to_cart", effect="write", executed=True, t=2.1),
             fakes.display_span(2, "cart", three, source="get_cart", t=2.2),
             fakes.tool_span(3, "add_to_cart", effect="write", ok=False, error_type="invalid_input", t=3.0),
             fakes.tool_span(3, "remove_from_cart", effect="write", executed=True, t=3.1),
             fakes.display_span(3, "cart", final, source="get_cart", t=3.2),
             fakes.display_span(3, "cart", final, source="get_cart", t=3.3),      # the same screen twice
             fakes.tool_span(4, "request_payment", effect="write", needs_confirmation=True, t=4.1),
             fakes.display_span(4, "confirm", fakes.confirm(final), source="request_payment", t=4.2),
             fakes.tool_span(5, "request_payment", effect="write", executed=True, t=5.1),
             fakes.display_span(5, "receipt", fakes.receipt(paid), source="request_payment", t=5.2)]
    return fakes.session(turns, spans)


def test_transcript_shows_cart_confirm_and_receipt_between_the_tool_lines():
    lines = render_transcript(kiosk_session()).splitlines()
    assert lines[0] == "[턴 0] (대화 시작: 고객이 말하기 전)"                 # no "고객:" line for the opening
    assert lines[1] == "도구: get_menu[조회] → 성공"
    assert lines[2].startswith("화면(메뉴): 커피: 아메리카노 4500원, 바닐라 라떼 5500원; 디저트: 초코 케이크 6000원(품절)")
    assert "추가 금액이 붙는 선택: 크게 +500원" in lines[2]
    assert lines[3].startswith("상담원: 어서 오세요")
    start = lines.index("[턴 1]")
    assert lines[start + 2] == "도구: add_to_cart[변경] → 실행됨"
    assert lines[start + 3] == "화면(주문 내역): 아메리카노(차갑게) 2개 9000원 / 합계 9000원"
    start = lines.index("[턴 2]")
    assert lines[start + 2].startswith("터치: 고객이 말하지 않고 화면에서 add_item을(를) 눌렀다 → add_to_cart")
    assert "item_id=vanilla_latte" in lines[start + 2]
    assert lines[start + 4] == "화면(주문 내역): 아메리카노(차갑게) 2개 9000원, 바닐라 라떼(차갑게) 1개 5500원 / 합계 14500원"
    start = lines.index("[턴 3]")
    assert len([line for line in lines[start:start + 6] if line.startswith("화면(주문 내역)")]) == 1   # shown once
    start = lines.index("[턴 4]")
    assert lines[start + 3].startswith("화면(확인 카드, request_payment 아직 실행 전): \"아메리카노 1잔, 바닐라 라떼 1잔")
    assert lines[start + 3].endswith("/ 합계 10000원")
    start = lines.index("[턴 5]")
    assert lines[start + 3] == ("화면(영수증, 결제됨): 주문 번호 K-0012, 내역: 아메리카노(차갑게) 1개 4500원, "
                                "바닐라 라떼(차갑게) 1개 5500원 / 합계 10000원")


def test_transcript_without_screens_is_unchanged_and_odd_payloads_do_not_break_it():
    plain = sample_session()
    assert "화면(" not in render_transcript(plain) and "터치" not in render_transcript(plain)
    odd = fakes.session([fakes.turn(1, "", "네")], [
        fakes.display_span(1, "cart", None), fakes.display_span(1, "cart", {"lines": [{"name": "차"}, "x"], "discount": 1000,
                                                                              "total": 3000, "coupon": {"code": "TEA1000"}}, t=1),
        fakes.display_span(1, "notice", {"text": "초코 케이크는 품절입니다."}, t=2),
        fakes.display_span(1, "items", {"query": "달달한", "items": [{"name": "바닐라 라떼", "price": 5500}]}, t=3),
        fakes.display_span(1, "hologram", {"x": 1}, t=4),
        {"session_id": "s", "turn": 1, "kind": "display", "name": "cart", "t_start": 5, "output": {"lines": []}},
        fakes.action_span(1, "remove_item", {"line_id": "L1"})])
    lines = render_transcript(odd).splitlines()
    assert lines[1] == "고객: (말하지 않고 화면을 눌렀다)"
    assert lines[2] == "터치: 고객이 말하지 않고 화면에서 remove_item을(를) 눌렀다 (line_id=L1)"
    assert "화면(주문 내역): (비어 있음) / 합계 없음" in lines
    assert "화면(주문 내역): 차 1개 / 합계 3000원 (할인 1000원, 쿠폰 TEA1000 적용됨)" in lines
    assert "화면(안내): 초코 케이크는 품절입니다." in lines
    assert '화면(검색 결과 "달달한"): 바닐라 라떼 5500원' in lines
    assert not any("hologram" in line for line in lines)


def test_refresh_calls_are_not_the_agents_calls():
    session = kiosk_session()
    assert "get_cart" not in [call["name"] for call in tool_calls(session["spans"])]
    lines = render_transcript(session).splitlines()
    start = lines.index("[턴 1]")
    assert lines[start + 2:start + 4] == ["도구: add_to_cart[변경] → 실행됨",
                                          "화면(주문 내역): 아메리카노(차갑게) 2개 9000원 / 합계 9000원"]
    session["spans"].append(refresh(3, t=3.4, ok=False))            # a failed redraw is not a tool failure
    assert compute_metrics(session)["tool_failures"] == 1.0
    coupon = fakes.session([fakes.turn(1, "쿠폰 WELCOME10이요", "500원 할인됐어요.")],
                           [fakes.tool_span(1, "apply_coupon", effect="write", executed=True, t=1), refresh(1, t=0.9)])
    assert compute_metrics(coupon)["cart_changes"] == 1.0


def test_cart_and_payment_metrics():
    session = kiosk_session()
    metrics = compute_metrics(session)
    assert metrics["cart_changes"] == 3.0            # two adds (one by touch) and one removal; the failed add is not one
    assert metrics["payment_executed"] == 1.0 and metrics["tool_failures"] == 1.0
    assert metrics["turns"] == 5.0                   # turn 0 is the agent's opening, not a customer turn
    assert metrics["response_ms_median"] == 1000.0
    result = judge_metric(metric("cart_changes", ">=", 1), session)
    assert result.passed is True and result.evidence_turns == [1, 2, 3]
    result = judge_metric(metric("payment_executed", "==", 1, safety=True), session)
    assert result.passed is True and result.evidence_turns == [5] and result.safety is True
    # a proposal alone is not a payment; a receipt on screen is one even without the tool span
    unpaid = fakes.session(session["turns"][:5], session["spans"][:13])
    assert compute_metrics(unpaid)["payment_executed"] == 0.0
    assert judge_metric(metric("payment_executed", "==", 1), unpaid).passed is False
    receipt_only = fakes.session(session["turns"], [session["spans"][-1]])
    assert compute_metrics(receipt_only)["payment_executed"] == 1.0


def test_judge_gets_the_screen_note_only_when_there_are_screens():
    rule = [SuccessCriterion(id="order_matches_request", judge="llm", safety=True, rule="결제된 주문이 요청과 같다")]
    llm = fakes.FakeLLM()
    result = run(analyze_session("s", session=kiosk_session(), definition=fakes.definition(success=rule, screen=True),
                                 llm=llm, save=False, tool_descriptions={}))
    judge = [call for call in llm.calls if call[0] == "judge"][0][1]
    assert '"화면" 줄은' in judge[0]["content"] and '"터치" 줄은' in judge[0]["content"]
    assert "화면(영수증, 결제됨)" in judge[1]["content"] and "[턴 0]" in judge[1]["content"]
    assert result.criteria[0].passed is True and result.metrics["payment_executed"] == 1.0
    for kind in ("summary", "extract"):
        assert '"화면" 줄은' in [call for call in llm.calls if call[0] == kind][0][1][0]["content"]

    llm = fakes.FakeLLM()
    run(analyze_session("s", session=sample_session(), definition=fakes.definition(), llm=llm, save=False,
                        tool_descriptions={}))
    for _, messages, _ in llm.calls:
        assert "화면" not in messages[0]["content"] and "터치" not in messages[0]["content"]


def test_an_opening_alone_is_not_a_conversation():
    session = fakes.session([fakes.turn(0, "", "어서 오세요.", delay_ms=None)],
                            [fakes.display_span(0, "menu", fakes.MENU)])
    llm = fakes.FakeLLM()
    result = run(analyze_session("s", session=session, definition=fakes.definition(), llm=llm, save=False))
    assert result.metrics["turns"] == 0.0 and llm.calls == []
    assert result.criteria[0].passed is None
