"""Simulated customer, case replay, verdict logic, suite, comparison, saved cases, REST: all with fakes."""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

import pytest

import eval_fakes as fakes
from agentkit import contracts
from agentkit.contracts import AgentTurn, CriterionResult

TestRun = contracts.TestRun
TestRun.__test__ = False
from agentkit.eval import compare, run_case, run_persona, run_suite, save_case, verdict
from agentkit.eval import deps
from agentkit.eval.screen import menu_text, resolve_touch
from agentkit.eval.simulate import SimulatedCustomer, fill_facts, persona_touches, unresolved_markers


def result(criterion_id, passed, safety=False):
    return CriterionResult(id=criterion_id, passed=passed, safety=safety)


# ---- verdict -------------------------------------------------------------------------------------

def test_verdict_all_expectations_met():
    assert verdict({"a": True, "b": False}, [result("a", True), result("b", False)]) == (True, [])


def test_verdict_mismatch_and_unjudged_and_missing():
    criteria = [result("a", False), result("b", None), result("d", True)]
    assert verdict({"a": True, "b": True, "c": True, "d": True}, criteria) == (False, ["a", "b", "c"])
    assert verdict({"b": False}, [result("b", None)]) == (False, ["b"])     # None is never "as expected"


def test_verdict_safety_failure_blocks_even_when_not_expected():
    passed, mismatches = verdict({"a": True}, [result("a", True), result("s", False, safety=True)])
    assert passed is False and mismatches == []
    assert verdict({}, [result("x", False)]) == (True, [])                  # a failed non-safety criterion outside expect
    assert verdict({}, [result("s", None, safety=True)]) == (True, [])      # unjudged safety, not in expect


# ---- simulated customer --------------------------------------------------------------------------

def agent_script(text, number, label):
    if number == 1:
        return "주문번호와 휴대폰 뒤 네 자리를 알려 주세요."
    if number == 2:
        return AgentTurn(text="확인됐습니다. 운동화 30000원 환불을 접수할까요?", ms=1200,
                         tool_calls=[{"name": "verify_customer", "ok": True}, {"name": "request_refund", "ok": True}])
    return AgentTurn(text="환불을 접수했습니다.", ms=900, tool_calls=[{"name": "request_refund", "ok": True, "executed": True}])


def run(coroutine):
    return asyncio.run(coroutine)


def persona_run(llm, script=agent_script, persona=None, **kwargs):
    store, drivers = fakes.MemoryStore(), []
    definition = fakes.definition()
    item = run(run_persona("shop", 3, persona or fakes.persona(), store=store, llm=llm,
                           definition_store=fakes.FakeDefinitionStore(definition),
                           driver_factory=fakes.driver_factory(store, script, drivers, **kwargs)))
    return item, store, drivers[0] if drivers else None


def test_persona_stops_when_the_customer_says_done():
    llm = fakes.FakeLLM(customer=[
        {"progress": "본인 확인 중", "done": False, "utterance": "A1001이고 뒤 네 자리는 1234예요"},
        {"progress": "환불 확인 요청 받음", "done": False, "utterance": "네, 해 주세요"},
        {"progress": "환불 접수됨", "done": True, "utterance": "감사합니다"}])
    item, store, driver = persona_run(llm)
    assert driver.heard == ["환불하고 싶어요", "A1001이고 뒤 네 자리는 1234예요", "네, 해 주세요"]   # closing not sent
    assert driver.closed == "completed"
    assert item.kind == "persona" and item.item_id == "refund_small" and item.session_id == driver.session_id
    assert item.passed is True and item.mismatches == [] and item.metrics["turns"] == 3.0
    assert ("analysis", driver.session_id) in store.documents
    assert llm.kinds().count("customer") == 3


def test_persona_facts_go_to_the_customer_prompt_only():
    llm = fakes.FakeLLM(customer=[{"progress": "", "done": True, "utterance": "네"}])
    item, store, driver = persona_run(llm, persona=fakes.persona(opening=None, facts={"order_id": "A1001", "phone_last4": "9876"}))
    system = [call for call in llm.calls if call[0] == "customer"][0][1][0]["content"]
    assert "order_id: A1001" in system and "phone_last4: 9876" in system and "급한 말투" in system
    # the first decision is the generated opening: `done` is ignored there, the utterance is said
    assert driver.heard[0] == "네" and len(driver.heard) == 1
    meta = store.sessions[driver.session_id]["meta"]
    assert "9876" not in str(meta) and meta["label"] == "refund_small" and meta["simulated"] is True


def test_persona_hard_stop_at_max_turns():
    llm = fakes.FakeLLM(customer=lambda user, system: {"progress": "", "done": False, "utterance": "그래서요?"})
    item, _, driver = persona_run(llm, script=lambda text, n, label: "잠시만요.", persona=fakes.persona(max_turns=4))
    assert len(driver.heard) == 4
    assert llm.kinds().count("customer") == 3       # no decision is asked for after the last allowed turn


def test_persona_stops_at_handoff():
    def script(text, number, label):
        if number == 2:
            return AgentTurn(text="상담원에게 연결해 드리겠습니다.", handoff=True, ms=700,
                             tool_calls=[{"name": "handoff_to_human", "ok": True, "executed": True}])
        return "주문번호를 알려 주세요."
    llm = fakes.FakeLLM(customer=lambda user, system: {"progress": "", "done": False, "utterance": "A1001이요"})
    item, store, driver = persona_run(llm, script=script)
    assert len(driver.heard) == 2 and driver.closed == "handoff"
    assert item.metrics["handoff"] == 1.0


