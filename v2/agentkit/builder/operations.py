"""The only things the builder agent can do (ARCHITECTURE.md section 10).

Every operation is a function over DefinitionStore and the evaluation bridge. Arguments coming
from the model are checked here; anything wrong is returned to the model as text (OperationError)
so it can correct the call. There is no operation that approves, deploys or rolls back.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

import yaml
from pydantic import ValidationError

from agentkit.contracts import (AgentDefinition, ExtractField, FlowRule, Guard, HandoffRule, ReleaseError,
                                SuccessCriterion, TestPersona)
from agentkit.definition import DefinitionError, DefinitionNotFound, definition_from_parts, validate_definition

CREATED_BY = "builder"
STATE_LABEL = {"draft": "초안", "tested": "시험 완료", "pending_approval": "승인 대기", "approved": "승인됨",
               "deployed": "배포됨", "retired": "물러남"}
# a newer line of work that is not deployed yet: a new draft continues from it, not from the stable one
CARRY_OVER_STATES = ("tested", "pending_approval", "approved")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
ITEM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
HANDOFF_TOOL = "handoff_to_human"


class OperationError(Exception):
    """Told to the model as the operation's result."""


# ---- argument schemas ---------------------------------------------------------------------------

def _p(type_, description="", **more):
    return {"type": type_, "description": description, **more}


AGENT = _p("string", "에이전트 id")
VERSION = _p("integer", "생략하면 가장 새 버전")
METRICS = ["response_ms_median", "response_ms_max", "tool_failures", "interruptions", "turns", "handoff"]
FLOW_PROPS = {"id": _p("string", "예: verify_first"), "rule": _p("string", "절차 한 문장")}
HANDOFF_PROPS = {"id": _p("string", "예: large_refund"), "when": _p("string", "조건 한 문장. 예: 환불 금액이 50000원을 넘는다")}
CRITERION_PROPS = {
    "id": _p("string", "예: verified_before_disclosure"),
    "rule": _p("string", "판정할 문장. 예: 본인 확인 전에 주문 내용을 말하지 않았다"),
    "safety": _p("boolean", "안전 기준이면 true. 기존 기준을 바꿀 때 생략하면 원래 값 유지"),
    "judge": _p("string", "기본 llm", enum=["llm", "metric"]),
    "metric": _p("string", "judge=metric일 때", enum=METRICS),
    "op": _p("string", "judge=metric일 때", enum=["<=", ">=", "==", "<", ">"]),
    "threshold": _p("number", "judge=metric일 때"),
}
EXTRACT_PROPS = {
    "name": _p("string", "예: inquiry_type"),
    "type": _p("string", "기본 string", enum=["string", "enum", "boolean", "number"]),
    "values": _p("array", "enum의 값 목록", items={"type": "string"}),
    "description": _p("string", "무엇을 뽑는지"),
    "pii": _p("boolean", "개인정보면 true"),
}
PERSONA_PROPS = {
    "id": _p("string", "예: refund_small"),
    "description": _p("string", "어떤 고객이고 어떻게 말하는지"),
    "goal": _p("string", "고객이 이루려는 것"),
    "facts": _p("object", "고객이 아는 값. 예: {\"order_id\": \"A1001\"}"),
    "opening": _p("string", "첫 마디"),
    "max_turns": _p("integer", "기본 8"),
    "expect": _p("object", "성공 기준 id -> 맞게 응대했을 때의 판정(true/false)"),
}


def _bare(properties, required):
    """The same object without the per-field descriptions (for the items of create_agent)."""
    return {"type": "object", "required": list(required),
            "properties": {key: {k: v for k, v in value.items() if k != "description"} for key, value in properties.items()}}


def _spec(name, description, properties, required=()):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": list(required)}}}


