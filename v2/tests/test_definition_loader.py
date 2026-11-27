"""Reading and checking definition files (R-DEF-1, R-DEF-2)."""
import shutil
from pathlib import Path

import pytest
import yaml

from agentkit.contracts import AgentDefinition
from agentkit.definition import (DefinitionError, DefinitionNotFound, DefinitionStore, dump_version_files,
                                 load_version_dir, validate_definition)

REAL_AGENTS = Path(__file__).resolve().parent.parent / "agents"


@pytest.fixture
def agents(tmp_path):
    root = tmp_path / "agents"
    shutil.copytree(REAL_AGENTS, root)
    return root


def edit(root, file, change, agent="shop-support", version=1):
    path = root / agent / "versions" / str(version) / file
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def fails(root, file, field, agent="shop-support"):
    with pytest.raises(DefinitionError) as caught:
        DefinitionStore(root).load(agent, 1)
    error = caught.value
    assert (file, field) in [(f, k) for f, k, _ in error.problems], str(error)
    assert file in str(error) and field in str(error) and agent in str(error)
    return error


def test_loads_into_contract_type(agents):
    d = DefinitionStore(agents).load("shop-support", 1)
    assert isinstance(d, AgentDefinition)
    assert d.id == "shop-support" and d.version == 1
    assert d.flow[0].id == "verify_first"


def test_env_var_selects_directory(agents, monkeypatch):
    monkeypatch.setenv("AGENTKIT_AGENTS_DIR", str(agents))
    assert DefinitionStore().root == agents
    monkeypatch.delenv("AGENTKIT_AGENTS_DIR")
    from agentkit import config
    assert DefinitionStore().root == config.AGENTS_DIR


def test_unknown_agent_and_version(agents):
    store = DefinitionStore(agents)
    with pytest.raises(DefinitionNotFound):
        store.load("no-such-agent")
    with pytest.raises(DefinitionNotFound):
        store.load("shop-support", 99)
    with pytest.raises(DefinitionNotFound):
        store.list_versions("../etc")
    (agents / "clinic-intake" / "deployment.json").unlink(missing_ok=True)
    with pytest.raises(DefinitionError):          # nothing deployed and no version named
        store.load("clinic-intake")


def test_wrong_type_names_file_and_field(agents):
    edit(agents, "agent.yaml", lambda d: d.update(reply_length="tiny"))
    fails(agents, "agent.yaml", "reply_length")


def test_missing_required_field(agents):
    edit(agents, "agent.yaml", lambda d: d.pop("persona"))
    fails(agents, "agent.yaml", "persona")


def test_nested_field_path(agents):
    edit(agents, "flow.yaml", lambda d: d["flow"][1].pop("rule"))
    fails(agents, "flow.yaml", "flow[1].rule")
    edit(agents, "agent.yaml", lambda d: d["voice"].update(stt="telepathy"), agent="tech-trend-anchor")
    fails(agents, "agent.yaml", "voice.stt", agent="tech-trend-anchor")


def test_unknown_field_is_an_error(agents):
    edit(agents, "agent.yaml", lambda d: d.update(promt="오타"))
    fails(agents, "agent.yaml", "promt")


def test_unknown_nested_field_is_an_error(agents):
    edit(agents, "guards.yaml", lambda d: d["guards"][0].update(patern="x"))
    fails(agents, "guards.yaml", "guards[0].patern")


def test_field_in_the_wrong_file(agents):
    edit(agents, "tools.yaml", lambda d: d.update(prohibitions=["x"]))
    error = fails(agents, "tools.yaml", "prohibitions")
    assert "flow.yaml" in str(error)


def test_missing_file(agents):
    (agents / "shop-support/versions/1/guards.yaml").unlink()
    fails(agents, "guards.yaml", "")


def test_broken_yaml(agents):
    (agents / "shop-support/versions/1/flow.yaml").write_text("flow: [\n  - {id: a, rule: b\n", encoding="utf-8")
    error = fails(agents, "flow.yaml", "")
    assert "YAML" in str(error)


def test_bad_regex_in_guard(agents):
    edit(agents, "guards.yaml", lambda d: d["guards"][0].update(pattern="(열린 괄호"))
    fails(agents, "guards.yaml", "guards[0].pattern")