def test_persona_is_asked_again_when_it_ends_on_an_open_proposal():
    def script(text, number, label):
        if number == 1:
            return AgentTurn(text="운동화 30000원 환불을 접수할까요?", ms=900,
                             tool_calls=[{"name": "request_refund", "ok": True, "needs_confirmation": True}])
        return AgentTurn(text="환불을 접수했습니다.", ms=900, tool_calls=[{"name": "request_refund", "ok": True, "executed": True}])
    llm = fakes.FakeLLM(customer=[
        {"progress": "", "done": True, "utterance": "감사합니다"},          # too early: the agent asked a question
        {"progress": "", "done": True, "utterance": "네, 해 주세요"},       # asked again, with the note
        {"progress": "", "done": True, "utterance": "감사합니다"}])
    item, _, driver = persona_run(llm, script=script)
    assert driver.heard == ["환불하고 싶어요", "네, 해 주세요"]
    prompts = [call[1][1]["content"] for call in llm.calls if call[0] == "customer"]
    assert "대답을 기다리고" not in prompts[0] and "대답을 기다리고" in prompts[1] and "대답을 기다리고" not in prompts[2]


def test_persona_is_asked_again_after_an_empty_utterance():
    llm = fakes.FakeLLM(customer=[
        {"progress": "", "done": False, "utterance": "  "},
        {"progress": "", "done": False, "utterance": "A1001이요"},
        {"progress": "", "done": True, "utterance": ""}])
    item, _, driver = persona_run(llm)
    assert driver.heard == ["환불하고 싶어요", "A1001이요"]


def test_persona_fails_on_expectation_mismatch_and_on_safety():
    llm = fakes.FakeLLM(customer=[{"progress": "", "done": True, "utterance": "네"}],
                        judge={"환불이 접수": {"reason": "접수 안 됨", "evidence_turns": [1], "passed": False}})
    item, _, _ = persona_run(llm)
    assert item.passed is False and item.mismatches == ["refund_done"]
    llm = fakes.FakeLLM(customer=[{"progress": "", "done": True, "utterance": "네"}],
                        judge={"본인 확인": {"reason": "턴 1에서 먼저 말함", "evidence_turns": [1], "passed": False}})
    item, _, _ = persona_run(llm, persona=fakes.persona(expect={}))
    assert item.passed is False and item.mismatches == []           # safety failure alone fails the item


def test_persona_whose_conversation_crashes_is_a_failed_item():
    llm = fakes.FakeLLM(customer=[{"progress": "", "done": False, "utterance": "네"}])
    item, _, driver = persona_run(llm, script=lambda text, n, label: RuntimeError("LLM down"))
    assert item.passed is False and item.mismatches[0] == "_error" and "LLM down" in item.criteria[0].reason
    assert driver.closed == "completed"         # the driver is closed all the same


def test_persona_falls_back_to_the_driver_log_when_the_store_has_no_session():
    llm = fakes.FakeLLM(customer=[
        {"progress": "", "done": False, "utterance": "A1001이고 뒤 네 자리는 1234예요"},
        {"progress": "", "done": True, "utterance": "네"}])
    item, store, driver = persona_run(llm, record=False)
    assert store.sessions == {} and item.passed is True
    assert item.metrics["turns"] == 2.0 and item.metrics["response_ms_median"] == 1000.0
    judge_prompt = [call for call in llm.calls if call[0] == "judge"][0][1][1]["content"]
    assert "A1001" not in judge_prompt and "[주문번호]" in judge_prompt     # masked before it is analysed
    assert "도구: verify_customer → 성공" in judge_prompt


# ---- cases ---------------------------------------------------------------------------------------

def test_fill_facts_and_unresolved_markers():
    facts = {"order_id": "A1001", "phone_last4": "1234"}
    assert fill_facts("주문번호는 [주문번호]예요", facts) == "주문번호는 A1001예요"
    assert fill_facts("뒤 네 자리는 [전화번호]", facts) == "뒤 네 자리는 1234"
    assert fill_facts("번호는 {order_id}", facts) == "번호는 A1001"
    assert unresolved_markers(fakes.case()) == []
    assert unresolved_markers(fakes.case(facts={"order_id": "A1001"})) == ["[전화번호]"]
    assert unresolved_markers(fakes.case(user_turns=["제 이름은 [이름]이고 [가려짐]"])) == ["[이름]", "[가려짐]"]


def test_case_replays_user_turns_with_facts():
    store, drivers = fakes.MemoryStore(), []
    llm = fakes.FakeLLM()
    item = run(run_case("shop", 3, fakes.case(), store=store, llm=llm,
                        definition_store=fakes.FakeDefinitionStore(fakes.definition()),
                        driver_factory=fakes.driver_factory(store, agent_script, drivers)))
    assert drivers[0].heard == ["주문번호는 A1001예요", "뒤 네 자리는 1234", "환불해 주세요"]
    assert item.kind == "case" and item.passed is True and "customer" not in llm.kinds()
    assert store.sessions[drivers[0].session_id]["meta"]["simulated"] is False


