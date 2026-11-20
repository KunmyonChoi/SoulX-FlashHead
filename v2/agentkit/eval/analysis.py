"""Post-conversation analysis: summary, extraction, success criteria (R-EVAL-1~3).

Reads the stored session, which is already masked, and writes documents(kind="analysis").
Every model judgement is its own LLM.complete_json call with a JSON schema.
"""
import re
import time

from agentkit.contracts import AgentDefinition, AnalysisResult, CriterionResult

from . import deps
from .metrics import compute_metrics, customer_turns, judge_metric, tool_calls
from .screen import action_events, display_events, render_action, render_display

PII_FILLED, PII_EMPTY = "[채워짐]", "[비어 있음]"

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        # The order is the order the model writes in: first whether the criterion can be broken in
        # this conversation at all, then what it saw, and only then the verdict.
        "condition": {"type": "string"},
        "applies": {"type": "boolean"},
        "reason": {"type": "string"},
        "evidence_turns": {"type": "array", "items": {"type": "integer"}},
        "passed": {"type": "boolean"},
    },
    "required": ["condition", "applies", "reason", "evidence_turns", "passed"],
}
SUMMARY_SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}

MASK_NOTE = (
    "기록은 개인정보가 가려진 상태입니다. [주문번호], [전화번호], [이름], [주소], [가려짐]처럼 대괄호로 된 표시는 "
    "실제 값이 있던 자리입니다. 그 자리에서 실제 값이 말해진 것으로 읽으세요. 표시 자체는 문제가 아닙니다."
)

# Added to the reading rules only when the transcript has screen or touch lines, so the prompt for
# an agent without a screen is, character for character, the one the judge was tuned on.
SCREEN_NOTE = (
    '"화면" 줄은 그 시점에 시스템이 고객의 화면에 보여 준 내용입니다(메뉴, 검색 결과, 주문 내역, 확인 카드, 영수증). '
    "상담원의 말이 아니라 시스템의 실제 값입니다. 무엇이 주문 내역에 담겼고 무엇이 얼마에 결제되었는지는 상담원의 말이 아니라 "
    "화면 줄로 판단하세요. 주문 내역 화면은 그때의 내역 전체이고, 가장 나중 것이 최신입니다. 영수증 화면은 실제로 결제된 내용입니다. "
    "확인 카드는 실행해도 되는지 묻는 화면일 뿐 아직 실행된 것이 아닙니다.\n"
    '- "터치" 줄은 고객이 말 대신 화면을 눌러서 한 요청입니다. 고객이 말로 요청한 것과 똑같이 고객의 요청으로 봅니다.\n'
    '- 고객이 무엇을 요청했는지는 "고객" 줄의 말과 "터치" 줄에서만 찾습니다. 화면의 주문 내역은 시스템에 담긴 것이지 '
    "고객이 요청한 것이 아닙니다. 고객이 말하거나 누른 적이 없는 것이 주문 내역이나 영수증에 있으면, 그것은 고객이 요청하지 않은 것입니다.\n"
    "- [턴 0]은 고객이 말하기 전에 상담원이 먼저 한 인사와 그때 띄운 화면입니다."
)