def test_guard_kind_and_unless_tools(agents):
    edit(agents, "guards.yaml", lambda d: d["guards"][0].update(unless_tools=[]))       # claims_lookup needs tools
    fails(agents, "guards.yaml", "guards[0].unless_tools")
    edit(agents, "guards.yaml", lambda d: d["guards"][0].update(unless_tools=["get_order", "not_my_tool"]))
    assert DefinitionStore(agents).load("shop-support", 1)      # a tool the agent lacks is harmless there
    edit(agents, "guards.yaml", lambda d: d["guards"][3].update(unless_tools=["get_order"]))   # kind regex
    fails(agents, "guards.yaml", "guards[3].unless_tools")


def test_metric_criterion_needs_metric_op_threshold(agents):
    def change(d):
        d["evaluation"]["success"].append({"id": "m1", "rule": "빠르다", "judge": "metric"})
        d["evaluation"]["success"].append({"id": "m2", "rule": "빠르다", "judge": "metric",
                                           "metric": "happiness", "op": "<=", "threshold": 1})
    edit(agents, "evaluation.yaml", change)
    n = len(DefinitionStore(agents).load("tech-trend-anchor", 1).evaluation.success)  # other agent still fine
    assert n > 0
    error = fails(agents, "evaluation.yaml", "evaluation.success[11].metric")
    fields = [k for _, k, _ in error.problems]
    assert "evaluation.success[11].op" in fields and "evaluation.success[11].threshold" in fields
    assert "evaluation.success[12].metric" in fields


def test_enum_field_needs_values(agents):
    edit(agents, "evaluation.yaml", lambda d: d["evaluation"]["extract"][0].update(values=[]))
    fails(agents, "evaluation.yaml", "evaluation.extract[0].values")


def test_duplicate_ids(agents):
    edit(agents, "flow.yaml", lambda d: d["flow"].append(dict(d["flow"][0])))
    fails(agents, "flow.yaml", "flow[12].id")


def test_expect_must_name_a_criterion(agents):
    edit(agents, "tests.yaml", lambda d: d["tests"]["personas"][0]["expect"].update(no_such_criterion=True))
    fails(agents, "tests.yaml", "tests.personas[0].expect.no_such_criterion")


def test_id_must_match_folder(agents):
    edit(agents, "agent.yaml", lambda d: d.update(id="other-shop"))
    fails(agents, "agent.yaml", "id")


def test_avatar_and_knowledge_requirements(agents):
    edit(agents, "agent.yaml", lambda d: (d["avatar"].pop("image"), d["knowledge"].pop("kb_id")),
         agent="tech-trend-anchor")
    error = fails(agents, "agent.yaml", "avatar.image", agent="tech-trend-anchor")
    assert "knowledge.kb_id" in [k for _, k, _ in error.problems]


def test_all_problems_are_reported_together(agents):
    edit(agents, "agent.yaml", lambda d: d.update(language="jp"))
    edit(agents, "tools.yaml", lambda d: d.update(tools="verify_customer"))
    error = fails(agents, "agent.yaml", "language")
    assert ("tools.yaml", "tools") in [(f, k) for f, k, _ in error.problems]


def test_tool_contracts_need_not_exist(agents):
    """The loader knows nothing about v2/tools: unknown tool names are fine (skeleton agents)."""
    edit(agents, "tools.yaml", lambda d: d["tools"].append("tool_that_does_not_exist_yet"))
    assert "tool_that_does_not_exist_yet" in DefinitionStore(agents).load("shop-support", 1).tools


def test_dump_and_load_round_trip(agents, tmp_path):
    for agent in DefinitionStore(agents).list_agents():
        original = DefinitionStore(agents).load(agent, 1)
        folder = tmp_path / "out" / agent
        folder.mkdir(parents=True)
        for file, text in dump_version_files(original).items():
            (folder / file).write_text(text, encoding="utf-8")
        assert load_version_dir(folder, agent, 1) == original


def test_validate_definition_object():
    good = AgentDefinition(id="mini", name="작은 에이전트", persona="친절한 상담원")
    assert validate_definition(good) == good
    bad = good.model_copy(update={"persona": "  "})
    with pytest.raises(DefinitionError) as caught:
        validate_definition(bad)
    assert caught.value.file == "agent.yaml" and caught.value.field == "persona"


# ---- opening, screen, actions (docs/DISPLAY.md): optional parts of agent.yaml ---------------------

SCREEN = {"opening": {"say": "어서 오세요.", "tools": [{"name": "get_order"}]},
          "screen": {"panels": ["menu", "cart"]},
          "actions": [{"name": "show_order", "tool": "get_order", "description": "주문을 다시 본다",
                       "say": "(화면에서 {order_id} 주문을 눌렀습니다)"},
                      {"name": "call_person", "tool": "handoff_to_human"}]}