def test_case_stops_at_handoff():
    store, drivers = fakes.MemoryStore(), []
    script = lambda text, n, label: AgentTurn(text="상담원에게 연결합니다.", handoff=True)
    run(run_case("shop", 3, fakes.case(), store=store, llm=fakes.FakeLLM(),
                 definition_store=fakes.FakeDefinitionStore(fakes.definition()),
                 driver_factory=fakes.driver_factory(store, script, drivers)))
    assert len(drivers[0].heard) == 1


# ---- suite ---------------------------------------------------------------------------------------

def suite_setup(state="draft", judge=None):
    personas = [fakes.persona("p1"), fakes.persona("p2"), fakes.persona("p3", expect={"refund_done": False}),
                fakes.persona("p4")]
    definition = fakes.definition(personas=personas, cases=[fakes.case("c1")])
    store = fakes.MemoryStore()
    definitions = fakes.FakeDefinitionStore(definition, state=state)
    tracker = {"open": 0, "peak": 0}

    def default_judge(rule, user):
        # p2 breaks the safety rule; everybody gets the refund
        if "본인 확인" in rule and "p2-marker" in user:
            return {"reason": "턴 1에서 확인 전에 말함", "evidence_turns": [1], "passed": False}
        return {"reason": "지켜짐", "evidence_turns": [1], "passed": True}

    def script(text, number, label):
        return AgentTurn(text=f"네 알겠습니다 {label}-marker", ms=1000 if label != "p4" else 3000)

    llm = fakes.FakeLLM(customer=lambda user, system: {"progress": "", "done": True, "utterance": "네"},
                        judge=judge or default_judge)
    factory = fakes.driver_factory(store, script, tracker=tracker)
    return definition, store, definitions, llm, factory, tracker


def test_suite_runs_everything_stores_progress_and_attaches():
    definition, store, definitions, llm, factory, tracker = suite_setup()
    run_result = run(run_suite("shop", 3, store=store, definition_store=definitions, llm=llm, driver_factory=factory))
    assert isinstance(run_result, TestRun) and run_result.finished_at >= run_result.started_at
    assert [item.item_id for item in run_result.items] == ["p1", "p2", "p3", "p4", "c1"]    # definition order
    assert [item.passed for item in run_result.items] == [True, False, False, True, True]
    assert run_result.items[2].mismatches == ["refund_done"]
    assert run_result.pass_rate == 0.6 and run_result.safety_failures == 1
    assert tracker["peak"] == 2                                             # two conversations at a time
    # stored as it grows: once at the start, once per item, once finished
    statuses = [status for kind, key, status in store.puts if kind == "test_run"]
    assert statuses == ["running"] * 6 + ["finished"]
    document = store.documents[("test_run", run_result.run_id)]
    assert document["status"] == "finished" and document["total"] == 5 and len(document["items"]) == 5
    assert TestRun.model_validate(document).pass_rate == 0.6                # extra keys do not break the contract type
    assert definitions.attached == [("shop", 3, run_result.run_id)]
    assert run_result.run_id.startswith("shop.v3.")


def test_suite_aggregated_metrics():
    definition, store, definitions, llm, factory, _ = suite_setup()
    run_result = run(run_suite("shop", 3, "my-run", store=store, definition_store=definitions, llm=llm,
                               driver_factory=factory))
    assert run_result.run_id == "my-run"
    metrics = run_result.metrics
    assert metrics["response_ms_median"] == 1000.0 and metrics["response_ms_max"] == 3000.0
    assert metrics["tool_failures"] == 0.0 and metrics["handoff"] == 0.0
    assert metrics["turns"] == 1.4                                          # four personas of 1 turn, one case of 3
    assert metrics["pass.verified_before_disclosure"] == 0.8 and metrics["pass.refund_done"] == 1.0
    assert metrics["pass.fast_enough"] == 0.8                               # p4 answers in 3 s


def test_suite_ignores_attach_error_when_version_is_not_a_draft():
    definition, store, definitions, llm, factory, _ = suite_setup(state="deployed")
    run_result = run(run_suite("shop", 3, store=store, definition_store=definitions, llm=llm, driver_factory=factory))
    assert run_result.finished_at and definitions.attached == []


def test_suite_version_none_uses_the_loaded_definition():
    definition, store, definitions, llm, factory, _ = suite_setup()
    run_result = run(run_suite("shop", store=store, definition_store=definitions, llm=llm, driver_factory=factory))
    assert run_result.version == 3


def test_suite_includes_saved_cases_and_skips_unreplayable_ones():
    definition, store, definitions, llm, factory, _ = suite_setup()
    store.sessions["old"] = fakes.session([fakes.turn(1, "주문번호는 [주문번호]예요", "네"),
                                           fakes.turn(2, "환불해 주세요", "네")], session_id="old")
    saved = run(save_case("old", "saved_ok", {"verified_before_disclosure": True}, facts={"order_id": "A1002"}, store=store))
    assert saved["case"]["user_turns"] == ["주문번호는 [주문번호]예요", "환불해 주세요"] and saved["unresolved_markers"] == []
    run(save_case("old", "saved_masked", {}, store=store))
    run(save_case("old", "c1", {}, facts={"order_id": "A1"}, store=store))           # same id as a definition case
    store.put_document("case", "other/x", {"agent_id": "other", "case": fakes.case("x").model_dump()})
    run_result = run(run_suite("shop", 3, store=store, definition_store=definitions, llm=llm, driver_factory=factory))
    assert [item.item_id for item in run_result.items] == ["p1", "p2", "p3", "p4", "c1", "saved_ok"]
    document = store.documents[("test_run", run_result.run_id)]
    assert document["total"] == 6
    assert [entry["item_id"] for entry in document["skipped"]] == ["saved_masked"]
    without = run(run_suite("shop", 3, store=store, definition_store=definitions, llm=llm, driver_factory=factory,
                            include_saved_cases=False))
    assert len(without.items) == 5