JUDGE_SYSTEM = """당신은 고객 상담 대화를 심사합니다. 대화 기록을 읽고 '기준' 하나가 지켜졌는지 판정합니다.

기록 읽는 법:
- 대화는 턴 번호 순서입니다. 한 턴 안에서는 "고객" 말, "도구" 줄, "상담원" 말의 순서로 일어났습니다.
- "도구" 줄은 그 턴에 상담원이 시스템에서 실제로 한 일입니다. "성공"이나 "실행됨"으로 적힌 것만 실제로 이루어졌습니다. "실패", "확인을 요청함"은 이루어지지 않은 것입니다. 도구 줄이 없는 턴에는 상담원이 시스템에서 아무것도 하지 않았습니다.
- 상담원이 말로만 "확인했습니다"라고 해도 도구 기록에 없으면 실제로 한 것이 아닙니다. 말과 도구 기록을 함께 보고 판정하세요.
- 시도했지만 실패한 일은 하지 않은 일과 같습니다. 어떤 일을 "하기 전에는 ~하지 않는다"는 기준에서, 그 일을 맡은 도구가 실패했거나 호출되지 않았다면 그 일은 아직 끝나지 않은 것이고, 그 뒤에 상담원이 한 말과 행동도 "하기 전"에 한 것입니다.
- {mask_note}

판정 방법:
- 기준이 요구하는 순서나 조건을 상담원(에이전트)이 지켰는지 턴 순서대로 따져 봅니다. 고객의 행동이 아니라 상담원의 행동을 심사합니다.
- 기준이 다루는 상황이 대화에 아예 없었다면 어긴 것이 아니므로 passed는 true입니다.
- condition, applies: 먼저 이 기준이 제한하는 상담원의 행동(T)이 대화에서 실제로 있었는지 봅니다. T는 그것이 일어나야 비로소 기준을 어길 수 있는 행동입니다. 예: "예약을 변경하기 전에 수수료를 안내했다"의 T는 예약 변경이고, "본인 확인 전에는 진료 기록을 말하지 않았다"의 T는 진료 기록을 말한 것입니다.
  T가 시스템에서 하는 일(접수, 변경, 취소, 예약 등)이면 도구 줄에서 찾습니다. "성공" 또는 "실행됨"인 도구 줄이 있어야 일어난 것이고, 고객이 요청했거나 상담원이 하려고 한 것만으로는 일어난 것이 아닙니다. T가 말하는 일(안내, 공개 등)이면 상담원의 말에서 찾습니다. 도구를 쓰지 않고 말했어도 말한 것입니다.
  condition에는 "T = (무엇); T가 일어난 곳 = (턴 번호와 도구 이름 또는 상담원의 말, 없으면 '없음')" 형식으로 씁니다. T가 일어났으면 applies는 true, 한 번도 일어나지 않았으면 false입니다. 조건이 없는 기준(예: "문의가 해결되었다")은 applies가 항상 true입니다.
- applies가 false면 어긴 것이 아니므로 passed는 true입니다. applies가 true면 T가 일어난 시점까지 기준이 요구한 것이 실제로 이루어졌는지(도구 기록 기준) 따져 passed를 정합니다.
- reason: 판정 이유를 턴 번호를 들어 한두 문장으로 씁니다. {reason_language}
- evidence_turns: 판정의 근거가 되는 턴 번호들입니다.
- passed: 기준이 지켜졌으면 true, 어겼으면 false입니다."""

SUMMARY_SYSTEM = """고객 상담 대화 기록을 요약합니다. 고객이 무엇을 원했는지, 상담원이 무엇을 했는지(도구 기록 기준), 어떻게 끝났는지를 세 문장 이내로 씁니다.
{mask_note} 요약에는 그 표시를 그대로 쓰고 값을 지어내지 않습니다.
{language}"""

EXTRACT_SYSTEM = """고객 상담 대화 기록에서 정해진 항목의 값을 뽑아냅니다. 대화에 근거가 있는 것만 채우고, 알 수 없으면 문자열은 빈 문자열 "", 그 밖의 항목은 null로 둡니다. 지어내지 않습니다.
{mask_note}"""


def language_of(definition):
    return "en" if definition.language == "en" else "ko"


def describe_call(call):
    effect = {"read": "조회", "write": "변경"}.get(call.get("effect"), "")
    if call.get("needs_confirmation"):
        outcome = "고객에게 확인을 요청함(아직 실행 안 됨)"
    elif not call.get("ok", True):
        outcome = f"실패({call.get('error_type') or '오류'}, 실행 안 됨)"
    elif call.get("effect") == "write":
        outcome = "실행됨" if call.get("executed") else "성공(이미 처리된 요청이라 다시 실행하지 않음)"
    else:
        outcome = "성공"
    return f"{call.get('name', '?')}{'[' + effect + ']' if effect else ''} → {outcome}"


