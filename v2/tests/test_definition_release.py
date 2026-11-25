"""Versions, drafts, diff, and every release rule (R-DEF-3, R-REL-1~6). Works on a temporary copy."""
import json
import shutil
from collections import Counter
from pathlib import Path

import pytest

from agentkit.contracts import AgentDefinition, Deployment, ExtractField, HandoffRule, ReleaseError, VersionInfo
from agentkit.definition import DefinitionError, DefinitionStore

from agents_baseline import copy_baseline, snapshot
REAL_AGENTS = Path(__file__).resolve().parent.parent / "agents"
BEFORE = snapshot()
SHOP = "shop-support"


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "agents"
    copy_baseline(root)
    return DefinitionStore(root)


def approved(store, agent=SHOP, based_on=None, approver="김담당"):
    """A new version taken through test, submit and approval."""
    n = store.create_draft(agent, based_on, "builder", "시험용")
    store.attach_test_run(agent, n, f"run-{n}")
    store.submit(agent, n)
    store.approve(agent, n, approver)
    return n


def state(store, n, agent=SHOP):
    return {v.version: v.state for v in store.list_versions(agent)}[n]


# ------------------------------------------------------------------ drafts and immutability

def test_create_draft_copies_the_base(store):
    n = store.create_draft(SHOP, 1, "builder", "말투 수정")
    assert n == 2
    info = store.list_versions(SHOP)[-1]
    assert isinstance(info, VersionInfo)
    assert (info.version, info.state, info.based_on, info.created_by, info.note) == (2, "draft", 1, "builder", "말투 수정")
    assert info.changed_fields == [] and info.safety_change is False and info.created_at > 0
    draft, base = store.load(SHOP, 2), store.load(SHOP, 1)
    assert draft.version == 2 and draft.model_dump(exclude={"version"}) == base.model_dump(exclude={"version"})
    assert store.diff(SHOP, 1, 2) == {}
    assert store.load(SHOP).version == 1                      # a draft does not take conversations


def test_create_draft_default_base(store):
    n = store.create_draft(SHOP, None, "kim")
    assert n == 2 and store.list_versions(SHOP)[-1].based_on == 1          # the stable version
    n = store.create_draft("clinic-intake", None, "kim")      # nothing deployed: the newest version
    assert n == 2 and store.list_versions("clinic-intake")[-1].based_on == 1
    with pytest.raises(DefinitionError):
        store.create_draft(SHOP, 42, "kim")


def test_save_draft_records_ordinary_change(store):
    n = store.create_draft(SHOP, 1, "builder")
    d = store.load(SHOP, n)
    d.persona += "\n더 짧게 말합니다."
    d.reply_length = "normal"
    info = store.save_draft(d, note="말투를 더 짧게")
    assert set(info.changed_fields) == {"persona", "reply_length"}
    assert info.safety_change is False and info.note == "말투를 더 짧게" and info.state == "draft"
    assert store.load(SHOP, n).reply_length == "normal"
    assert store.list_versions(SHOP)[-1].changed_fields == info.changed_fields
    diff = store.diff(SHOP, 1, n)
    assert set(diff) == {"persona", "reply_length"}
    assert diff["reply_length"] == {"before": "short", "after": "normal"}


@pytest.mark.parametrize("field, change", [
    ("tools", lambda d: d.tools.remove("track_delivery")),
    ("handoff", lambda d: d.handoff.__setitem__(0, HandoffRule(id="large_refund", when="환불 금액이 50000원을 넘는다"))),
    ("prohibitions", lambda d: d.prohibitions.pop()),
    ("guards", lambda d: d.guards.pop()),
    ("evaluation.success", lambda d: setattr(d.evaluation.success[0], "safety", False)),
    ("evaluation.success", lambda d: setattr(d.evaluation.success[1], "rule", "동의를 받았다")),
    ("evaluation.extract", lambda d: d.evaluation.extract.append(ExtractField(name="customer_name", pii=True))),
])
def test_save_draft_flags_safety_change(store, field, change):
    n = store.create_draft(SHOP, 1, "builder")
    d = store.load(SHOP, n)
    change(d)
    info = store.save_draft(d)
    assert info.safety_change is True
    assert field in info.changed_fields