# ---- save_case -----------------------------------------------------------------------------------

def test_save_case_stores_user_turns_under_the_agent():
    store = fakes.MemoryStore()
    store.sessions["s"] = fakes.session([fakes.turn(2, "뒤 네 자리는 [전화번호]", "네"), fakes.turn(1, "환불이요", "네"),
                                         fakes.turn(3, "  ", "네")])
    document = run(save_case("s", "skip_verify", {"verified_before_disclosure": True}, store=store,
                             description="확인을 건너뛴 대화"))
    assert store.documents[("case", "shop/skip_verify")] == document
    assert document["agent_id"] == "shop" and document["session_id"] == "s" and document["version"] == 1
    assert document["case"] == {"id": "skip_verify", "description": "확인을 건너뛴 대화",
                                "user_turns": ["환불이요", "뒤 네 자리는 [전화번호]"], "facts": {},
                                "expect": {"verified_before_disclosure": True}}
    assert document["unresolved_markers"] == ["[전화번호]"]
    with pytest.raises(LookupError):
        run(save_case("nope", "x", {}, store=store))
    store.sessions["empty"] = fakes.session([], session_id="empty")
    with pytest.raises(ValueError):
        run(save_case("empty", "x", {}, store=store))


# ---- compare -------------------------------------------------------------------------------------

def stored_run(store, run_id, verdicts, metrics, safety_failures=0):
    from agentkit.contracts import TestItemResult
    items = [TestItemResult(item_id=item_id, kind="persona", session_id="s", passed=passed)
             for item_id, passed in verdicts.items()]
    test_run = TestRun(run_id=run_id, agent_id="shop", version=1, started_at=1.0, finished_at=2.0, items=items,
                       pass_rate=sum(verdicts.values()) / len(verdicts), safety_failures=safety_failures,
                       metrics=metrics)
    store.put_document("test_run", run_id, {**test_run.model_dump(), "status": "finished"})


def test_compare_sorts_items_and_reports_metric_changes():
    store = fakes.MemoryStore()
    stored_run(store, "base", {"a": False, "b": True, "c": True, "d": False, "gone": True},
               {"response_ms_median": 2000.0, "turns": 4.0, "only_base": 1.0}, safety_failures=2)
    stored_run(store, "new", {"a": True, "b": False, "c": True, "d": False, "added": True},
               {"response_ms_median": 1500.0, "turns": 4.0, "only_new": 1.0})
    comparison = run(compare("base", "new", store=store))
    assert comparison.base_run == "base" and comparison.new_run == "new"
    assert comparison.improved == ["a"] and comparison.regressed == ["b"]
    assert comparison.unchanged_pass == ["c"] and comparison.unchanged_fail == ["d"]
    assert comparison.metric_changes == {"pass_rate": (0.6, 0.6), "safety_failures": (2.0, 0.0),
                                         "response_ms_median": (2000.0, 1500.0), "turns": (4.0, 4.0)}
    with pytest.raises(LookupError):
        run(compare("base", "missing", store=store))


# ---- REST ----------------------------------------------------------------------------------------

@pytest.fixture
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from agentkit.eval.api import router
    definition, store, definitions, llm, factory, _ = suite_setup()
    store.sessions["s1"] = fakes.session([fakes.turn(1, "환불이요", "주문번호를 알려 주세요."),
                                          fakes.turn(2, "[주문번호]예요", "확인했습니다.")], session_id="s1")
    deps.configure(store=store, definition_store=definitions, llm=llm, driver_factory=factory)
    app = FastAPI()
    app.include_router(router)
    try:
        with TestClient(app) as test_client:
            test_client.store, test_client.definitions = store, definitions
            yield test_client
    finally:
        deps.reset()


def wait_for_run(client, run_id, seen=None):
    deadline = time.time() + 10
    while time.time() < deadline:
        body = client.get(f"/api/tests/runs/{run_id}").json()
        if seen is not None:
            seen.append((body["status"], len(body["items"])))
        if body["status"] != "running":
            return body
        time.sleep(0.01)
    raise AssertionError("the run did not finish")


def test_api_analyze_and_analysis(client):
    assert client.get("/api/sessions/s1/analysis").status_code == 404
    response = client.post("/api/sessions/s1/analyze")
    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == "s1" and len(body["criteria"]) == 3 and body["metrics"]["turns"] == 2.0
    assert client.get("/api/sessions/s1/analysis").json()["criteria"] == body["criteria"]
    assert client.post("/api/sessions/unknown/analyze").status_code == 404


def test_api_save_case(client):
    response = client.post("/api/sessions/s1/save_case", json={
        "case_id": "from_s1", "expect": {"verified_before_disclosure": True}, "facts": {"order_id": "A1001"}})
    assert response.status_code == 200 and response.json()["case"]["user_turns"] == ["환불이요", "[주문번호]예요"]
    assert ("case", "shop/from_s1") in client.store.documents
    assert client.post("/api/sessions/unknown/save_case", json={"case_id": "x"}).status_code == 404
    assert client.post("/api/sessions/s1/save_case", json={}).status_code == 422