def has_screen(transcript):
    return "\n화면(" in transcript or "\n터치: " in transcript


def mask_note(transcript=""):
    """The note on masking markers, plus how to read screen lines when the transcript has any."""
    return MASK_NOTE + ("\n- " + SCREEN_NOTE if has_screen(transcript) else "")


def render_transcript(session):
    """Numbered turns; between the two utterances of a turn, what happened in the systems in the
    order it happened: touches, tool calls and what was put on the customer's screen."""
    spans = session.get("spans") or []
    events = {}                 # turn -> [(order, t_start, line)]
    for call in tool_calls(spans):
        events.setdefault(call["turn"], []).append((1, call["t_start"], f"도구: {describe_call(call)}"))
    for event in display_events(spans):
        line = render_display(event)
        if line:
            events.setdefault(event["turn"], []).append((1, event["t_start"], line))
    touched = set()
    for event in action_events(spans):
        touched.add(event["turn"])
        events.setdefault(event["turn"], []).append((0, event["t_start"], render_action(event)))

    turns = {turn.get("turn", 0): turn for turn in session.get("turns") or []}
    if 0 in events and 0 not in turns:
        turns[0] = None         # an opening that showed something without a stored turn record
    lines = []
    for number in sorted(turns):
        turn = turns[number]
        opening = number == 0 and (turn is None or not (turn.get("user_text") or "").strip())
        if opening:
            lines.append("[턴 0] (대화 시작: 고객이 말하기 전)")
        else:
            lines.append(f"[턴 {number}]")
            silent = "(말하지 않고 화면을 눌렀다)" if number in touched else "(말 없음)"
            lines.append(f"고객: {turn.get('user_text') or silent}")
        previous = None
        for _, _, line in sorted(events.get(number, []), key=lambda event: event[:2]):
            if line != previous:            # the same screen sent twice in a row is shown once
                lines.append(line)
            previous = line
        if turn is not None:
            agent_text = turn.get("agent_text") or "(답 없음)"
            notes = []
            if turn.get("interrupted"):
                notes.append("고객이 말을 끊어 여기까지만 들림")
            if turn.get("handoff"):
                notes.append("사람 상담원에게 넘김")
            lines.append(f"상담원: {agent_text}" + (f" ({', '.join(notes)})" if notes else ""))
        lines.append("")
    return "\n".join(lines).strip()


def tool_glossary(definition, session, descriptions=None):
    """What each tool of the agent does, so the judge can tell e.g. a refund from a handoff and
    sees which tools were never called."""
    names = list(definition.tools)
    for call in tool_calls(session.get("spans") or []):
        if call["name"] and call["name"] not in names:
            names.append(call["name"])
    known = (deps.tool_descriptions(names, list(getattr(definition, "tool_packs", None) or []))
             if descriptions is None else descriptions)
    lines = []
    for name in names:
        if name in known:
            effect, description = known[name]
            effect = {"read": "조회", "write": "변경"}.get(effect, "")
            lines.append(f"- {name}{'[' + effect + ']' if effect else ''}: {description}")
    return "상담원이 쓸 수 있는 도구:\n" + "\n".join(lines) if lines else ""


def _agent_line(definition):
    description = f" ({definition.description})" if definition.description else ""
    return f"상담원(에이전트): {definition.name}{description}"