@pytest.mark.parametrize("change", [
    lambda d: d.flow.pop(),
    lambda d: setattr(d.evaluation.success[-1], "threshold", 20),            # a criterion that is not a safety one
    lambda d: d.evaluation.extract.append(ExtractField(name="mood")),
    lambda d: d.tests.personas.pop(),
    lambda d: setattr(d.voice, "tts_voice", "M1"),
])
def test_other_changes_are_not_safety_changes(store, change):
    n = store.create_draft(SHOP, 1, "builder")
    d = store.load(SHOP, n)
    change(d)
    info = store.save_draft(d)
    assert info.safety_change is False and info.changed_fields


def test_screen_parts_are_ordinary_changes_and_show_in_the_diff(store):
    from agentkit.contracts import ClientAction, Opening, OpeningCall, ScreenSettings
    n = store.create_draft(SHOP, 1, "builder", "화면 추가")
    d = store.load(SHOP, n)
    d.opening = Opening(say="어서 오세요.", tools=[OpeningCall(name="get_order")])
    d.screen = ScreenSettings(panels=["cart"])
    d.actions = [ClientAction(name="call_person", tool="handoff_to_human", say="(화면에서 직원을 불렀습니다)")]
    info = store.save_draft(d)
    assert set(info.changed_fields) == {"opening", "screen", "actions"} and info.safety_change is False
    assert store.load(SHOP, n) == d
    diff = store.diff(SHOP, 1, n)
    assert set(diff) == {"opening", "screen", "actions"}
    assert diff["opening"]["before"] == {"say": "", "tools": [], "brief": ""}
    assert diff["opening"]["after"]["tools"] == [{"name": "get_order", "arguments": {}}]
    assert diff["actions"]["before"] == [] and diff["actions"]["after"][0]["tool"] == "handoff_to_human"
    # an action on a tool the agent does not have is refused and nothing is written
    d.actions = [ClientAction(name="pay", tool="request_payment")]
    with pytest.raises(DefinitionError) as caught:
        store.save_draft(d)
    assert caught.value.field == "actions[0].tool"
    assert store.load(SHOP, n).actions[0].name == "call_person"
    # removing the tool an action needs is refused the same way
    d = store.load(SHOP, n)
    d.tools.remove("handoff_to_human")
    with pytest.raises(DefinitionError):
        store.save_draft(d)
    d = store.load(SHOP, n)
    d.opening, d.screen, d.actions = Opening(), ScreenSettings(), []
    assert store.save_draft(d).changed_fields == []


def test_safety_flag_clears_when_the_change_is_undone(store):
    n = store.create_draft(SHOP, 1, "builder")
    d = store.load(SHOP, n)
    removed = d.prohibitions.pop()
    assert store.save_draft(d).safety_change is True
    d.prohibitions.append(removed)
    info = store.save_draft(d)
    assert info.safety_change is False and info.changed_fields == []


def test_save_draft_rejects_invalid_definition(store):
    n = store.create_draft(SHOP, 1, "builder")
    d = store.load(SHOP, n)
    d.guards[0].pattern = "(깨진"
    with pytest.raises(DefinitionError) as caught:
        store.save_draft(d)
    assert caught.value.file == "guards.yaml" and caught.value.field == "guards[0].pattern"
    assert store.load(SHOP, n).guards[0].pattern != "(깨진"          # nothing was written