def test_opening_screen_and_actions_load_from_agent_yaml(agents):
    assert DefinitionStore(agents).load("shop-support", 1).opening.say == ""        # absent: empty defaults
    edit(agents, "agent.yaml", lambda d: d.update(SCREEN))
    d = DefinitionStore(agents).load("shop-support", 1)
    assert d.opening.say == "어서 오세요." and d.opening.tools[0].name == "get_order"
    assert d.opening.tools[0].arguments == {}
    assert d.screen.panels == ["menu", "cart"]
    assert [(a.name, a.tool) for a in d.actions] == [("show_order", "get_order"), ("call_person", "handoff_to_human")]
    assert d.actions[0].say == "(화면에서 {order_id} 주문을 눌렀습니다)" and d.actions[1].description == ""


def test_screen_parts_belong_in_agent_yaml(agents):
    edit(agents, "flow.yaml", lambda d: d.update(actions=SCREEN["actions"]))
    error = fails(agents, "flow.yaml", "actions")
    assert "agent.yaml" in str(error)


def test_action_tool_must_be_one_of_the_agents_tools(agents):
    edit(agents, "agent.yaml", lambda d: d.update(actions=[{"name": "add_item", "tool": "add_to_cart"},
                                                           {"name": "nothing", "tool": " "}]))
    error = fails(agents, "agent.yaml", "actions[0].tool")
    assert "add_to_cart" in str(error) and "tools" in str(error)
    assert "actions[1].tool" in [k for _, k, _ in error.problems]


def test_action_names_are_unique_and_plain(agents):
    edit(agents, "agent.yaml", lambda d: d.update(actions=[{"name": "show", "tool": "get_order"},
                                                           {"name": "show", "tool": "track_delivery"},
                                                           {"name": "주문 보기", "tool": "get_order"}]))
    error = fails(agents, "agent.yaml", "actions[1].name")
    assert "중복" in str(error) and "actions[2].name" in [k for _, k, _ in error.problems]


def test_opening_tool_must_be_one_of_the_agents_tools(agents):
    edit(agents, "agent.yaml", lambda d: d.update(opening={"say": "어서 오세요.", "tools": [{"name": "get_menu"}]}))
    error = fails(agents, "agent.yaml", "opening.tools[0].name")
    assert "get_menu" in str(error)


def test_unknown_keys_and_values_inside_the_screen_parts(agents):
    edit(agents, "agent.yaml", lambda d: d.update(opening={"sey": "오타"}))
    fails(agents, "agent.yaml", "opening.sey")
    edit(agents, "agent.yaml", lambda d: d.update(opening={}, actions=[{"name": "a", "tool": "get_order", "icon": "x"}]))
    fails(agents, "agent.yaml", "actions[0].icon")
    edit(agents, "agent.yaml", lambda d: d.update(actions=[], screen={"panels": ["menu", "hologram"]}))
    fails(agents, "agent.yaml", "screen.panels[1]")
    edit(agents, "agent.yaml", lambda d: d.update(screen={"panels": ["cart", "cart"]}))
    fails(agents, "agent.yaml", "screen.panels[1]")


def test_empty_screen_parts_are_not_written(agents):
    """An agent without a screen keeps its agent.yaml; one with a screen round-trips (see the test above
    for every shipped agent)."""
    plain = DefinitionStore(agents).load("shop-support", 1)
    text = dump_version_files(plain)["agent.yaml"]
    assert "opening" not in text and "screen" not in text and "actions" not in text
    edit(agents, "agent.yaml", lambda d: d.update(SCREEN))
    text = dump_version_files(DefinitionStore(agents).load("shop-support", 1))["agent.yaml"]
    data = yaml.safe_load(text)
    assert data["opening"]["tools"] == [{"name": "get_order", "arguments": {}}] and data["screen"] == SCREEN["screen"]
    assert [a["name"] for a in data["actions"]] == ["show_order", "call_person"]


def test_cart_and_payment_metrics_are_known(agents):
    def change(d):
        d["evaluation"]["success"].append({"id": "m1", "rule": "담았다", "judge": "metric", "metric": "cart_changes",
                                           "op": ">=", "threshold": 1})
        d["evaluation"]["success"].append({"id": "m2", "rule": "결제했다", "judge": "metric",
                                           "metric": "payment_executed", "op": "==", "threshold": 1})
    edit(agents, "evaluation.yaml", change)
    assert len(DefinitionStore(agents).load("shop-support", 1).evaluation.success) == 13