SPECS: list[dict] = [
    _spec("read_definition", "정의서를 더 자세히 읽는다. 선택된 에이전트의 요약은 이미 주어져 있다.",
          {"agent_id": AGENT, "version": VERSION,
           "part": _p("string", "기본 summary", enum=["summary", "guards", "evaluation", "tests", "all"])}, ["agent_id"]),
    _spec("list_versions", "버전 목록과 상태를 본다.", {"agent_id": AGENT}, ["agent_id"]),
    _spec("create_agent",
          "새 에이전트의 초안(버전 1)을 만든다. 담당자가 하는 일/하지 않는 일, 필요한 외부 동작, 본인 확인 방법, "
          "넘김 조건을 답한 뒤에만 호출한다. 완성된 정의서를 한 번에 넘긴다.",
          {"id": _p("string", "영문 소문자, 숫자, 하이픈. 예: hotel-booking"),
           "name": _p("string", "사람이 읽는 이름"),
           "description": _p("string", "하는 일 한 줄"),
           "persona": _p("string", "누구이고 어떻게 말하는지, 하는 일과 하지 않는 일. 여러 문장"),
           "language": _p("string", "기본 ko", enum=["ko", "en", "auto"]),
           "reply_length": _p("string", "기본 short", enum=["short", "normal", "long", "explain"]),
           "tools": _p("array", "등록된 도구 이름만", items={"type": "string"}),
           "flow": _p("array", "지켜야 할 절차. id는 영문(예: verify_first)", items=_bare(FLOW_PROPS, ["id", "rule"])),
           "handoff": _p("array", "사람에게 넘기는 조건", items=_bare(HANDOFF_PROPS, ["id", "when"])),
           "no_handoff": _p("boolean", "담당자가 넘기지 않기로 정했을 때만 true"),
           "prohibitions": _p("array", "절대 하지 않는 일. 문장 목록", items={"type": "string"}),
           "success": _p("array", "성공 기준. safety=true인 안전 기준을 하나 이상 포함. rule은 '…하지 않았다'처럼 판정할 문장",
                         items=_bare({k: CRITERION_PROPS[k] for k in ("id", "rule", "safety")}, ["id", "rule"])),
           "extract": _p("array", "대화에서 뽑을 정보. name은 영문(예: order_id). 개인정보면 pii=true",
                         items=_bare(EXTRACT_PROPS, ["name"])),
           "personas": _p("array", "시험용 가상 고객 2~4명. expect는 성공 기준 id -> true/false",
                          items=_bare(PERSONA_PROPS, ["id", "description", "goal"]))},
          ["id", "name", "persona", "tools", "flow", "prohibitions", "success", "extract", "personas"]),
    _spec("create_draft", "기존 버전을 바탕으로 빈 초안을 만든다. 변경 연산이 초안을 알아서 만들므로 보통은 필요 없다.",
          {"agent_id": AGENT, "based_on": _p("integer", "생략하면 배포된 버전")}, ["agent_id"]),
    _spec("set_field", "단일 항목을 바꾼다. 말투·성격은 persona(전체 문장을 새로 써서 넘긴다), 답 길이는 reply_length.",
          {"agent_id": AGENT,
           "field": _p("string", "persona, reply_length(short|normal|long|explain), name, description, "
                                 "language(ko|en|auto), live_clock, latency_budget_ms, voice.tts_voice, voice.stt, "
                                 "voice.endpoint, voice.interrupt_mode, voice.barge_in, voice.speculative, llm.model"),
           "value": _p("string", "새 값")}, ["agent_id", "field", "value"]),
    _spec("add_flow_rule", "지켜야 할 절차를 더한다. 있는 id를 주면 그 규칙을 바꾼다.",
          {"agent_id": AGENT, **FLOW_PROPS}, ["agent_id", "id", "rule"]),
    _spec("remove_flow_rule", "흐름 규칙 하나를 지운다.", {"agent_id": AGENT, "id": _p("string")}, ["agent_id", "id"]),
    _spec("add_prohibition", "금지 사항을 더한다. 기존 문장을 고치려면 replaces에 그 번호를 준다. [안전 규칙]",
          {"agent_id": AGENT, "text": _p("string", "예: 50000원을 넘는 환불을 접수하지 않는다"),
           "replaces": _p("integer", "바꿀 기존 금지 사항의 번호(1부터)")}, ["agent_id", "text"]),
    _spec("add_handoff_rule", "사람(상담원)에게 넘기는 조건을 더한다. 있는 id를 주면 그 조건을 바꾼다. [안전 규칙]",
          {"agent_id": AGENT, **HANDOFF_PROPS}, ["agent_id", "id", "when"]),
    _spec("add_guard", "말하기 전 검사 규칙(정규식)을 더한다. 있는 id면 바꾼다. [안전 규칙]",
          {"agent_id": AGENT, "id": _p("string"),
           "kind": _p("string", "regex: pattern에 맞는 문장은 위반. claims_lookup: unless_tools를 부르지 않고 pattern에 맞게 말하면 위반",
                      enum=["regex", "claims_lookup"]),
           "pattern": _p("string", "정규식"),
           "unless_tools": _p("array", "claims_lookup일 때", items={"type": "string"}),
           "nudge": _p("string", "걸렸을 때 모델에 주는 안내")}, ["agent_id", "id", "pattern", "nudge"]),
    _spec("set_tools", "쓸 수 있는 도구 목록 전체를 정한다. 등록된 도구만. [안전 규칙]",
          {"agent_id": AGENT, "tools": _p("array", "전체 목록", items={"type": "string"})}, ["agent_id", "tools"]),
    _spec("add_success_criterion", "성공 기준을 더한다. 있는 id면 바꾼다.",
          {"agent_id": AGENT, **CRITERION_PROPS}, ["agent_id", "id", "rule"]),
    _spec("add_extract_field", "대화에서 뽑을 정보를 더한다. 있는 name이면 바꾼다.",
          {"agent_id": AGENT, **EXTRACT_PROPS}, ["agent_id", "name"]),
    _spec("add_persona", "시험용 가상 고객을 더한다. 있는 id면 바꾼다.",
          {"agent_id": AGENT, **PERSONA_PROPS}, ["agent_id", "id", "description", "goal"]),
    _spec("run_tests", "가상 고객 시험 한 벌을 시작한다. 시험해 달라고 하면 바로 호출한다. 몇 분 걸리며 실행 번호만 바로 돌아온다.",
          {"agent_id": AGENT, "version": VERSION}, ["agent_id"]),
    _spec("check_tests", "시험 실행의 진행과 결과(통과율, 안전 실패, 실패 항목)를 본다.",
          {"run_id": _p("string", "생략하면 마지막 실행"), "agent_id": AGENT}, []),
    _spec("compare_runs", "두 시험 실행을 비교한다: 좋아진 항목, 나빠진 항목, 지표 변화.",
          {"new_run_id": _p("string", "생략하면 마지막 실행"),
           "base_run_id": _p("string", "생략하면 바탕 버전이나 배포 버전의 최근 실행"), "agent_id": AGENT}, []),
    _spec("submit_for_approval", "시험이 끝난 버전의 승인을 사람에게 요청한다.",
          {"agent_id": AGENT, "version": VERSION}, ["agent_id"]),
    _spec("list_failed_sessions", "최근 대화 중 성공 기준에 실패한 것을 기준별로 묶어 본다.",
          {"agent_id": AGENT, "limit": _p("integer", "살펴볼 대화 수. 기본 100")}, ["agent_id"]),
]
OPERATION_NAMES = [spec["function"]["name"] for spec in SPECS]
MODIFYING = ("create_agent", "create_draft", "set_field", "add_flow_rule", "remove_flow_rule", "add_prohibition",
             "add_handoff_rule", "add_guard", "set_tools", "add_success_criterion", "add_extract_field", "add_persona")
_SCHEMAS = {spec["function"]["name"]: spec["function"]["parameters"] for spec in SPECS}
CREATE_MODE = ("create_agent", "read_definition", "list_versions")


def specs_for(mode: str) -> list[dict]:
    """The operations offered to the model. "create": a new agent is being discussed (the big
    create_agent schema and the two readers). "edit": everything but create_agent. Splitting keeps
    the prompt inside the model's context window; no mode contains approve, deploy or rollback."""
    if mode == "create":
        return [spec for spec in SPECS if spec["function"]["name"] in CREATE_MODE]
    return [spec for spec in SPECS if spec["function"]["name"] != "create_agent"]


def _coerce(value, schema, path):
    """The value in the shape the schema asks for; OperationError names the argument."""
    kind = schema.get("type")
    if kind == "string":
        if isinstance(value, bool) or isinstance(value, (dict, list)):
            raise OperationError(f"{path}: 글자여야 합니다")
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        value = str(value) if not isinstance(value, str) else value
    elif kind == "integer":
        if isinstance(value, str) and re.fullmatch(r"\s*-?\d+\s*", value):
            value = int(value)
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int):
            raise OperationError(f"{path}: 정수여야 합니다")
    elif kind == "number":
        if isinstance(value, str):
            try:
                value = float(value)
            except ValueError:
                raise OperationError(f"{path}: 숫자여야 합니다") from None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise OperationError(f"{path}: 숫자여야 합니다")
    elif kind == "boolean":
        if isinstance(value, str) and value.strip().lower() in ("true", "false", "yes", "no", "예", "아니오"):
            value = value.strip().lower() in ("true", "yes", "예")
        if not isinstance(value, bool):
            raise OperationError(f"{path}: true 또는 false여야 합니다")
    elif kind in ("array", "object"):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                if kind == "array" and schema.get("items", {}).get("type") == "string":
                    value = [value]
                else:
                    raise OperationError(f"{path}: {'목록' if kind == 'array' else '객체'}이어야 합니다") from None
        if kind == "array":
            if not isinstance(value, list):
                raise OperationError(f"{path}: 목록이어야 합니다")
            items = schema.get("items")
            if items:
                value = [_coerce(item, items, f"{path}[{index}]") for index, item in enumerate(value)]
        else:
            if not isinstance(value, dict):
                raise OperationError(f"{path}: 객체여야 합니다")
            if "properties" in schema:
                value = _check_object(value, schema, path)
    if "enum" in schema and value not in schema["enum"]:
        raise OperationError(f"{path}: '{value}'는 쓸 수 없습니다. 가능한 값: {', '.join(map(str, schema['enum']))}")
    return value


