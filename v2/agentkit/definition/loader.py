"""Six YAML files <-> one contracts.AgentDefinition, with checks that name the file and the field."""
from __future__ import annotations

import re
import types
import typing
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from agentkit.contracts import AgentDefinition

from .errors import DefinitionError

# file -> the top-level keys it holds (ARCHITECTURE.md section 3)
FILES: dict[str, tuple[str, ...]] = {
    "agent.yaml": ("id", "version", "name", "description", "persona", "language", "reply_length",
                   "live_clock", "llm", "voice", "avatar", "knowledge", "opening", "screen", "actions"),
    "tools.yaml": ("tool_packs", "tools"),
    "flow.yaml": ("flow", "handoff", "prohibitions"),
    "guards.yaml": ("guards",),
    "evaluation.yaml": ("evaluation",),
    "tests.yaml": ("tests",),
}
FILE_OF = {key: file for file, keys in FILES.items() for key in keys}
METRICS = ("response_ms_median", "response_ms_max", "tool_failures", "interruptions", "turns", "handoff",
           "cart_changes", "payment_executed")
# optional parts (docs/DISPLAY.md): left out of the written files while they are empty, so an agent
# that has no screen keeps the agent.yaml it had
OPTIONAL_KEYS = ("opening", "screen", "actions", "tool_packs")
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
ITEM_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def file_of(field: str) -> str:
    """The file a field path ("flow[0].rule") lives in."""
    return FILE_OF.get(re.split(r"[.\[]", field, maxsplit=1)[0], "agent.yaml")


def _path(loc) -> str:
    out = ""
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else (("." if out else "") + str(part))
    return out