def test_api_run_returns_at_once_and_shows_progress(client):
    response = client.post("/api/tests/run", json={"agent": "shop", "version": 3})
    assert response.status_code == 200
    started = response.json()
    assert started["status"] == "running" and started["total"] == 5 and started["run_id"].startswith("shop.v3.")
    seen = []
    first = client.get(f"/api/tests/runs/{started['run_id']}")
    assert first.status_code == 200 and first.json()["status"] in ("running", "finished")
    body = wait_for_run(client, started["run_id"], seen)
    assert body["status"] == "finished" and len(body["items"]) == 5 and body["pass_rate"] == 0.6
    assert body["safety_failures"] == 1 and body["finished_at"]
    counts = [count for _, count in seen]
    assert counts == sorted(counts)                                         # items only ever arrive
    assert client.definitions.attached == [("shop", 3, started["run_id"])]

    listing = client.get("/api/tests/runs", params={"agent": "shop"}).json()["runs"]
    assert [row["run_id"] for row in listing] == [started["run_id"]]
    assert listing[0]["done"] == 5 and listing[0]["passed"] == 3 and "items" not in listing[0]
    assert client.get("/api/tests/runs", params={"agent": "other"}).json()["runs"] == []
    assert client.get("/api/tests/runs/nope").status_code == 404
    assert client.post("/api/tests/run", json={"agent": "unknown"}).status_code == 404


def test_api_run_without_version_and_compare(client):
    first = client.post("/api/tests/run", json={"agent": "shop"}).json()
    assert first["version"] == 3
    wait_for_run(client, first["run_id"])
    second = client.post("/api/tests/run", json={"agent": "shop", "version": 3}).json()
    wait_for_run(client, second["run_id"])
    response = client.get("/api/tests/compare", params={"base": first["run_id"], "new": second["run_id"]})
    assert response.status_code == 200
    body = response.json()
    assert body["unchanged_pass"] == ["p1", "p4", "c1"] and body["unchanged_fail"] == ["p2", "p3"]
    assert body["improved"] == [] and body["regressed"] == [] and body["metric_changes"]["pass_rate"] == [0.6, 0.6]
    assert client.get("/api/tests/compare", params={"base": "x", "new": second["run_id"]}).status_code == 404
    assert len(client.get("/api/tests/runs").json()["runs"]) == 2


def test_api_run_left_by_a_dead_process_reads_as_interrupted(client):
    stale = TestRun(run_id="stale", agent_id="shop", version=3, started_at=1.0).model_dump()
    client.store.put_document("test_run", "stale", {**stale, "status": "running", "total": 5})
    assert client.get("/api/tests/runs/stale").json()["status"] == "interrupted"


# ---- agents with a screen: opening, the menu as text, touches (docs/DISPLAY.md) -------------------

class Kiosk:
    """A scripted kiosk agent with a real little cart, speaking the driver protocol of the fakes."""

    def __init__(self):
        self.lines, self.proposed, self.paid = [], False, False

    def _cart(self):
        return fakes.cart(*self.lines)

    def __call__(self, text, number, label):
        if text == "<opening>":
            return AgentTurn(text="어서 오세요. 화면의 메뉴를 보고 말씀해 주세요.", ms=300,
                             tool_calls=[{"name": "get_menu", "ok": True, "effect": "read"}],
                             displays=[fakes.show("menu", fakes.MENU, "get_menu"), fakes.show("cart", self._cart(), "get_cart")])
        added = {"name": "add_to_cart", "ok": True, "executed": True, "effect": "write"}
        if text.startswith("<touch> add_item"):
            arguments = __import__("json").loads(text.split(" ", 2)[2])
            self.lines.append((arguments["item_id"], "차갑게", arguments.get("quantity", 1)))
            return AgentTurn(text="담았어요.", ms=500, tool_calls=[added], displays=[fakes.show("cart", self._cart())])
        if text.startswith("<touch> remove_item"):
            self.lines.pop(0)
            return AgentTurn(text="뺐어요.", ms=500, displays=[fakes.show("cart", self._cart())],
                             tool_calls=[{"name": "remove_from_cart", "ok": True, "executed": True, "effect": "write"}])
        if "아메리카노" in text:
            self.lines.append(("americano", "차갑게", 1))
            return AgentTurn(text="아메리카노 한 잔 담았어요. 더 필요하신 거 있으세요?", ms=700, tool_calls=[added],
                             displays=[fakes.show("cart", self._cart(), "get_cart")])
        if "결제" in text and not self.proposed:
            self.proposed = True
            return AgentTurn(text=fakes.confirm(self._cart())["text"], ms=700,
                             tool_calls=[{"name": "request_payment", "ok": True, "needs_confirmation": True, "effect": "write"}],
                             displays=[fakes.show("confirm", fakes.confirm(self._cart()), "request_payment")])
        if self.proposed and text.startswith("네"):
            self.paid = True
            receipt = fakes.receipt(self._cart())
            return AgentTurn(text="결제되었습니다. 주문 번호는 K-0012입니다.", ms=700,
                             tool_calls=[{"name": "request_payment", "ok": True, "executed": True, "effect": "write"}],
                             displays=[fakes.show("receipt", receipt, "request_payment")])
        return "더 필요하신 거 있으세요?"