def _check_object(arguments, schema, path=""):
    prefix = f"{path}." if path else ""
    properties = schema.get("properties", {})
    unknown = [key for key in arguments if key not in properties]
    if unknown:
        raise OperationError(f"{prefix}{unknown[0]}: 모르는 인자입니다. 쓸 수 있는 인자: {', '.join(properties)}")
    out = {}
    for key, value in arguments.items():
        if value is None:
            continue
        out[key] = _coerce(value, properties[key], prefix + key)
    missing = [key for key in schema.get("required", []) if key not in out or out[key] == ""]
    if missing:
        raise OperationError(f"필요한 인자가 없습니다: {', '.join(prefix + key for key in missing)}")
    return out


def check_arguments(name: str, arguments: Any) -> dict:
    if name not in _SCHEMAS:
        raise OperationError(f"'{name}'이라는 연산은 없습니다. 쓸 수 있는 연산: {', '.join(OPERATION_NAMES)}")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            raise OperationError("인자는 JSON 객체여야 합니다") from None
    if not isinstance(arguments, dict):
        raise OperationError("인자는 JSON 객체여야 합니다")
    return _check_object(arguments, _SCHEMAS[name])


# ---- per-turn bookkeeping -----------------------------------------------------------------------

class Turn:
    """What happened while one message was answered. The reply's change block is built from this
    and from the store, never from what the model says."""

    def __init__(self, default_agent: Optional[str] = None, user_turns: int = 1, last_run_id: Optional[str] = None):
        self.default_agent = default_agent
        self.user_turns = user_turns
        self.last_run_id = last_run_id
        self.changes: dict[tuple[str, int], dict] = {}   # (agent, version) -> {"created": bool, "ops": [..]}
        self.new_agents: list[str] = []
        self.runs: list[dict] = []                       # {run_id, agent_id, version, total}
        self.submitted: list[tuple[str, int]] = []
        self.log: list[dict] = []                        # {name, arguments, ok, result}
        self.removed: dict[tuple[str, int], set[str]] = {}   # values this turn replaced somewhere

    def touched(self, agent_id, version, created, summary):
        entry = self.changes.setdefault((agent_id, version), {"created": False, "ops": []})
        entry["created"] = entry["created"] or created
        if summary:
            entry["ops"].append(summary)
        self.default_agent = agent_id

    @property
    def acted(self):
        return bool(self.changes or self.runs or self.submitted)


# ---- helpers on definitions ---------------------------------------------------------------------

def _numbers(text: str) -> set[str]:
    """Numbers of four digits or more in a sentence, "5만"/"10만 원" style included."""
    text = text.replace(",", "")
    found = set(re.findall(r"(?<!\d)\d{4,}(?!\d)", text))
    for digits in re.findall(r"(?<!\d)(\d{1,4})\s?만(?!\s?\d)", text):
        found.add(str(int(digits) * 10000))
    return found


def text_items(definition: AgentDefinition) -> dict[str, str]:
    """label -> sentence, for every place a business value can be written."""
    items = {"persona": definition.persona}
    for rule in definition.flow:
        items[f"flow.{rule.id}"] = rule.rule
    for rule in definition.handoff:
        items[f"handoff.{rule.id}"] = rule.when
    for index, sentence in enumerate(definition.prohibitions, 1):
        items[f"prohibitions[{index}]"] = sentence
    for criterion in definition.evaluation.success:
        if criterion.judge == "llm":
            items[f"success.{criterion.id}"] = criterion.rule
    return items


def removed_values(before: AgentDefinition, after: AgentDefinition) -> set[str]:
    """Numbers that a change took out of a sentence (the sentence itself still exists)."""
    a, b = text_items(before), text_items(after)
    removed = set()
    for label, old in a.items():
        if label in b and b[label] != old:
            removed |= _numbers(old) - _numbers(b[label])
    return removed


def stale_places(definition: AgentDefinition, values) -> dict[str, list[str]]:
    """Where the definition still carries one of these values:
    {"100000": ["flow.refund_limit", "prohibitions[3]"]}."""
    items = text_items(definition)
    out = {}
    for value in sorted(values):
        places = [label for label, sentence in items.items() if value in _numbers(sentence)]
        if places:
            out[value] = places
    return out


def _upsert(items: list, new, key: str) -> bool:
    """Replaces the item with the same key or appends. True when one was replaced."""
    for index, item in enumerate(items):
        if getattr(item, key) == getattr(new, key):
            items[index] = new
            return True
    items.append(new)
    return False


def _item_id(value, what="id"):
    if not ITEM_ID_RE.match(value or ""):
        raise OperationError(f"{what} '{value}'는 쓸 수 없습니다. 영문, 숫자, 밑줄로 짧게 적으세요 (예: large_refund)")
    return value


def _model(cls, data):
    try:
        return cls.model_validate(data)
    except ValidationError as error:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in error.errors())
        raise OperationError(f"형식이 맞지 않습니다: {problems}") from None