def _model_of(annotation):
    """(kind, model) for a field annotation: kind "model", "list" (of models) or None."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return "model", annotation
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin in (typing.Union, types.UnionType):
        for arg in args:
            found = _model_of(arg)
            if found[0]:
                return found
    if origin in (list, typing.List) and args:
        kind, model = _model_of(args[0])
        if kind == "model":
            return "list", model
    return None, None


def _unknown_keys(model: type[BaseModel], data: Any, loc: tuple) -> list[tuple[tuple, str]]:
    """Keys the model does not know. Pydantic ignores them silently; a typo ("promt") must not pass."""
    if not isinstance(data, dict):
        return []
    found = []
    for key, value in data.items():
        if key not in model.model_fields:
            found.append((loc + (key,), "알 수 없는 항목입니다 (unknown field)"))
            continue
        kind, sub = _model_of(model.model_fields[key].annotation)
        if kind == "model":
            found += _unknown_keys(sub, value, loc + (key,))
        elif kind == "list" and isinstance(value, list):
            for index, item in enumerate(value):
                found += _unknown_keys(sub, item, loc + (key, index))
    return found


def _duplicates(items, attr, base) -> list[tuple[str, str]]:
    seen, found = set(), []
    for index, item in enumerate(items):
        value = getattr(item, attr)
        if value in seen:
            found.append((f"{base}[{index}].{attr}", f"'{value}'가 중복됩니다 (duplicate)"))
        seen.add(value)
        if not ITEM_ID_PATTERN.match(str(value)):
            found.append((f"{base}[{index}].{attr}", f"'{value}': 영문, 숫자, _ - . 만 쓸 수 있습니다"))
    return found


def semantic_problems(d: AgentDefinition) -> list[tuple[str, str]]:
    """Faults the types cannot catch, as (field path, message)."""
    out: list[tuple[str, str]] = []
    if not ID_PATTERN.match(d.id):
        out.append(("id", f"'{d.id}': 영문 소문자, 숫자, - _ 만 쓸 수 있습니다"))
    if d.version < 1:
        out.append(("version", "1 이상이어야 합니다"))
    for name in ("name", "persona"):
        if not getattr(d, name).strip():
            out.append((name, "비어 있습니다 (empty)"))
    if d.avatar.enabled and not d.avatar.image:
        out.append(("avatar.image", "avatar.enabled가 true이면 image가 필요합니다"))
    if d.knowledge.kind != "none" and not d.knowledge.kb_id:
        out.append(("knowledge.kb_id", f"knowledge.kind가 {d.knowledge.kind}이면 kb_id가 필요합니다"))
    if not 0 <= d.knowledge.threshold <= 1:
        out.append(("knowledge.threshold", "0과 1 사이여야 합니다"))
    for name in ("num_ctx", "num_predict"):
        if getattr(d.llm, name) <= 0:
            out.append((f"llm.{name}", "0보다 커야 합니다"))

    seen = set()
    for index, tool in enumerate(d.tools):
        if not tool.strip():
            out.append((f"tools[{index}]", "도구 이름이 비어 있습니다"))
        if tool in seen:
            out.append((f"tools[{index}]", f"'{tool}'가 중복됩니다 (duplicate)"))
        seen.add(tool)

    # screen and first words: every tool they name must be one of the agent's own tools, or the
    # opening call / the touch would reach a tool the agent was never given
    for index, call in enumerate(d.opening.tools):
        if not call.name.strip():
            out.append((f"opening.tools[{index}].name", "도구 이름이 비어 있습니다"))
        elif call.name not in d.tools:
            out.append((f"opening.tools[{index}].name",
                        f"'{call.name}'는 이 에이전트의 tools에 없습니다 (tools.yaml에 먼저 추가)"))
    seen_panels = set()
    for index, panel in enumerate(d.screen.panels):
        if panel in seen_panels:
            out.append((f"screen.panels[{index}]", f"'{panel}'가 중복됩니다 (duplicate)"))
        seen_panels.add(panel)
    out += _duplicates(d.actions, "name", "actions")
    for index, action in enumerate(d.actions):
        if not action.tool.strip():
            out.append((f"actions[{index}].tool", "도구 이름이 비어 있습니다"))
        elif action.tool not in d.tools:
            out.append((f"actions[{index}].tool",
                        f"'{action.tool}'는 이 에이전트의 tools에 없습니다 (tools.yaml에 먼저 추가)"))

    out += _duplicates(d.flow, "id", "flow")
    out += _duplicates(d.handoff, "id", "handoff")
    for index, rule in enumerate(d.flow):
        if not rule.rule.strip():
            out.append((f"flow[{index}].rule", "비어 있습니다 (empty)"))
    for index, rule in enumerate(d.handoff):
        if not rule.when.strip():
            out.append((f"handoff[{index}].when", "비어 있습니다 (empty)"))
    for index, text in enumerate(d.prohibitions):
        if not text.strip():
            out.append((f"prohibitions[{index}]", "비어 있습니다 (empty)"))

    out += _duplicates(d.guards, "id", "guards")
    for index, guard in enumerate(d.guards):
        base = f"guards[{index}]"
        try:
            re.compile(guard.pattern)
        except re.error as error:
            out.append((f"{base}.pattern", f"정규식이 틀렸습니다: {error}"))
        if not guard.pattern:
            out.append((f"{base}.pattern", "비어 있습니다 (empty)"))
        if not guard.nudge.strip():
            out.append((f"{base}.nudge", "비어 있습니다 (empty)"))
        if guard.kind == "regex" and guard.unless_tools:
            out.append((f"{base}.unless_tools", "kind가 regex인 규칙에는 쓸 수 없습니다 (claims_lookup 전용)"))
        if guard.kind == "claims_lookup" and not guard.unless_tools:
            out.append((f"{base}.unless_tools", "kind가 claims_lookup이면 도구가 하나 이상 필요합니다"))
        # unless_tools naming a tool the agent does not have is allowed: it is harmless, and
        # refusing it would make "remove a tool" fail for every guard that mentions the tool

    ev = d.evaluation
    out += _duplicates(ev.success, "id", "evaluation.success")
    for index, c in enumerate(ev.success):
        base = f"evaluation.success[{index}]"
        if not c.rule.strip():
            out.append((f"{base}.rule", "비어 있습니다 (empty)"))
        if c.judge == "metric":
            if c.metric is None:
                out.append((f"{base}.metric", "judge가 metric이면 필요합니다"))
            elif c.metric not in METRICS:
                out.append((f"{base}.metric", f"'{c.metric}': {', '.join(METRICS)} 중 하나여야 합니다"))
            if c.op is None:
                out.append((f"{base}.op", "judge가 metric이면 필요합니다"))
            if c.threshold is None:
                out.append((f"{base}.threshold", "judge가 metric이면 필요합니다"))
        else:
            for name in ("metric", "op", "threshold"):
                if getattr(c, name) is not None:
                    out.append((f"{base}.{name}", "judge가 llm인 기준에는 쓸 수 없습니다"))
    out += _duplicates(ev.extract, "name", "evaluation.extract")
    for index, f in enumerate(ev.extract):
        base = f"evaluation.extract[{index}]"
        if f.type == "enum" and not f.values:
            out.append((f"{base}.values", "type이 enum이면 값이 하나 이상 필요합니다"))
        if f.type != "enum" and f.values:
            out.append((f"{base}.values", "type이 enum일 때만 쓸 수 있습니다"))
    if ev.latency_budget_ms <= 0:
        out.append(("evaluation.latency_budget_ms", "0보다 커야 합니다"))

    criteria = {c.id for c in ev.success}
    out += _duplicates(d.tests.personas, "id", "tests.personas")
    out += _duplicates(d.tests.cases, "id", "tests.cases")
    persona_ids = {p.id for p in d.tests.personas}
    for index, p in enumerate(d.tests.personas):
        base = f"tests.personas[{index}]"
        if not p.goal.strip():
            out.append((f"{base}.goal", "비어 있습니다 (empty)"))
        if p.max_turns < 1:
            out.append((f"{base}.max_turns", "1 이상이어야 합니다"))
        for key in p.expect:
            if key not in criteria:
                out.append((f"{base}.expect.{key}", "evaluation.success에 없는 기준입니다"))
    for index, case in enumerate(d.tests.cases):
        base = f"tests.cases[{index}]"
        if case.id in persona_ids:
            out.append((f"{base}.id", f"'{case.id}'는 가상 고객 id와 겹칩니다"))
        if not case.user_turns:
            out.append((f"{base}.user_turns", "사용자 턴이 하나 이상 필요합니다"))
        for key in case.expect:
            if key not in criteria:
                out.append((f"{base}.expect.{key}", "evaluation.success에 없는 기준입니다"))
    return out


def definition_from_parts(data: dict, agent_id: str = "", version: int | None = None) -> AgentDefinition:
    """Builds and checks a definition from the merged content of the files.

    Raises DefinitionError listing every fault with its file and field."""
    problems = [(file_of(_path(loc)), _path(loc), message)
                for loc, message in _unknown_keys(AgentDefinition, data, ())]
    try:
        definition = AgentDefinition.model_validate(data)
    except ValidationError as error:
        for item in error.errors():
            path = _path(item["loc"])
            problems.append((file_of(path), path, item["msg"]))
        raise DefinitionError(problems, agent_id, version) from None
    problems += [(file_of(field), field, message) for field, message in semantic_problems(definition)]
    if agent_id and definition.id != agent_id:
        problems.append(("agent.yaml", "id", f"'{definition.id}'가 폴더 이름 '{agent_id}'와 다릅니다"))
    if version is not None and definition.version != version:
        problems.append(("agent.yaml", "version", f"{definition.version}이 폴더의 버전 {version}과 다릅니다"))
    if problems:
        raise DefinitionError(problems, agent_id or definition.id, version)
    return definition


def validate_definition(definition: AgentDefinition) -> AgentDefinition:
    """Re-checks an AgentDefinition object (one built in code, e.g. by the builder)."""
    return definition_from_parts(definition.model_dump(mode="json"), definition.id, definition.version)


def load_version_dir(path: Path, agent_id: str = "", version: int | None = None) -> AgentDefinition:
    """Reads the six files of one version folder."""
    path = Path(path)
    problems: list[tuple[str, str, str]] = []
    merged: dict[str, Any] = {}
    for file, keys in FILES.items():
        target = path / file
        if not target.is_file():
            problems.append((file, "", "파일이 없습니다 (missing file)"))
            continue
        try:
            content = yaml.safe_load(target.read_text(encoding="utf-8"))
        except yaml.YAMLError as error:
            mark = getattr(error, "problem_mark", None)
            where = f"{mark.line + 1}번째 줄: " if mark else ""
            problems.append((file, "", f"YAML을 읽을 수 없습니다: {where}{getattr(error, 'problem', None) or error}"))
            continue
        if content is None:
            content = {}
        if not isinstance(content, dict):
            problems.append((file, "", "최상위가 '항목: 값' 형태여야 합니다 (mapping expected)"))
            continue
        for key, value in content.items():
            if key not in keys:
                home = FILE_OF.get(key)
                hint = f"{home}에 적어야 합니다" if home else "알 수 없는 항목입니다 (unknown field)"
                problems.append((file, str(key), hint))
            else:
                merged[key] = value
    if problems:
        raise DefinitionError(problems, agent_id, version)
    if version is not None:
        merged.setdefault("version", version)
    return definition_from_parts(merged, agent_id, version)


class _Dumper(yaml.SafeDumper):
    pass


def _str_representer(dumper, value):
    if "\n" in value:
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", value)


_Dumper.add_representer(str, _str_representer)


def dump_version_files(definition: AgentDefinition) -> dict[str, str]:
    """file name -> YAML text. The version number is not written: the folder says it."""
    data = definition.model_dump(mode="json")
    empty = AgentDefinition(id="x", name="x", persona="x").model_dump(mode="json")
    out = {}
    for file, keys in FILES.items():
        part = {key: data[key] for key in keys if key in data and key != "version"
                and not (key in OPTIONAL_KEYS and data[key] == empty[key])}
        out[file] = yaml.dump(part, Dumper=_Dumper, allow_unicode=True, sort_keys=False, width=100)
    return out