@pytest.mark.parametrize("target", ["deployed", "tested", "pending_approval", "approved", "retired"])
def test_only_a_draft_can_be_edited(store, target):
    """R-DEF-3 / R-BLD-3: a version that left the draft state never changes."""
    if target == "deployed":
        n = 1
    else:
        n = store.create_draft(SHOP, 1, "builder")
        store.attach_test_run(SHOP, n, "run-x")
        if target in ("pending_approval", "approved", "retired"):
            store.submit(SHOP, n)
        if target in ("approved", "retired"):
            store.approve(SHOP, n, "김담당")
        if target == "retired":
            store.deploy(SHOP, n)
            store.rollback(SHOP)
    assert state(store, n) == target
    before = store.load(SHOP, n)
    d = before.model_copy(deep=True)
    d.persona = "전혀 다른 사람"
    with pytest.raises(ReleaseError):
        store.save_draft(d)
    assert store.load(SHOP, n) == before


def test_create_agent(store):
    d = AgentDefinition(id="new-agent", version=7, name="새 에이전트", persona="친절한 안내원",
                        prohibitions=["거짓말을 하지 않는다"])
    info = store.create_agent(d, "builder")
    assert (info.version, info.state, info.based_on, info.created_by) == (1, "draft", None, "builder")
    assert info.safety_change is True and "prohibitions" in info.changed_fields and "persona" in info.changed_fields
    assert "new-agent" in store.list_agents()
    assert store.load("new-agent", 1).version == 1
    assert store.deployment("new-agent") == Deployment(agent_id="new-agent")
    with pytest.raises(DefinitionError):
        store.create_agent(d, "builder")                       # already exists
    with pytest.raises(DefinitionError):
        store.create_agent(AgentDefinition(id="Bad Id!", name="x", persona="y"), "builder")
    plain = store.create_agent(AgentDefinition(id="plain", name="x", persona="y"), "kim")
    assert plain.safety_change is False


# ------------------------------------------------------------------ release rules, one by one

def test_attach_test_run_moves_draft_to_tested(store):
    n = store.create_draft(SHOP, 1, "builder")
    info = store.attach_test_run(SHOP, n, "run-1")
    assert (info.state, info.test_run_id) == ("tested", "run-1")
    assert store.attach_test_run(SHOP, n, "run-2").test_run_id == "run-2"      # a newer run on a tested version
    with pytest.raises(ReleaseError):
        store.attach_test_run(SHOP, n, "")


def test_attach_test_run_refused_after_tested(store):
    with pytest.raises(ReleaseError):
        store.attach_test_run(SHOP, 1, "run-9")                # deployed
    assert store.list_versions(SHOP)[0].test_run_id is None
    n = store.create_draft(SHOP, 1, "builder")
    store.attach_test_run(SHOP, n, "run-1")
    store.submit(SHOP, n)
    with pytest.raises(ReleaseError):
        store.attach_test_run(SHOP, n, "run-2")                # pending approval


def test_submit_without_test_run_is_refused(store):
    """R-REL-2"""
    n = store.create_draft(SHOP, 1, "builder")
    with pytest.raises(ReleaseError):
        store.submit(SHOP, n)
    assert state(store, n) == "draft"


def test_submit_moves_tested_to_pending(store):
    n = store.create_draft(SHOP, 1, "builder")
    store.attach_test_run(SHOP, n, "run-1")
    assert store.submit(SHOP, n).state == "pending_approval"
    with pytest.raises(ReleaseError):
        store.submit(SHOP, n)                                  # already submitted
    with pytest.raises(ReleaseError):
        store.submit(SHOP, 1)                                  # deployed


def test_approve_needs_a_name(store):
    """R-REL-3"""
    n = store.create_draft(SHOP, 1, "builder")
    store.attach_test_run(SHOP, n, "run-1")
    store.submit(SHOP, n)
    for nobody in ("", "   ", None):
        with pytest.raises(ReleaseError):
            store.approve(SHOP, n, nobody)
    assert state(store, n) == "pending_approval"