def summarize(definition: AgentDefinition, info=None, part: str = "summary", rule_chars: int = 400) -> str:
    """A definition as compact text for the model."""
    d = definition
    if part in ("guards", "evaluation", "tests", "all"):
        data = d.model_dump(mode="json")
        chosen = data if part == "all" else {part: data[part]}
        head = f"# {d.id} v{d.version}" + (f" ({STATE_LABEL.get(info.state, info.state)})" if info else "")
        return head + "\n" + yaml.safe_dump(chosen, allow_unicode=True, sort_keys=False, width=200)

    def cut(text):
        text = " ".join(str(text).split())
        return text if len(text) <= rule_chars else text[:rule_chars - 1] + "…"

    lines = [f"{d.id} v{d.version}" + (f" ({STATE_LABEL.get(info.state, info.state)})" if info else "")
             + f" · 이름: {d.name}"]
    if d.description:
        lines.append(f"설명: {cut(d.description)}")
    lines.append("persona (역할과 말투):")
    lines += ["  " + line for line in d.persona.strip().splitlines()]
    lines.append(f"language={d.language} reply_length={d.reply_length} live_clock={str(d.live_clock).lower()} "
                 f"latency_budget_ms={d.evaluation.latency_budget_ms}")
    lines.append("tools (쓸 수 있는 도구): " + (", ".join(d.tools) or "없음"))
    lines.append("flow (지켜야 할 절차):" + ("" if d.flow else " 없음"))
    lines += [f"  - {rule.id}: {cut(rule.rule)}" for rule in d.flow]
    lines.append("handoff (사람에게 넘기는 조건):" + ("" if d.handoff else " 없음"))
    lines += [f"  - {rule.id}: {cut(rule.when)}" for rule in d.handoff]
    lines.append("prohibitions (금지 사항):" + ("" if d.prohibitions else " 없음"))
    lines += [f"  {index}. {cut(sentence)}" for index, sentence in enumerate(d.prohibitions, 1)]
    lines.append("guards (말하기 전 검사): " + (", ".join(guard.id for guard in d.guards) or "없음"))
    lines.append("success (성공 기준):" + ("" if d.evaluation.success else " 없음"))
    for criterion in d.evaluation.success:
        if criterion.judge == "metric":
            lines.append(f"  - {criterion.id} [수치: {criterion.metric} {criterion.op} {criterion.threshold:g}]")
        else:
            lines.append(f"  - {criterion.id}{' [안전]' if criterion.safety else ''}: {cut(criterion.rule)}")
    lines.append("extract (뽑을 정보): " + (", ".join(
        f"{field.name}({field.type}{', 개인정보' if field.pii else ''})" for field in d.evaluation.extract) or "없음"))
    lines.append("personas (가상 고객): " + (", ".join(persona.id for persona in d.tests.personas) or "없음")
                 + f" · 저장된 사례 {len(d.tests.cases)}개")
    return "\n".join(lines)


# ---- the operations -----------------------------------------------------------------------------