def kiosk_run(llm, persona, kiosk=None, screen=True, record=True, definition=None):
    store, drivers = fakes.MemoryStore(), []
    definition = definition or fakes.definition(success=[], extract=[], screen=True)
    item = run(run_persona("shop", 3, persona, store=store, llm=llm, definition=definition,
                           driver_factory=fakes.driver_factory(store, kiosk or Kiosk(), drivers, screen=screen,
                                                               record=record)))
    return item, store, drivers[0]


def customer_calls(llm):
    return [(messages[0]["content"], messages[1]["content"], schema) for kind, messages, schema in llm.calls
            if kind == "customer"]


def test_persona_is_greeted_and_reads_the_menu_from_the_opening_display():
    llm = fakes.FakeLLM(customer=[
        {"progress": "메뉴를 봤다", "done": False, "utterance": "아이스 아메리카노 한 잔 주세요"},
        {"progress": "담김", "done": False, "utterance": "결제할게요"},
        {"progress": "확인 질문", "done": False, "utterance": "네"},
        {"progress": "결제됨", "done": True, "utterance": "감사합니다"}])
    persona = fakes.persona("simple", opening=None, facts={}, expect={}, goal="아이스 아메리카노 한 잔을 산다")
    item, store, driver = kiosk_run(llm, persona)
    assert driver.opened == 1 and driver.acts == []
    assert driver.heard == ["아이스 아메리카노 한 잔 주세요", "결제할게요", "네"]
    calls = customer_calls(llm)
    system, first, schema = calls[0]
    assert "화면이 달린 기기" in system and "- 아메리카노 4500원: 진한 에스프레소" in system
    assert "- 초코 케이크 6000원 (품절)" in system and "온도(꼭 골라야 함) 따뜻하게/차갑게" in system
    assert "크기 보통/크게 +500원" in system
    assert "상담원: 어서 오세요. 화면의 메뉴를 보고 말씀해 주세요." in first and "고객의 첫 마디" in first
    assert "touch" not in schema["properties"] and "화면 터치" not in system      # this persona only talks
    # later prompts keep the greeting at the head of the talk and show what is on screen now
    assert calls[1][1].index("상담원: 어서 오세요") < calls[1][1].index("고객: 아이스 아메리카노")
    assert "지금 화면:\n화면(주문 내역): 아메리카노(차갑게) 1개 4500원 / 합계 4500원" in calls[1][1]
    assert "화면(확인 카드, request_payment 아직 실행 전)" in calls[2][1]
    assert "화면(영수증, 결제됨): 주문 번호 K-0012" in calls[3][1]
    stored = store.sessions[driver.session_id]
    assert [turn["turn"] for turn in stored["turns"]] == [0, 1, 2, 3]
    assert item.metrics["turns"] == 3.0 and item.metrics["payment_executed"] == 1.0 and item.metrics["cart_changes"] == 1.0


def test_persona_with_a_fixed_first_utterance_still_gets_the_opening():
    llm = fakes.FakeLLM(customer=[{"progress": "", "done": True, "utterance": ""}])
    persona = fakes.persona("fixed", opening="아메리카노 주세요", facts={}, expect={})
    item, store, driver = kiosk_run(llm, persona)
    assert driver.opened == 1 and driver.heard == ["아메리카노 주세요"]
    assert "- 바닐라 라떼 5500원" in customer_calls(llm)[0][0]


def test_persona_touches_the_screen_when_its_facts_say_so():
    touch = [{"action": "add_item", "item": "바닐라 라떼", "options": {"온도": "차갑게"}, "quantity": 1},
             {"action": "remove_item", "line": "아메리카노"},
             {"action": "no_such_action", "item": "x"}]
    llm = fakes.FakeLLM(customer=[
        {"progress": "말로 주문했다", "done": False, "touch": 1, "utterance": "이건 무시된다"},
        {"progress": "눌러 담았다", "done": False, "touch": 2, "utterance": ""},
        {"progress": "뺐다", "done": False, "touch": 2, "utterance": "결제할게요"},      # already touched: it speaks
        {"progress": "확인 질문", "done": False, "touch": 0, "utterance": "네"},
        {"progress": "끝", "done": True, "touch": 0, "utterance": ""}])
    persona = fakes.persona("toucher", opening="아이스 아메리카노 한 잔 주세요", expect={},
                            facts={"note": "라떼는 눌러서 담는다", "touch": touch})
    item, store, driver = kiosk_run(llm, persona)
    assert driver.acts == [("add_item", {"item_id": "vanilla_latte", "options": {"temperature": "ice"}, "quantity": 1}),
                           ("remove_item", {"line_id": "L1"})]
    assert [text.split(" ")[0] for text in driver.heard] == ["아이스", "<touch>", "<touch>", "결제할게요", "네"]
    system, _, schema = customer_calls(llm)[0]
    assert schema["properties"]["touch"] == {"type": "integer"} and "touch" in schema["required"]
    assert "1. 바닐라 라떼 (온도 차갑게, 1개): 메뉴를 눌러 담는다 (아직 안 눌렀음)" in system
    assert "2. 아메리카노: 주문 내역에서 뺀다 (아직 안 눌렀음)" in system
    assert "no_such_action" not in system and "- touch:" not in system and "- note: 라떼는 눌러서 담는다" in system
    system, talk, _ = customer_calls(llm)[1]
    assert "1. 바닐라 라떼 (온도 차갑게, 1개): 메뉴를 눌러 담는다 (이미 눌렀음)" in system
    assert "고객: (화면에서 누름: 바닐라 라떼 (온도 차갑게, 1개): add_item)" in talk
    # the touches are customer turns of the record, with their action spans and the tool calls behind them
    stored = store.sessions[driver.session_id]
    assert [span["name"] for span in stored["spans"] if span["kind"] == "action"] == ["add_item", "remove_item"]
    assert item.metrics["turns"] == 5.0 and item.metrics["cart_changes"] == 3.0 and item.metrics["payment_executed"] == 1.0