def test_builder_cannot_approve(store):
    """R-REL-3, D3"""
    n = store.create_draft(SHOP, 1, "builder")
    store.attach_test_run(SHOP, n, "run-1")
    store.submit(SHOP, n)
    for name in ("builder", "Builder", " BUILDER "):
        with pytest.raises(ReleaseError):
            store.approve(SHOP, n, name)
    assert state(store, n) == "pending_approval"


def test_approve_records_who_and_when(store):
    n = store.create_draft(SHOP, 1, "builder")
    store.attach_test_run(SHOP, n, "run-1")
    store.submit(SHOP, n)
    info = store.approve(SHOP, n, "김담당")
    assert (info.state, info.approved_by) == ("approved", "김담당") and info.approved_at > 0
    saved = json.loads((store.root / SHOP / "versions" / str(n) / "meta.json").read_text(encoding="utf-8"))
    assert saved["approved_by"] == "김담당" and saved["state"] == "approved"


@pytest.mark.parametrize("stage", ["draft", "tested"])
def test_approve_only_from_pending(store, stage):
    n = store.create_draft(SHOP, 1, "builder")
    if stage == "tested":
        store.attach_test_run(SHOP, n, "run-1")
    with pytest.raises(ReleaseError):
        store.approve(SHOP, n, "김담당")
    assert state(store, n) == stage


@pytest.mark.parametrize("stage", ["draft", "tested", "pending_approval"])
def test_deploy_of_unapproved_version_is_refused(store, stage):
    """R-REL-4"""
    n = store.create_draft(SHOP, 1, "builder")
    if stage != "draft":
        store.attach_test_run(SHOP, n, "run-1")
    if stage == "pending_approval":
        store.submit(SHOP, n)
    for percent in (100, 10):
        with pytest.raises(ReleaseError):
            store.deploy(SHOP, n, percent)
    assert state(store, n) == stage
    assert store.deployment(SHOP).stable == 1 and store.deployment(SHOP).canary is None


def test_deploy_full_makes_stable_and_retires_the_old_one(store):
    n = approved(store)
    deployment = store.deploy(SHOP, n)
    assert isinstance(deployment, Deployment)
    assert (deployment.stable, deployment.previous_stable, deployment.canary, deployment.canary_percent) == (n, 1, None, 0)
    assert deployment.updated_at > 0
    assert state(store, n) == "deployed" and state(store, 1) == "retired"
    assert store.deployment(SHOP) == deployment
    assert store.load(SHOP).version == n
    assert store.resolve(SHOP, "s1") == n


def test_deploy_percent_makes_canary(store):
    """R-REL-5"""
    n = approved(store)
    deployment = store.deploy(SHOP, n, 20)
    assert (deployment.stable, deployment.canary, deployment.canary_percent) == (1, n, 20)
    assert state(store, n) == "deployed" and state(store, 1) == "deployed"
    assert store.load(SHOP).version == 1                       # version None stays the stable one
    assert store.deploy(SHOP, n, 50).canary_percent == 50      # an already deployed version: widen the canary
    promoted = store.deploy(SHOP, n, 100)                      # promote
    assert (promoted.stable, promoted.canary, promoted.canary_percent, promoted.previous_stable) == (n, None, 0, 1)
    assert state(store, 1) == "retired"


def test_deploy_percent_must_be_1_to_100(store):
    n = approved(store)
    for percent in (0, -5, 101, 12.5, "50", True):
        with pytest.raises(ReleaseError):
            store.deploy(SHOP, n, percent)
    assert state(store, n) == "approved"


def test_canary_needs_a_stable_version(store):
    agent = "clinic-intake"
    store.attach_test_run(agent, 1, "run-1")
    store.submit(agent, 1)
    store.approve(agent, 1, "김담당")
    with pytest.raises(ReleaseError):
        store.deploy(agent, 1, 10)
    with pytest.raises(ReleaseError):
        store.deploy(SHOP, 1, 10)                              # the stable version cannot be its own canary
    assert store.deploy(agent, 1).stable == 1