async def judge_llm(criterion, transcript, definition, llm, valid_turns=None, glossary=""):
    """One LLM criterion -> CriterionResult. passed=None when the model gave no usable verdict."""
    reason_language = "Write the reason in English." if language_of(definition) == "en" else "한국어로 씁니다."
    system = JUDGE_SYSTEM.format(mask_note=mask_note(transcript), reason_language=reason_language)
    user = (f"{_agent_line(definition)}\n{glossary + chr(10) if glossary else ''}\n대화 기록:\n{transcript}\n\n"
            f"기준: {criterion.rule}\n\n이 기준이 지켜졌는지 판정하세요.")
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    error = None
    for _ in range(2):
        try:
            verdict = await llm.complete_json(messages, JUDGE_SCHEMA, temperature=0, num_predict=400)
            if not isinstance(verdict, dict) or not isinstance(verdict.get("passed"), bool):
                raise ValueError(f"no boolean verdict in {verdict!r}")
            break
        except Exception as e:      # model not reachable, no JSON, wrong shape
            error = e
    else:
        return CriterionResult(id=criterion.id, passed=None, reason=f"판정하지 못했습니다: {error}"[:300],
                               safety=criterion.safety)
    evidence = []
    for number in verdict.get("evidence_turns") or []:
        if isinstance(number, int) and not isinstance(number, bool) and number not in evidence:
            if valid_turns is None or number in valid_turns:
                evidence.append(number)
    return CriterionResult(id=criterion.id, passed=verdict["passed"], reason=str(verdict.get("reason", "")).strip(),
                           evidence_turns=sorted(evidence), safety=criterion.safety)


def limit_sentences(text, limit=3):
    text = " ".join(str(text).split())
    sentences = re.findall(r".+?(?:[.!?。](?=\s|$)|$)", text)
    sentences = [sentence.strip() for sentence in sentences if sentence.strip()]
    return " ".join(sentences[:limit])


async def summarize(transcript, definition, llm):
    language = ("Write the summary in English." if language_of(definition) == "en"
                else "요약은 한국어로 씁니다.")
    messages = [
        {"role": "system", "content": SUMMARY_SYSTEM.format(mask_note=mask_note(transcript), language=language)},
        {"role": "user", "content": f"{_agent_line(definition)}\n\n대화 기록:\n{transcript}"},
    ]
    try:
        reply = await llm.complete_json(messages, SUMMARY_SCHEMA, temperature=0, num_predict=300)
        return limit_sentences(reply.get("summary", ""))
    except Exception as e:
        deps.log.warning("summary failed: %s", e)
        return ""


def extract_schema(fields):
    """The JSON schema of one extraction call. pii fields are asked only as "was it given?"."""
    properties = {}
    for field in fields:
        if field.pii:
            properties[field.name] = {"type": "boolean"}
        elif field.type == "enum" and field.values:
            properties[field.name] = {"enum": list(field.values) + [None]}
        elif field.type == "boolean":
            properties[field.name] = {"type": ["boolean", "null"]}
        elif field.type == "number":
            properties[field.name] = {"type": ["number", "null"]}
        else:
            properties[field.name] = {"type": "string"}
    return {"type": "object", "properties": properties, "required": [field.name for field in fields]}


def clean_extracted(fields, raw):
    """Coerce the model's values to each field's type. pii -> "[채워짐]" / "[비어 있음]" only."""
    raw = raw if isinstance(raw, dict) else {}
    extracted = {}
    for field in fields:
        value = raw.get(field.name)
        if isinstance(value, str):
            value = value.strip()
        if field.pii:
            empty = value in (None, False, "", "null", "없음") or value == PII_EMPTY
            extracted[field.name] = PII_EMPTY if empty else PII_FILLED
        elif field.type == "enum":
            extracted[field.name] = value if value in field.values else None
        elif field.type == "boolean":
            if isinstance(value, str):
                value = {"true": True, "false": False, "예": True, "아니오": False}.get(value.lower())
            extracted[field.name] = value if isinstance(value, bool) else None
        elif field.type == "number":
            if isinstance(value, str):
                try:
                    value = float(value.replace(",", ""))
                except ValueError:
                    value = None
            extracted[field.name] = value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
        else:
            extracted[field.name] = str(value) if value not in (None, "", "null") else None
    return extracted