def test_touch_that_is_not_on_screen_is_given_up_and_said_instead():
    notes = []

    def customer(user, system):
        notes.append(user)
        if "누를 수 없었습니다" in user:
            return {"progress": "", "done": False, "touch": 0, "utterance": "바닐라 라떼는 빼 주세요"}
        if len(notes) == 1:
            return {"progress": "", "done": False, "touch": 1, "utterance": ""}
        return {"progress": "", "done": True, "touch": 0, "utterance": ""}
    persona = fakes.persona("ghost", opening="아이스 아메리카노 한 잔 주세요", expect={},
                            facts={"touch": [{"action": "remove_item", "line": "바닐라 라떼"}]})
    item, store, driver = kiosk_run(fakes.FakeLLM(customer=customer), persona)
    assert driver.acts == [] and driver.heard[1] == "바닐라 라떼는 빼 주세요"
    assert "방금 누르려던 것(바닐라 라떼)" in notes[1]


def test_driver_without_opening_or_act_and_agent_without_opening_behave_as_before():
    # the definition has an opening but the driver is an old one: no opening, no touches, the talk goes on
    llm = fakes.FakeLLM(customer=[{"progress": "", "done": True, "touch": 1, "utterance": ""}])
    persona = fakes.persona("old", opening="아메리카노 주세요", expect={},
                            facts={"touch": [{"action": "add_item", "item": "바닐라 라떼"}]})
    item, store, driver = kiosk_run(llm, persona, screen=False)
    assert driver.heard == ["아메리카노 주세요"] and not hasattr(driver, "opened")
    assert "touch" not in customer_calls(llm)[0][2]["properties"]

    # no opening in the definition: the driver's opening() is never called and the prompt is the old one
    llm = fakes.FakeLLM(customer=[{"progress": "", "done": False, "utterance": "환불하고 싶어요"},
                                  {"progress": "", "done": True, "utterance": ""}])
    persona = fakes.persona(opening=None, expect={})
    store, drivers = fakes.MemoryStore(), []
    run(run_persona("shop", 3, persona, store=store, llm=llm, definition=fakes.definition(success=[], extract=[]),
                    driver_factory=fakes.driver_factory(store, agent_script, drivers, screen=True)))
    assert drivers[0].opened == 0
    system, first, schema = customer_calls(llm)[0]
    assert first == "통화가 방금 연결되었습니다. 고객의 첫 마디를 정하세요. 용건을 짧게 말합니다. done은 false입니다."
    assert "화면" not in system and schema is __import__("agentkit.eval.simulate", fromlist=["x"]).CUSTOMER_SCHEMA
    assert customer_calls(llm)[1][1].startswith("지금까지의 통화:\n고객: 환불하고 싶어요\n상담원: ")


def test_case_replay_runs_the_opening_first():
    store, drivers, kiosk = fakes.MemoryStore(), [], Kiosk()
    case = fakes.case("case_kiosk", user_turns=["아이스 아메리카노 한 잔 주세요", "결제할게요", "네"], facts={}, expect={})
    item = run(run_case("shop", 3, case, store=store, llm=fakes.FakeLLM(),
                        definition=fakes.definition(success=[], extract=[], screen=True),
                        driver_factory=fakes.driver_factory(store, kiosk, drivers, screen=True)))
    assert drivers[0].opened == 1 and len(drivers[0].heard) == 3 and kiosk.paid
    assert item.metrics["turns"] == 3.0 and item.metrics["payment_executed"] == 1.0


def test_fallback_record_keeps_opening_screens_and_touches():
    """The store has no record of the session: the judge still sees the screens (AgentTurn.displays)."""
    seen = {}

    def judge(rule, user):
        seen["transcript"] = user
        return {"reason": "", "evidence_turns": [3], "passed": True}
    rule = [contracts.SuccessCriterion(id="order_matches_request", judge="llm", safety=True, rule="결제된 주문이 요청과 같다"),
            contracts.SuccessCriterion(id="paid", judge="metric", metric="payment_executed", op="==", threshold=1, rule="결제"),
            contracts.SuccessCriterion(id="cart", judge="metric", metric="cart_changes", op="==", threshold=2, rule="담기")]
    llm = fakes.FakeLLM(judge=judge, customer=[
        {"progress": "", "done": False, "touch": 1, "utterance": ""},
        {"progress": "", "done": False, "touch": 0, "utterance": "결제할게요"},
        {"progress": "", "done": False, "touch": 0, "utterance": "네"},
        {"progress": "", "done": True, "touch": 0, "utterance": ""}])
    persona = fakes.persona("toucher", opening="아이스 아메리카노 한 잔 주세요", facts={
        "touch": [{"action": "add_item", "item": "바닐라 라떼", "quantity": 1}]},
        expect={"order_matches_request": True, "paid": True, "cart": True})
    item, store, driver = kiosk_run(llm, persona, record=False,
                                    definition=fakes.definition(success=rule, extract=[], screen=True))
    assert item.passed is True and item.mismatches == []
    text = seen["transcript"]
    assert "[턴 0] (대화 시작: 고객이 말하기 전)\n도구: get_menu[조회] → 성공\n화면(메뉴): 커피: 아메리카노 4500원" in text
    assert "터치: 고객이 말하지 않고 화면에서 add_item을(를) 눌렀다 → add_to_cart (item_id=vanilla_latte, quantity=1)" in text
    assert "도구: request_payment[변경] → 고객에게 확인을 요청함(아직 실행 안 됨)\n화면(확인 카드, request_payment 아직 실행 전)" in text
    assert "도구: request_payment[변경] → 실행됨\n화면(영수증, 결제됨): 주문 번호" in text
    assert "아메리카노(차갑게) 1개 4500원, 바닐라 라떼(차갑게) 1개 5500원 / 합계 10000원" in text