def test_second_canary_replaces_the_first(store):
    a, b = approved(store), approved(store)
    store.deploy(SHOP, a, 10)
    deployment = store.deploy(SHOP, b, 30)
    assert (deployment.stable, deployment.canary, deployment.canary_percent) == (1, b, 30)
    assert state(store, a) == "retired" and state(store, b) == "deployed"


def test_retired_version_cannot_be_deployed_again(store):
    n = approved(store)
    store.deploy(SHOP, n)
    with pytest.raises(ReleaseError):
        store.deploy(SHOP, 1)
    assert store.deployment(SHOP).stable == n


def test_rollback_returns_to_previous_stable(store):
    """R-REL-6"""
    n = approved(store)
    store.deploy(SHOP, n)
    deployment = store.rollback(SHOP)
    assert (deployment.stable, deployment.previous_stable, deployment.canary) == (1, None, None)
    assert state(store, 1) == "deployed" and state(store, n) == "retired"
    assert store.load(SHOP).version == 1 and store.resolve(SHOP, "s1") == 1
    with pytest.raises(ReleaseError):
        store.rollback(SHOP)                                   # nothing further back


def test_rollback_without_previous_is_refused(store):
    with pytest.raises(ReleaseError):
        store.rollback(SHOP)
    with pytest.raises(ReleaseError):
        store.rollback("clinic-intake")
    assert store.deployment(SHOP).stable == 1


def test_rollback_withdraws_a_running_canary(store):
    n = approved(store)
    store.deploy(SHOP, n, 25)
    deployment = store.rollback(SHOP)
    assert (deployment.stable, deployment.canary, deployment.canary_percent) == (1, None, 0)
    assert state(store, n) == "retired" and state(store, 1) == "deployed"
    assert {store.resolve(SHOP, f"s{i}") for i in range(200)} == {1}


def test_full_cycle_twice(store):
    a = approved(store)
    store.deploy(SHOP, a)
    b = approved(store, based_on=a)
    store.deploy(SHOP, b, 10)
    store.deploy(SHOP, b)
    assert [v.state for v in store.list_versions(SHOP)] == ["retired", "retired", "deployed"]
    deployment = store.rollback(SHOP)
    assert (deployment.stable, deployment.previous_stable) == (a, None)
    assert [v.state for v in store.list_versions(SHOP)] == ["retired", "deployed", "retired"]


# ------------------------------------------------------------------ canary split

def test_resolve_is_stable_per_session_key(store):
    n = approved(store)
    store.deploy(SHOP, n, 30)
    keys = [f"session-{i}" for i in range(300)]
    first = [store.resolve(SHOP, key) for key in keys]
    assert first == [store.resolve(SHOP, key) for key in keys]
    assert first == [DefinitionStore(store.root).resolve(SHOP, key) for key in keys]   # across store objects


@pytest.mark.parametrize("percent", [5, 30, 50, 90])
def test_canary_share_roughly_matches(store, percent):
    n = approved(store)
    store.deploy(SHOP, n, percent)
    counts = Counter(store.resolve(SHOP, f"conversation-{i}") for i in range(4000))
    assert set(counts) == {1, n}
    assert abs(counts[n] / 4000 - percent / 100) < 0.03


def test_widening_the_canary_keeps_earlier_sessions_on_it(store):
    n = approved(store)
    store.deploy(SHOP, n, 10)
    keys = [f"k{i}" for i in range(1000)]
    on_canary = {key for key in keys if store.resolve(SHOP, key) == n}
    store.deploy(SHOP, n, 40)
    assert on_canary <= {key for key in keys if store.resolve(SHOP, key) == n}


def test_resolve_without_deployment(store):
    with pytest.raises(DefinitionError):
        store.resolve("clinic-intake", "s1")


def test_real_agents_directory_untouched():
    """These tests never write to v2/agents (compared with the directory as it was when this module loaded)."""
    assert snapshot() == BEFORE
