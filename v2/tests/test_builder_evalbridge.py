"""EvalBridge against the real evaluation module and a real trace store in a temporary folder.
No LLM: run_suite is replaced by a function that writes a finished run."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

import pytest

import test_builder_support as support
from agentkit.builder import Operations, Turn
from agentkit.builder.evalbridge import EvalBridge

pytest.importorskip("agentkit.eval")
trace = pytest.importorskip("agentkit.trace")


def run_document(run_id, version, results, started_at):
    items = [{"item_id": item, "kind": "persona", "session_id": "s-" + item, "passed": passed, "criteria": [],
              "mismatches": [] if passed else ["no_handoff"], "metrics": {}} for item, passed in results.items()]
    return {"run_id": run_id, "agent_id": "shop-support", "version": version, "started_at": started_at,
            "finished_at": started_at + 1, "items": items, "pass_rate": sum(results.values()) / len(results),
            "safety_failures": 0, "metrics": {"turns": 4.0 + version}, "status": "finished", "total": len(items),
            "skipped": []}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = support.copy_agents(tmp_path)
    data = trace.TraceStore(tmp_path / "data")
    bridge = EvalBridge(store, trace_store=data)
    ops = Operations(store, bridge, lambda: dict(support.CATALOG), lambda: data)
    return store, data, bridge, ops


async def test_compare_and_check_read_the_stored_runs(setup):
    store, data, bridge, ops = setup
    now = time.time()
    data.put_document("test_run", "base", run_document("base", 1, {"a": True, "b": False, "c": True}, now - 60))
    turn = Turn()
    await ops.execute(turn, "add_handoff_rule", {"agent_id": "shop-support", "id": "large_refund", "when": "환불 금액이 50000원을 넘는다"})
    data.put_document("test_run", "new", run_document("new", 2, {"a": True, "b": True, "c": False}, now))
    assert [run["run_id"] for run in await bridge.list_runs("shop-support")] == ["new", "base"]
    ok, text = await ops.execute(turn, "check_tests", {"agent_id": "shop-support"})        # newest run of the agent
    assert ok and "실행 new (shop-support v2): 끝났습니다. 통과 2/3" in text and "실패 c" in text
    ok, text = await ops.execute(turn, "compare_runs", {"new_run_id": "new"})               # base found by version
    assert ok and "기준 base → 새 new" in text, text
    assert "좋아진 항목: b" in text and "나빠진 항목: c" in text and "그대로 통과 1개" in text and "turns 5 → 6" in text
    ok, text = await ops.execute(turn, "compare_runs", {"new_run_id": "new", "base_run_id": "missing"})
    assert not ok and "찾을 수 없습니다" in text


async def test_start_runs_the_suite_in_the_background_and_attaches_it(setup, monkeypatch):
    store, data, bridge, ops = setup
    import agentkit.eval as evaluation
    seen = {}

    async def fake_run_suite(agent_id, version, run_id, *, store=None, definition_store=None, **_):
        seen.update(agent_id=agent_id, version=version, definition_store=definition_store, store=store)
        store.put_document("test_run", run_id, run_document(run_id, version, {"a": True}, time.time()))
        definition_store.attach_test_run(agent_id, version, run_id)

    monkeypatch.setattr(evaluation, "run_suite", fake_run_suite)
    turn = Turn()
    await ops.execute(turn, "set_field", {"agent_id": "shop-support", "field": "reply_length", "value": "normal"})
    ok, text = await ops.execute(turn, "run_tests", {"agent_id": "shop-support"})
    assert ok and "시험을 시작했습니다" in text
    run_id = turn.runs[0]["run_id"]
    assert run_id.startswith("shop-support.v2.") and turn.runs[0]["total"] == 11
    first = await bridge.load(run_id)                    # readable at once, before the task has run
    assert first["status"] == "running" and first["items"] == []
    await bridge.wait(run_id, timeout=5)
    assert seen["definition_store"] is store and seen["store"] is data and seen["version"] == 2
    assert (await bridge.load(run_id))["status"] == "finished"
    assert store.version_info("shop-support", 2).state == "tested"
    ok, text = await ops.execute(turn, "submit_for_approval", {"agent_id": "shop-support"})
    assert ok and store.version_info("shop-support", 2).state == "pending_approval"


async def test_a_failed_suite_is_marked_as_error(setup, monkeypatch):
    store, data, bridge, ops = setup
    import agentkit.eval as evaluation

    async def broken(*args, **kwargs):
        raise RuntimeError("model server down")

    monkeypatch.setattr(evaluation, "run_suite", broken)
    turn = Turn()
    await ops.execute(turn, "run_tests", {"agent_id": "shop-support", "version": 1})
    run_id = turn.runs[0]["run_id"]
    await bridge.wait(run_id, timeout=5)
    ok, text = await ops.execute(turn, "check_tests", {})
    assert ok and "끝까지 돌지 못했습니다 (error)" in text and "model server down" in text
    assert store.version_info("shop-support", 1).state == "deployed"