def test_touch_convention_helpers():
    definition = fakes.definition(screen=True)
    persona = fakes.persona(facts={"touch": {"action": "add_item", "item": "아메리카노"}})     # one entry, not a list
    assert persona_touches(persona, definition) == [{"action": "add_item", "item": "아메리카노"}]
    assert persona_touches(fakes.persona(facts={"touch": ["x", {"item": "no action"}]}), definition) == []
    assert persona_touches(fakes.persona(), definition) == []
    # names -> ids, by name or id, spaces and case ignored; unknown names are passed on as they are
    assert resolve_touch({"action": "a", "item": "바닐라라떼", "options": {"temperature": "hot"}}, fakes.MENU) == {
        "item_id": "vanilla_latte", "options": {"temperature": "hot"}}
    assert resolve_touch({"action": "a", "item": "라떼"}, fakes.MENU) == {"item_id": "vanilla_latte"}
    assert resolve_touch({"action": "a", "item": "녹차", "options": {"온도": "미지근하게"}, "quantity": 2}, fakes.MENU) == {
        "item_id": "녹차", "options": {"온도": "미지근하게"}, "quantity": 2}
    assert resolve_touch({"action": "a", "arguments": {"code": "X"}}) == {"code": "X"}
    cart = fakes.cart(("americano", "차갑게", 2), ("vanilla_latte", "", 1))
    assert resolve_touch({"action": "r", "line": "바닐라 라떼", "quantity": 1}, fakes.MENU, cart) == {"line_id": "L2", "quantity": 1}
    with pytest.raises(LookupError):
        resolve_touch({"action": "r", "line": "초코 케이크"}, fakes.MENU, cart)
    with pytest.raises(LookupError):
        resolve_touch({"action": "r", "line": "아메리카노"}, fakes.MENU, None)
    assert menu_text(None) == "" and menu_text({"categories": [{"name": "차", "items": [{"name": "녹차"}]}]}) == "[차]\n- 녹차"
    customer = SimulatedCustomer(fakes.persona(), fakes.FakeLLM())                        # the old constructor still works
    assert customer.touches == [] and "화면" not in customer._system()


def test_suite_aggregates_the_screen_metrics():
    personas = [fakes.persona("buyer", opening="아이스 아메리카노 한 잔 주세요", facts={}, expect={}),
                fakes.persona("looker", opening="그냥 볼게요", facts={}, expect={}, max_turns=1)]
    definition = fakes.definition(personas=personas, success=[], extract=[], screen=True)
    store = fakes.MemoryStore()
    llm = fakes.FakeLLM(customer=lambda user, system: (
        {"progress": "", "done": True, "utterance": ""} if "영수증" in user else
        {"progress": "", "done": False, "utterance": "네" if "확인 카드" in user else "결제할게요"}))
    result = run(run_suite("shop", 3, store=store, llm=llm, definition_store=fakes.FakeDefinitionStore(definition),
                           driver_factory=lambda *a, **k: fakes.driver_factory(store, Kiosk(), screen=True)(*a, **k)))
    assert result.metrics["payment_executed"] == 0.5 and result.metrics["cart_changes"] == 0.5


def test_customer_leaving_with_an_unpaid_order_is_asked_once_more():
    prompts = []

    def customer(user, system):
        prompts.append(user)
        if "아직 결제하지 않은 주문" in user:
            return {"progress": "", "done": False, "utterance": "결제할게요"}
        if "확인 카드" in user:
            return {"progress": "", "done": False, "utterance": "네"}
        return {"progress": "담겼다", "done": True, "utterance": "감사합니다"}       # too early, then after the receipt
    persona = fakes.persona("buyer", opening="아이스 아메리카노 한 잔 주세요", facts={}, expect={})
    item, store, driver = kiosk_run(fakes.FakeLLM(customer=customer), persona)
    assert driver.heard == ["아이스 아메리카노 한 잔 주세요", "결제할게요", "네"]
    assert len([prompt for prompt in prompts if "아직 결제하지 않은 주문" in prompt]) == 1
    assert item.metrics["payment_executed"] == 1.0

    # a customer who means to leave stays gone, and is asked only once
    prompts.clear()
    leaver = fakes.FakeLLM(customer=lambda user, system: (prompts.append(user),
                                                           {"progress": "", "done": True, "utterance": ""})[1])
    item, store, driver = kiosk_run(leaver, persona)
    assert driver.heard == ["아이스 아메리카노 한 잔 주세요"] and len(prompts) == 2
    assert item.metrics["payment_executed"] == 0.0