class Operations:
    """store: DefinitionStore. evaluator: see evalbridge.EvalBridge (start, load, compare, list_runs).
    tool_catalog: callable -> {name: (effect, description)} or None when the contracts cannot be read.
    trace_store: callable -> the trace store (list_sessions, get_document), for list_failed_sessions."""

    def __init__(self, store, evaluator=None, tool_catalog: Optional[Callable[[], Optional[dict]]] = None,
                 trace_store: Optional[Callable[[], Any]] = None):
        self.store = store
        self.evaluator = evaluator
        self.tool_catalog = tool_catalog or (lambda: None)
        self.trace_store = trace_store

    async def execute(self, turn: Turn, name: str, arguments: Any, allowed=None) -> tuple[bool, str]:
        """Runs one operation. (ok, text for the model). Never raises.
        allowed: the names offered to the model in this turn; anything else is refused."""
        try:
            if allowed is not None and name in _SCHEMAS and name not in allowed:
                raise OperationError(
                    f"'{name}'은 지금 쓸 수 없습니다. 지금 쓸 수 있는 연산: {', '.join(allowed)}. "
                    + ("새 에이전트를 만들려면 담당자에게 '새 에이전트를 만들고 싶다'고 말해 달라고 하세요."
                       if name == "create_agent" else "이미 있는 에이전트를 고치려면 어느 에이전트인지 담당자에게 확인하세요."))
            if isinstance(arguments, dict) and "agent_id" in _SCHEMAS.get(name, {}).get("properties", {}) \
                    and not arguments.get("agent_id") and turn.default_agent:
                arguments = {**arguments, "agent_id": turn.default_agent}
            checked = check_arguments(name, arguments)
            result = getattr(self, "_op_" + name)(turn, **checked)
            if hasattr(result, "__await__"):
                result = await result
            ok, text = True, str(result)
        except OperationError as error:
            ok, text = False, f"오류: {error}"
        except DefinitionNotFound as error:
            ok, text = False, f"오류: {error}. 에이전트 목록: {', '.join(self._agents()) or '없음'}"
        except DefinitionError as error:
            ok, text = False, (f"오류: 정의서가 형식에 맞지 않아 저장하지 않았습니다.\n{error}\n"
                               "형식 문제는 담당자에게 묻지 말고 인자를 고쳐 다시 호출하세요.")
        except ReleaseError as error:
            ok, text = False, f"거절됨: {error}"
        except Exception as error:   # a bug or an outside failure must not end the conversation
            ok, text = False, f"오류: 연산을 실행하지 못했습니다 ({type(error).__name__}: {error})"
        turn.log.append({"name": name, "arguments": arguments if isinstance(arguments, dict) else {},
                         "ok": ok, "result": text[:600]})
        return ok, text

    # ---------------------------------------------------------------- shared pieces

    def _agents(self) -> list[str]:
        try:
            return self.store.list_agents()
        except Exception:
            return []

    def _newest(self, agent_id):
        versions = self.store.list_versions(agent_id)
        if not versions:
            raise DefinitionNotFound(f"에이전트 '{agent_id}'에 버전이 없습니다", agent_id)
        return versions[-1]

    def _version(self, agent_id, version):
        """An existing version number: the given one or the newest."""
        if version is None:
            return self._newest(agent_id).version
        return self.store.version_info(agent_id, version).version

    def _known_tools(self, names: list[str]) -> None:
        catalog = self.tool_catalog()
        if catalog is None:
            raise OperationError("등록된 도구 목록을 읽지 못해 도구를 정할 수 없습니다. 개발자에게 알리세요")
        seen, unknown = set(), []
        for name in names:
            if name in seen:
                raise OperationError(f"도구 '{name}'가 두 번 들어 있습니다")
            seen.add(name)
            if name not in catalog:
                unknown.append(name)
        if unknown:
            raise OperationError(
                f"등록되지 않은 도구입니다: {', '.join(unknown)}. 도구를 지어낼 수 없습니다. 등록된 도구: "
                f"{', '.join(sorted(catalog)) or '없음'}. 필요한 동작을 하는 도구가 없으면 담당자에게 "
                "'개발자가 그 도구를 먼저 등록해야 한다'고 알리세요")

    def _modify(self, turn: Turn, agent_id: str, mutate: Callable[[AgentDefinition], str]) -> str:
        """Applies `mutate` to the agent's draft. The newest version is edited when it is a draft;
        otherwise a draft is created (from the newest version when that one is tested but not yet
        deployed, else from the stable version) - but only after the change has passed validation."""
        newest = self._newest(agent_id)
        if newest.state == "draft":
            base_version, create = newest.version, False
        else:
            create = True
            if newest.state in CARRY_OVER_STATES:
                base_version = newest.version
            else:
                base_version = self.store.deployment(agent_id).stable or newest.version
        before = self.store.load(agent_id, base_version)
        after = before.model_copy(deep=True)
        summary = mutate(after)
        # model_copy keeps objects that were assigned as plain dicts; a round trip normalises them
        after = validate_definition(AgentDefinition.model_validate(after.model_dump(mode="json")))
        if after.model_dump(mode="json") == before.model_dump(mode="json"):
            raise OperationError("이미 그 내용입니다. 바뀐 것이 없어 저장하지 않았습니다")
        if create:
            version = self.store.create_draft(agent_id, base_version, CREATED_BY, note=summary)
            after = after.model_copy(update={"version": version})
            note = summary
        else:
            version = base_version
            old_note = newest.note if newest.created_by == CREATED_BY else ""
            note = (old_note + "; " + summary if old_note else summary)[-300:]
        info = self.store.save_draft(after, note=note)
        turn.touched(agent_id, version, create, summary)
        removed = turn.removed.setdefault((agent_id, version), set())
        removed |= removed_values(before, after)
        stale = stale_places(after, removed)
        short = summary if len(summary) <= 60 else summary[:59] + "…"
        text = (f"저장됨: {agent_id} 초안 v{version}{f' (v{base_version} 바탕으로 새로 만듦)' if create else ''}. {short}. "
                f"바뀐 항목: {', '.join(info.changed_fields) or '없음'}. 안전 규칙 변경: {'예' if info.safety_change else '아니오'}.")
        for value, places in stale.items():
            text += (f"\n주의: 값 {value}이(가) 아직 남아 있는 항목: {', '.join(places)}. "
                     "요청과 어긋나면 이 항목들도 고치세요.")
        return text

    # ---------------------------------------------------------------- reading

    def _op_read_definition(self, turn, agent_id, version=None, part="summary"):
        version = self._version(agent_id, version)
        return summarize(self.store.load(agent_id, version), self.store.version_info(agent_id, version), part)

    def _op_list_versions(self, turn, agent_id):
        versions = self.store.list_versions(agent_id)
        deployment = self.store.deployment(agent_id)
        lines = [f"{agent_id}: 배포(stable) v{deployment.stable}" if deployment.stable else f"{agent_id}: 배포된 버전 없음"]
        if deployment.canary:
            lines[0] += f", 일부 배포(canary) v{deployment.canary} {deployment.canary_percent}%"
        for info in versions:
            line = f"- v{info.version} {STATE_LABEL.get(info.state, info.state)}"
            if info.based_on:
                line += f", v{info.based_on} 바탕"
            if info.created_by:
                line += f", 만든 이 {'제작 에이전트' if info.created_by == CREATED_BY else info.created_by}"
            if info.changed_fields:
                line += f", 바뀐 항목 {', '.join(info.changed_fields)}"
            if info.safety_change:
                line += ", 안전 규칙 변경"
            if info.test_run_id:
                line += f", 시험 실행 {info.test_run_id}"
            if info.note:
                line += f" · {info.note[:80]}"
            lines.append(line)
        return "\n".join(lines)

    # ---------------------------------------------------------------- creating

    def _op_create_agent(self, turn, id, name, persona, tools, flow, prohibitions, success, extract, personas,
                         description="", language="ko", reply_length="short", handoff=None, no_handoff=False):
        if turn.user_turns < 2:
            raise OperationError(
                "아직 담당자에게 되묻지 않았습니다. 지금은 만들지 말고 먼저 물어보세요: (1) 에이전트가 하는 일과 하지 않는 일 "
                "(2) 외부 시스템에서 해야 하는 동작과 그중 무엇이 변경인지 (3) 개인정보를 다룬다면 고객 본인 확인 방법 "
                "(4) 사람에게 넘기는 조건. 답을 받은 뒤 create_agent를 다시 호출하세요")
        if not ID_RE.match(id):
            raise OperationError(f"id '{id}'는 쓸 수 없습니다. 영문 소문자, 숫자, 하이픈만 씁니다 (예: hotel-booking)")
        if id in self._agents():
            raise OperationError(f"'{id}'는 이미 있는 에이전트입니다. 고치려면 변경 연산을 쓰고, 새로 만들려면 다른 id를 쓰세요")
        handoff = handoff or []
        missing = []
        if len(persona.strip()) < 30:
            missing.append("persona가 너무 짧습니다(역할, 말투, 하는 일과 하지 않는 일을 적으세요)")
        if not flow:
            missing.append("flow(지켜야 할 절차)가 비어 있습니다")
        if not prohibitions:
            missing.append("prohibitions(금지 사항)가 비어 있습니다")
        if not handoff and not no_handoff:
            missing.append("handoff(사람에게 넘기는 조건)가 비어 있습니다. 담당자가 넘기지 않기로 정했다면 no_handoff=true")
        if len(success) < 2:
            missing.append("success(성공 기준)가 2개 이상 필요합니다")
        if not any(item.get("safety") for item in success):
            missing.append("success에 안전 기준(safety: true)이 하나 이상 필요합니다")
        if not extract:
            missing.append("extract(뽑을 정보)가 비어 있습니다")
        if len(personas) < 2:
            missing.append("personas(가상 고객)가 2명 이상 필요합니다")
        if missing:
            raise OperationError("정의서가 완성되지 않았습니다: " + "; ".join(missing))
        bad = [f"{what} '{value}'" for what, values in (
            ("flow의 id", [item.get("id") for item in flow]), ("handoff의 id", [item.get("id") for item in handoff]),
            ("success의 id", [item.get("id") for item in success]), ("extract의 name", [item.get("name") for item in extract]),
            ("personas의 id", [item.get("id") for item in personas])) for value in values if not ITEM_ID_RE.match(value or "")]
        if bad:
            raise OperationError(
                f"영문이 아닌 이름이 있습니다: {', '.join(bad[:6])}. id와 name은 영문 소문자, 숫자, 밑줄로 적습니다 "
                "(예: order_id, verify_first). 담당자에게 묻지 말고 직접 영문 이름을 지어 같은 내용으로 다시 호출하세요")
        self._known_tools(tools)
        catalog = self.tool_catalog() or {}
        added_handoff_tool = bool(handoff and HANDOFF_TOOL in catalog and HANDOFF_TOOL not in tools)
        if added_handoff_tool:      # without it the runtime cannot hand off at all
            tools = [*tools, HANDOFF_TOOL]
        data = {"id": id, "name": name, "description": description, "persona": persona, "language": language,
                "reply_length": reply_length, "tools": tools, "flow": flow, "handoff": handoff,
                "prohibitions": prohibitions, "evaluation": {"success": success, "extract": extract},
                "tests": {"personas": personas}}
        definition = definition_from_parts(data, id)
        info = self.store.create_agent(definition, CREATED_BY)
        turn.new_agents.append(id)
        turn.touched(id, info.version, True, "새 에이전트")
        return (f"새 에이전트 '{id}'의 초안 v{info.version}을 만들었습니다. 도구 {len(tools)}개, 흐름 규칙 {len(flow)}개, "
                f"넘김 조건 {len(handoff)}개, 금지 사항 {len(prohibitions)}개, 성공 기준 {len(success)}개, "
                f"가상 고객 {len(personas)}명. 배포된 것은 아닙니다. 시험과 사람의 승인을 거쳐야 합니다."
                + (f" 넘김 조건이 있어 tools에 {HANDOFF_TOOL}을 더했습니다." if added_handoff_tool else ""))

    def _op_create_draft(self, turn, agent_id, based_on=None):
        newest = self._newest(agent_id)
        if newest.state == "draft":
            turn.default_agent = agent_id
            return (f"{agent_id}에는 이미 초안 v{newest.version}이 있습니다. 새로 만들지 않았습니다. "
                    "변경 연산은 이 초안에 적용됩니다.")
        if based_on is None and newest.state in CARRY_OVER_STATES:
            based_on = newest.version
        if based_on is not None:
            self.store.version_info(agent_id, based_on)
        version = self.store.create_draft(agent_id, based_on, CREATED_BY, note="")
        info = self.store.version_info(agent_id, version)
        turn.touched(agent_id, version, True, "")
        return f"{agent_id} v{version} 초안을 v{info.based_on} 바탕으로 만들었습니다. 아직 바뀐 내용은 없습니다."

    # ---------------------------------------------------------------- editing

    SCALARS = {"name": str, "description": str, "persona": str, "language": str, "reply_length": str,
               "live_clock": bool, "latency_budget_ms": int, "voice.tts_voice": str, "voice.stt": str,
               "voice.endpoint": str, "voice.interrupt_mode": str, "voice.barge_in": bool,
               "voice.speculative": bool, "llm.model": str, "llm.num_ctx": int, "llm.num_predict": int}

    def _op_set_field(self, turn, agent_id, field, value):
        field = field.strip()
        if field == "evaluation.latency_budget_ms":
            field = "latency_budget_ms"
        if field not in self.SCALARS:
            hint = ""
            if field in ("tools", "flow", "handoff", "prohibitions", "guards", "success", "extract", "personas"):
                hint = f" '{field}'는 전용 연산(add_/remove_/set_tools)으로 고칩니다."
            raise OperationError(f"'{field}'는 set_field로 바꿀 수 없습니다.{hint} 바꿀 수 있는 항목: {', '.join(self.SCALARS)}")
        kind = self.SCALARS[field]
        schema = {"type": {str: "string", bool: "boolean", int: "integer"}[kind]}
        value = _coerce(value, schema, "value")
        if kind is str and not value.strip():
            raise OperationError("value가 비어 있습니다")

        def mutate(d: AgentDefinition):
            data = d.model_dump(mode="json")
            if field == "reply_length" and d.reply_length == value:
                raise OperationError(
                    f"reply_length는 이미 {value}입니다. 바뀐 것이 없습니다. 말투나 간결함을 바꾸려는 것이면 되묻지 말고 "
                    "지금 persona를 요청에 맞게 새로 써서 set_field(field=persona)를 호출하세요")
            if field == "latency_budget_ms":
                data["evaluation"]["latency_budget_ms"] = value
            elif "." in field:
                group, key = field.split(".")
                data[group][key] = value
            else:
                data[field] = value
            new = _model(AgentDefinition, data)
            for name in AgentDefinition.model_fields:
                setattr(d, name, getattr(new, name))
            shown = value if not isinstance(value, str) or len(value) <= 60 else value[:59] + "…"
            return f"{field} = {shown}"

        return self._modify(turn, agent_id, mutate)

    def _op_add_flow_rule(self, turn, agent_id, id, rule):
        new = FlowRule(id=_item_id(id), rule=rule.strip())

        def mutate(d):
            return f"흐름 규칙 {id} {'바꿈' if _upsert(d.flow, new, 'id') else '더함'}: {new.rule}"
        return self._modify(turn, agent_id, mutate)

    def _op_remove_flow_rule(self, turn, agent_id, id):
        def mutate(d):
            kept = [rule for rule in d.flow if rule.id != id]
            if len(kept) == len(d.flow):
                raise OperationError(f"흐름 규칙 '{id}'가 없습니다. 있는 규칙: {', '.join(r.id for r in d.flow) or '없음'}")
            d.flow = kept
            return f"흐름 규칙 {id} 지움"
        return self._modify(turn, agent_id, mutate)

    def _op_add_prohibition(self, turn, agent_id, text, replaces=None):
        text = text.strip()

        def mutate(d):
            if replaces is not None:
                if not 1 <= replaces <= len(d.prohibitions):
                    raise OperationError(f"replaces={replaces}: 금지 사항은 1번부터 {len(d.prohibitions)}번까지 있습니다")
                d.prohibitions[replaces - 1] = text
                return f"금지 사항 {replaces}번 바꿈: {text}"
            if text in d.prohibitions:
                raise OperationError("같은 금지 사항이 이미 있습니다")
            d.prohibitions.append(text)
            return f"금지 사항 더함: {text}"
        return self._modify(turn, agent_id, mutate)

    def _op_add_handoff_rule(self, turn, agent_id, id, when):
        new = HandoffRule(id=_item_id(id), when=when.strip())

        def mutate(d):
            replaced = _upsert(d.handoff, new, "id")
            return f"넘김 조건 {id} {'바꿈' if replaced else '더함'}: {new.when}"
        text = self._modify(turn, agent_id, mutate)
        catalog = self.tool_catalog() or {}
        version = max(v for (a, v) in turn.changes if a == agent_id)
        if HANDOFF_TOOL in catalog and HANDOFF_TOOL not in self.store.load(agent_id, version).tools:
            text += f"\n주의: 이 에이전트의 tools에 {HANDOFF_TOOL}이 없어 실제로 넘길 수 없습니다. set_tools로 더하세요."
        return text

    def _op_add_guard(self, turn, agent_id, id, pattern, nudge, kind="regex", unless_tools=None):
        try:
            re.compile(pattern)
        except re.error as error:
            raise OperationError(f"pattern이 올바른 정규식이 아닙니다: {error}") from None
        new = _model(Guard, {"id": _item_id(id), "kind": kind, "pattern": pattern, "nudge": nudge,
                             "unless_tools": unless_tools or []})

        def mutate(d):
            return f"검사 규칙 {id} {'바꿈' if _upsert(d.guards, new, 'id') else '더함'}"
        return self._modify(turn, agent_id, mutate)

    def _op_set_tools(self, turn, agent_id, tools):
        self._known_tools(tools)

        def mutate(d):
            added = [name for name in tools if name not in d.tools]
            removed = [name for name in d.tools if name not in tools]
            d.tools = list(tools)
            return "도구 목록 바꿈" + (f", 더함: {', '.join(added)}" if added else "") \
                + (f", 뺌: {', '.join(removed)}" if removed else "")
        return self._modify(turn, agent_id, mutate)

    def _op_add_success_criterion(self, turn, agent_id, id, rule, safety=None, judge=None, metric=None,
                                  op=None, threshold=None):
        given = {"safety": safety, "judge": judge, "metric": metric, "op": op, "threshold": threshold}
        _item_id(id)

        def mutate(d):
            old = next((c for c in d.evaluation.success if c.id == id), None)
            data = old.model_dump() if old else {}        # what is not given keeps its old value
            data.update({"id": id, "rule": rule.strip()}, **{k: v for k, v in given.items() if v is not None})
            if data.get("judge", "llm") == "llm":
                data.update(metric=None, op=None, threshold=None)
            new = _model(SuccessCriterion, data)
            replaced = _upsert(d.evaluation.success, new, "id")
            flag = ""
            if old is not None and old.safety != new.safety:      # never a silent change
                flag = f" [안전 기준 표시 {'아니오→예' if new.safety else '예→아니오'}]"
            return f"성공 기준 {id}{' (안전)' if new.safety else ''} {'바꿈' if replaced else '더함'}{flag}: {new.rule}"
        return self._modify(turn, agent_id, mutate)

    def _op_add_extract_field(self, turn, agent_id, name, type=None, values=None, description=None, pii=None):
        given = {"type": type, "values": values, "description": description, "pii": pii}
        _item_id(name, "name")

        def mutate(d):
            old = next((f for f in d.evaluation.extract if f.name == name), None)
            data = old.model_dump() if old else {}
            data.update({"name": name}, **{k: v for k, v in given.items() if v is not None})
            new = _model(ExtractField, data)
            replaced = _upsert(d.evaluation.extract, new, "name")
            return f"뽑을 정보 {name}{' (개인정보)' if new.pii else ''} {'바꿈' if replaced else '더함'}"
        return self._modify(turn, agent_id, mutate)

    def _op_add_persona(self, turn, agent_id, id, description, goal, facts=None, opening=None, max_turns=None,
                        expect=None):
        for key, verdict in (expect or {}).items():
            if not isinstance(verdict, bool):
                raise OperationError(f"expect.{key}: true 또는 false여야 합니다")
        given = {"facts": facts, "opening": opening, "max_turns": max_turns, "expect": expect}
        _item_id(id)

        def mutate(d):
            old = next((p for p in d.tests.personas if p.id == id), None)
            data = old.model_dump() if old else {}
            data.update({"id": id, "description": description, "goal": goal},
                        **{k: v for k, v in given.items() if v is not None})
            new = _model(TestPersona, data)
            known = {criterion.id for criterion in d.evaluation.success}
            unknown = [key for key in new.expect if key not in known]
            if unknown:
                raise OperationError(f"expect의 '{unknown[0]}'는 성공 기준 id가 아닙니다. 있는 기준: {', '.join(sorted(known))}")
            return f"가상 고객 {id} {'바꿈' if _upsert(d.tests.personas, new, 'id') else '더함'}"
        return self._modify(turn, agent_id, mutate)

    # ---------------------------------------------------------------- tests and release request

    def _evaluator(self):
        if self.evaluator is None:
            raise OperationError("평가 모듈을 쓸 수 없어 시험을 다룰 수 없습니다")
        return self.evaluator

    async def _op_run_tests(self, turn, agent_id, version=None):
        version = self._version(agent_id, version)
        definition = self.store.load(agent_id, version)
        total = len(definition.tests.personas) + len(definition.tests.cases)
        if not total:
            raise OperationError(f"{agent_id} v{version}에는 가상 고객도 사례도 없어 시험할 것이 없습니다. add_persona로 먼저 더하세요")
        started = await self._evaluator().start(agent_id, version)
        run = {"run_id": started["run_id"], "agent_id": agent_id, "version": version,
               "total": started.get("total", total)}
        turn.runs.append(run)
        turn.last_run_id = run["run_id"]
        turn.default_agent = agent_id
        return (f"시험을 시작했습니다. 실행 번호 {run['run_id']}, {agent_id} v{version}, 항목 {run['total']}개. "
                "끝나려면 몇 분 걸립니다. 아직 결과가 없으니 결과를 지어내지 말고, 시험이 돌고 있으며 "
                "나중에 결과를 물어보면 알려 주겠다고 답하세요.")

    async def _run_id(self, turn, run_id, agent_id):
        if run_id:
            return run_id
        if turn.last_run_id:
            return turn.last_run_id
        if agent_id:
            runs = await self._evaluator().list_runs(agent_id)
            if runs:
                return runs[0]["run_id"]
        raise OperationError("어느 실행인지 알 수 없습니다. run_id를 주거나 먼저 run_tests를 호출하세요")

    async def _op_check_tests(self, turn, run_id=None, agent_id=None):
        run_id = await self._run_id(turn, run_id, agent_id)
        run = await self._evaluator().load(run_id)
        if run is None:
            raise OperationError(f"시험 실행 '{run_id}'를 찾을 수 없습니다")
        items = run.get("items") or []
        total = run.get("total") or len(items)
        status = run.get("status") or ("finished" if run.get("finished_at") else "running")
        head = f"실행 {run_id} ({run.get('agent_id')} v{run.get('version')}): "
        if status == "running":
            return head + f"아직 도는 중입니다. {len(items)}/{total} 항목 완료. 잠시 뒤 다시 확인하세요."
        if status in ("error", "interrupted"):
            return head + f"끝까지 돌지 못했습니다 ({status}). {len(items)}/{total} 항목만 완료. {run.get('error', '')}".rstrip()
        passed = sum(1 for item in items if item.get("passed"))
        lines = [head + f"끝났습니다. 통과 {passed}/{len(items)} (통과율 {round((run.get('pass_rate') or 0) * 100)}%), "
                        f"안전 기준 실패 {run.get('safety_failures', 0)}건."]
        for item in items:
            if item.get("passed"):
                continue
            reasons = []
            for criterion in item.get("criteria") or []:
                if criterion.get("passed") is False and (criterion.get("safety") or criterion.get("id") in (item.get("mismatches") or [])):
                    reasons.append(f"{criterion.get('id')}{'[안전]' if criterion.get('safety') else ''}: "
                                   f"{(criterion.get('reason') or '')[:120]}")
            mismatches = ", ".join(item.get("mismatches") or [])
            lines.append(f"- 실패 {item.get('item_id')} (기대와 다른 기준: {mismatches or '없음'})"
                         + ("".join("\n    " + reason for reason in reasons[:3])))
        if run.get("skipped"):
            lines.append(f"건너뛴 사례 {len(run['skipped'])}개.")
        try:
            info = self.store.version_info(run.get("agent_id"), run.get("version"))
            lines.append(f"이 버전의 상태: {STATE_LABEL.get(info.state, info.state)}.")
        except Exception:
            pass
        return "\n".join(lines)

    async def _op_compare_runs(self, turn, new_run_id=None, base_run_id=None, agent_id=None):
        evaluator = self._evaluator()
        new_run_id = await self._run_id(turn, new_run_id, agent_id)
        new = await evaluator.load(new_run_id)
        if new is None:
            raise OperationError(f"시험 실행 '{new_run_id}'를 찾을 수 없습니다")
        if (new.get("status") or "finished") == "running":
            raise OperationError(f"실행 {new_run_id}은 아직 도는 중입니다. 끝난 뒤 비교하세요")
        if not base_run_id:
            base_run_id, target = await self._base_run(new)
            if base_run_id is None:
                # nothing to compare with yet: start the suite of the base version instead of refusing
                started = await self._op_run_tests(turn, new.get("agent_id"), target)
                turn.last_run_id = new_run_id       # the run being judged stays the conversation's "last run"
                return (f"비교할 기준 실행이 없어 기준 버전 v{target}의 시험을 지금 시작했습니다. {started.split('. ', 1)[1]} "
                        f"기준 시험이 끝난 뒤 담당자가 다시 요청하면 compare_runs(new_run_id={new_run_id})를 호출하세요.")
        try:
            comparison = await evaluator.compare(base_run_id, new_run_id)
        except LookupError as error:
            raise OperationError(f"비교할 실행을 찾을 수 없습니다: {error}") from None
        c = comparison.model_dump() if hasattr(comparison, "model_dump") else dict(comparison)
        lines = [f"비교: 기준 {c['base_run']} → 새 {c['new_run']}",
                 f"좋아진 항목: {', '.join(c['improved']) or '없음'}",
                 f"나빠진 항목: {', '.join(c['regressed']) or '없음'}",
                 f"그대로 통과 {len(c['unchanged_pass'])}개, 그대로 실패: {', '.join(c['unchanged_fail']) or '없음'}"]
        changes = []
        for name, pair in (c.get("metric_changes") or {}).items():
            before, after = pair
            if before != after or name in ("pass_rate", "safety_failures"):
                changes.append(f"{name} {before:g} → {after:g}")
        if changes:
            lines.append("지표: " + "; ".join(changes[:12]))
        return "\n".join(lines)

    async def _base_run(self, new: dict):
        """(run id, None) of the run to compare against: the newest finished run of the version
        this one is based on, else of the stable version, else of any older version.
        (None, version) when there is no such run but a base version whose suite can be started."""
        agent_id, version = new.get("agent_id"), new.get("version")
        wanted = []
        try:
            info = self.store.version_info(agent_id, version)
            if info.based_on:
                wanted.append(info.based_on)
            stable = self.store.deployment(agent_id).stable
            if stable and stable != version and stable not in wanted:
                wanted.append(stable)
        except Exception:
            pass
        runs = [run for run in await self._evaluator().list_runs(agent_id)
                if run.get("run_id") != new.get("run_id") and (run.get("status") or "finished") == "finished"]
        for target in wanted:
            for run in runs:
                if run.get("version") == target:
                    return run["run_id"], None
        for run in runs:
            if run.get("version") is not None and version is not None and run["version"] < version:
                return run["run_id"], None
        if not wanted:
            raise OperationError("비교할 기준 실행이 없고, 기준으로 삼을 이전 버전도 없습니다")
        running = [run for run in await self._evaluator().list_runs(agent_id)
                   if run.get("version") == wanted[0] and run.get("status") == "running"]
        if running:
            raise OperationError(f"기준 버전 v{wanted[0]}의 시험({running[0]['run_id']})이 아직 도는 중입니다. 끝난 뒤 비교하세요")
        return None, wanted[0]

    def _op_submit_for_approval(self, turn, agent_id, version=None):
        version = self._version(agent_id, version)
        info = self.store.submit(agent_id, version)       # ReleaseError carries the refusal reason
        turn.submitted.append((agent_id, version))
        turn.default_agent = agent_id
        return (f"{agent_id} v{version}의 승인을 요청했습니다. 상태: {STATE_LABEL.get(info.state, info.state)}. "
                "승인과 배포는 사람이 제작실 화면에서 합니다. 제작 에이전트는 승인하거나 배포할 수 없습니다.")

    # ---------------------------------------------------------------- failed conversations (R-BLD-6)

    def _op_list_failed_sessions(self, turn, agent_id, limit=100):
        self.store.list_versions(agent_id)      # unknown agent -> DefinitionNotFound
        if self.trace_store is None:
            raise OperationError("대화 기록 저장소를 쓸 수 없습니다")
        store = self.trace_store()
        rows = store.list_sessions(agent=agent_id, limit=max(1, min(int(limit), 500)))
        analysed = [row for row in rows if row.get("analysis")]
        groups: dict[str, list[dict]] = {}
        for row in analysed:
            for criterion_id in row["analysis"].get("failed_ids") or []:
                groups.setdefault(criterion_id, []).append(row)
        if not groups:
            return (f"{agent_id}: 최근 대화 {len(rows)}개 중 분석된 것은 {len(analysed)}개이고, "
                    "성공 기준에 실패한 대화는 없습니다.")
        safety = set()
        try:
            newest = self.store.load(agent_id, self._newest(agent_id).version)
            safety = {criterion.id for criterion in newest.evaluation.success if criterion.safety}
        except Exception:
            pass
        lines = [f"{agent_id}: 최근 대화 {len(rows)}개 중 분석된 {len(analysed)}개를 실패한 기준별로 묶었습니다."]
        ordered = sorted(groups.items(), key=lambda pair: (pair[0] not in safety, -len(pair[1])))
        for criterion_id, sessions in ordered:
            versions = sorted({row.get("version") for row in sessions if row.get("version") is not None})
            lines.append(f"- {criterion_id}{' [안전]' if criterion_id in safety else ''}: {len(sessions)}건 "
                         f"(버전 {', '.join('v' + str(v) for v in versions)})")
            for row in sessions[:2]:
                reason = ""
                try:
                    document = store.get_document("analysis", row["session_id"]) or {}
                    for criterion in document.get("criteria") or []:
                        if criterion.get("id") == criterion_id:
                            reason = (criterion.get("reason") or "")[:140]
                except Exception:
                    pass
                label = f" {row.get('label')}" if row.get("label") else ""
                lines.append(f"    예: {row['session_id']}{label}" + (f" - {reason}" if reason else ""))
        return "\n".join(lines)