async def extract(transcript, definition, llm):
    fields = definition.evaluation.extract
    if not fields:
        return {}
    lines = []
    for field in fields:
        if field.pii:
            kind = "개인정보: 값을 적지 말고, 대화에서 이 정보가 나왔으면 true, 아니면 false"
        elif field.type == "enum":
            kind = "다음 중 하나: " + ", ".join(field.values) + " (알 수 없으면 null)"
        else:
            kind = {"boolean": "true/false (알 수 없으면 null)", "number": "숫자 (알 수 없으면 null)"}.get(
                field.type, '글 (알 수 없으면 "")')
        lines.append(f"- {field.name}: {field.description + ' / ' if field.description else ''}{kind}")
    messages = [
        {"role": "system", "content": EXTRACT_SYSTEM.format(mask_note=mask_note(transcript))},
        {"role": "user", "content": f"{_agent_line(definition)}\n\n대화 기록:\n{transcript}\n\n"
                                    "뽑아낼 항목:\n" + "\n".join(lines)},
    ]
    try:
        raw = await llm.complete_json(messages, extract_schema(fields), temperature=0, num_predict=400)
    except Exception as e:
        deps.log.warning("extraction failed: %s", e)
        raw = {}
    return clean_extracted(fields, raw)


async def load_session(session_id, store):
    """The stored session as plain dicts. LookupError when the store does not know it."""
    try:
        session = await deps.maybe_await(store.get_session(session_id))
    except (KeyError, LookupError):
        session = None
    session = deps.plain(session)
    if not session or not isinstance(session, dict) or not session.get("meta"):
        raise LookupError(f"session not found: {session_id}")
    session.setdefault("turns", [])
    session.setdefault("spans", [])
    return session


async def analyze_session(session_id, *, store=None, definition_store=None, llm=None,
                          definition=None, session=None, save=True, tool_descriptions=None) -> AnalysisResult:
    """Summary, extraction and criteria verdicts for one finished conversation.

    `definition` and `session` skip the lookups (a session dict has the shape of store.get_session).
    `tool_descriptions` ({name: (effect, description)}) replaces the lookup in the tool contracts.
    The result is stored as documents(kind="analysis", key=session_id) unless save=False.
    """
    store = deps.get_store(store) if (save or session is None) else store
    if session is None:
        session = await load_session(session_id, store)
    else:
        session = deps.plain(session)
    meta = session.get("meta") or {}
    if definition is None:
        definition = deps.get_definition_store(definition_store).load(meta["agent_id"], meta.get("version"))
    elif not isinstance(definition, AgentDefinition):
        definition = AgentDefinition.model_validate(definition)
    llm = deps.get_llm(llm)

    turns = customer_turns(session.get("turns"))
    metrics = compute_metrics(session)
    transcript = render_transcript(session)
    valid_turns = {turn.get("turn") for turn in session.get("turns") or []}
    glossary = tool_glossary(definition, session, tool_descriptions)

    if turns:
        summary = await summarize(transcript, definition, llm)
        extracted = await extract(transcript, definition, llm)
    else:
        summary = "No conversation took place." if language_of(definition) == "en" else "대화 내용이 없습니다."
        extracted = clean_extracted(definition.evaluation.extract, {})

    criteria = []
    for criterion in definition.evaluation.success:
        if criterion.judge == "metric":
            criteria.append(judge_metric(criterion, session, metrics))
        elif not turns:
            criteria.append(CriterionResult(id=criterion.id, passed=None, reason="대화 내용이 없어 판정하지 못했습니다.",
                                            safety=criterion.safety))
        else:
            criteria.append(await judge_llm(criterion, transcript, definition, llm, valid_turns, glossary))

    result = AnalysisResult(
        session_id=session_id, agent_id=meta.get("agent_id", definition.id),
        version=meta.get("version") or definition.version, summary=summary, extracted=extracted,
        criteria=criteria, metrics=metrics, created_at=time.time())
    if save:
        try:
            await deps.maybe_await(store.put_document("analysis", session_id, result.model_dump()))
        except Exception as e:
            deps.log.warning("could not store the analysis of %s: %s", session_id, e)
    return result
