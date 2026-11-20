"""BuilderAgent: a conversation with the person who owns an agent, in which an LLM edits the
definition through the operations of operations.py and nothing else (ARCHITECTURE.md section 10).

What the reply says about versions, changed fields and safety changes is appended by code from
the definition store (change_block); the model's own wording is never the source of those facts.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from collections import OrderedDict
from typing import Any, Optional

from .operations import MODIFYING, OPERATION_NAMES, STATE_LABEL, Operations, Turn, specs_for, stale_places
from .prompt import CLAIM_NUDGE, STALE_NUDGE, system_prompt

log = logging.getLogger("agentkit.builder")

MAX_ROUNDS = 8                 # generation passes per message (each may call operations)
HISTORY_MESSAGES = 10          # user/assistant messages kept from earlier turns
HISTORY_CHARS = 1200
TOKENS_PER_CHAR = 0.5          # measured with gemma4:26b on these prompts (Korean text and JSON specs)
REPLY_RESERVE = 900            # tokens kept free for the model's answer or its next call
# the person starts talking about a new agent
NEW_AGENT = re.compile(r"(새|새로운|신규)\s*(음성\s*)?(상담\s*)?에이전트|에이전트\S*\s*(하나\s*)?(새로\s*)?(만들|생성|제작)"
                       r"|(상담원|챗봇)\S{0,2}\s*(하나\s*)?(새로\s*)?(만들|생성)")
MAX_CONVERSATIONS = 200
NUM_CTX = int(os.environ.get("AGENTKIT_BUILDER_NUM_CTX", 8192))     # same as the runtime: no model reload
NUM_PREDICT = int(os.environ.get("AGENTKIT_BUILDER_NUM_PREDICT", 2500))

FIELD_LABEL = {
    "name": "이름", "description": "설명", "persona": "역할과 말투", "language": "언어", "reply_length": "답 길이",
    "live_clock": "날짜·시각 알림", "llm": "모델 설정", "voice": "음성 설정", "avatar": "아바타", "knowledge": "지식 검색",
    "tools": "도구", "flow": "흐름 규칙", "handoff": "넘김 조건", "prohibitions": "금지 사항", "guards": "검사 규칙",
    "evaluation.success": "성공 기준", "evaluation.extract": "뽑을 정보", "evaluation.latency_budget_ms": "지연 기준",
    "tests.personas": "가상 고객", "tests.cases": "저장된 사례",
    "safety_criteria": "안전 성공 기준", "pii_fields": "개인정보 추출 항목",
}
# the model says it did something although no operation succeeded in this turn
CLAIM = re.compile(r"(바꿨|바꾸었|변경했|변경하였|수정했|수정하였|추가했|추가하였|반영했|반영하였|적용했|적용하였|"
                   r"만들었|생성했|삭제했|지웠|제거했|저장했|시작했|요청했|넣었)(습니다|어요|고\b)|(변경|수정|반영|생성|추가|적용)\s?(완료|되었습니다|됐습니다)"
                   r"|시험\S{0,2}\s?(시작|실행)(합니다|하겠습니다|할게요)")
BLOCK_MARK = "[변경 기록"
BLOCK_HEAD = "[변경 기록 · 정의서 저장소 기준]"
UNBACKED_LINE = "주의: 위 답은 무언가를 더 고쳤다고 말하지만, 그 말에 해당하는 연산은 실행되지 않았습니다. 이 기록에 적힌 것만 실제로 바뀌었습니다."
NO_CHANGE_LINE = "[변경 기록 · 정의서 저장소 기준]\n이번 답에서 정의서는 바뀌지 않았습니다. 실행된 변경 연산이 없습니다."


class BuilderError(Exception):
    """The builder could not answer (the model server failed before anything was done)."""


class Conversation:
    def __init__(self, conversation_id: str):
        self.id = conversation_id
        self.agent_id: Optional[str] = None
        self.history: list[dict] = []       # {"role": "user"|"assistant", "content": str}
        self.user_turns = 0
        self.last_run_id: Optional[str] = None
        self.creating = False               # a new agent is being discussed (create mode)
        self.creating_turns = 0             # messages received in create mode
        self.lock = asyncio.Lock()

    def create_turns(self, mode: str) -> int:
        """How many messages the person has sent about the new agent, this one included.
        create_agent is refused on the first one: the builder has to ask before it creates."""
        if mode == "create":
            self.creating_turns += 1
            return self.creating_turns
        self.creating_turns = 0
        return self.user_turns


def _labels(fields):
    return ", ".join(FIELD_LABEL.get(field, field) for field in fields)


def _safety_parts(before, after) -> list[str]:
    try:
        from agentkit.definition.diffing import safety_changes
        return safety_changes(before, after)
    except Exception:
        return []


class BuilderAgent:
    """store: DefinitionStore (default: the configured agents directory).
    llm: agentkit.llm.LLM or anything with the same stream() surface.
    evaluator: evalbridge.EvalBridge or a fake (start, load, compare, list_runs).
    tool_catalog: callable -> {tool name: (effect, description)} | None; default reads v2/tools.
    trace_store: callable -> trace store, for list_failed_sessions; default agentkit.trace.get_store."""

    operation_names = list(OPERATION_NAMES)

    def __init__(self, store=None, llm=None, evaluator=None, tool_catalog=None, trace_store=None, model=None):
        if store is None:
            from agentkit.definition import DefinitionStore
            store = DefinitionStore()
        if llm is None:
            from agentkit.llm import LLM
            llm = LLM()
        if evaluator is None:
            from .evalbridge import EvalBridge
            evaluator = EvalBridge(store)
        self.store = store
        self.llm = llm
        self.model = model
        self.tool_catalog = tool_catalog or _default_tool_catalog
        self.operations = Operations(store, evaluator, self.tool_catalog, trace_store or _default_trace_store)
        self.conversations: "OrderedDict[str, Conversation]" = OrderedDict()

    # ---------------------------------------------------------------- conversations

    def conversation(self, conversation_id: Optional[str]) -> Conversation:
        if conversation_id and conversation_id in self.conversations:
            self.conversations.move_to_end(conversation_id)
            return self.conversations[conversation_id]
        conversation = Conversation(str(conversation_id) if conversation_id else uuid.uuid4().hex[:12])
        self.conversations[conversation.id] = conversation
        while len(self.conversations) > MAX_CONVERSATIONS:
            self.conversations.popitem(last=False)
        return conversation

    # ---------------------------------------------------------------- one message

    async def chat(self, message: str, agent_id: Optional[str] = None, conversation_id: Optional[str] = None) -> dict:
        message = (message or "").strip()
        if not message:
            raise ValueError("message is empty")
        conversation = self.conversation(conversation_id)
        async with conversation.lock:
            return await self._chat(conversation, message, agent_id)

    def _mode(self, conversation: Conversation, message: str, agent_id: Optional[str]) -> str:
        """Which prompt and operation set this message gets. Decided by code, not by the model:
        the full set does not fit the model's context window next to a definition."""
        known = set(self.operations._agents())
        mentioned = [name for name in sorted(known, key=len, reverse=True)
                     if re.search(rf"(?<![a-z0-9_-]){re.escape(name)}(?![a-z0-9_-])", message)]
        if agent_id:
            if agent_id != conversation.agent_id:
                conversation.creating = False
            conversation.agent_id = agent_id
        elif mentioned and not conversation.creating:
            conversation.agent_id = mentioned[0]        # "shop-support에서 ... 바꿔 줘" without a selection
        if NEW_AGENT.search(message) and not mentioned:
            conversation.creating = True
        if conversation.agent_id is None or conversation.agent_id not in known:
            if conversation.agent_id is not None and not conversation.creating:
                return "edit"       # an unknown id was selected: the prompt says so and the model asks
            conversation.creating = True
        return "create" if conversation.creating else "edit"

    def _fit(self, messages: list[dict], exchange: list[dict], specs) -> list[dict]:
        """messages + exchange, cut to what the context window holds: older history goes first,
        then the results of earlier operations in this turn are shortened to their first line."""
        budget = NUM_CTX - REPLY_RESERVE - int(len(json.dumps(specs or [], ensure_ascii=False)) * TOKENS_PER_CHAR)

        def size(items):
            return sum(int(len(str(m.get("content") or "")) * TOKENS_PER_CHAR)
                       + int(len(json.dumps(m.get("tool_calls") or "", ensure_ascii=False)) * TOKENS_PER_CHAR) + 8
                       for m in items)

        head, history, last = messages[:1], list(messages[1:-1]), messages[-1:]
        while history and size(head + history + last + exchange) > budget:
            del history[:2]
        exchange = [dict(m) for m in exchange]
        for entry in exchange[:-2]:
            if size(head + history + last + exchange) <= budget:
                break
            if entry.get("role") == "tool":
                entry["content"] = entry["content"].split("\n")[0][:80]
        return head + history + last + exchange

    async def _chat(self, conversation: Conversation, message: str, agent_id: Optional[str]) -> dict:
        mode = self._mode(conversation, message, agent_id)
        conversation.user_turns += 1
        turn = Turn(default_agent=None if mode == "create" else conversation.agent_id,
                    user_turns=conversation.create_turns(mode), last_run_id=conversation.last_run_id)
        try:
            catalog = self.tool_catalog()
        except Exception as error:
            log.warning("tool catalog unavailable: %s", error)
            catalog = None
        specs = specs_for(mode)
        allowed = [spec["function"]["name"] for spec in specs]
        system = system_prompt(self.store, catalog, conversation.agent_id, mode)
        status = await self._run_status(conversation) if mode == "edit" else ""
        messages = [{"role": "system", "content": system + ("\n\n" + status if status else "")}]
        messages += conversation.history[-HISTORY_MESSAGES:]
        messages.append({"role": "user", "content": message})
        exchange: list[dict] = []
        text, nudged, stale_nudged, failure = "", False, False, None
        mark, unbacked = 0, False       # successful acting operations at the last reminder; claim without one
        try:
            for round_index in range(MAX_ROUNDS):
                last = round_index == MAX_ROUNDS - 1
                text, calls = await self._generate(self._fit(messages, exchange, specs), None if last else specs)
                if calls and not last:      # the last pass offers no operations and runs none
                    exchange.append({"role": "assistant", "content": text,
                                     "tool_calls": [{"function": dict(call)} for call in calls]})
                    for call in calls:
                        ok, result = await self.operations.execute(turn, call["name"], call["arguments"], allowed)
                        exchange.append({"role": "tool", "tool_name": call["name"], "content": result})
                    continue
                stale = self._stale(turn)
                if stale and not stale_nudged and not last:
                    stale_nudged = True     # a value was changed in one place and left in another: one reminder
                    mark = self._acted(turn)
                    places = "; ".join(f"{value}: {', '.join(where)}" for value, where in stale.items())
                    if text.strip():
                        exchange.append({"role": "assistant", "content": text})
                    exchange.append({"role": "user", "content": STALE_NUDGE.format(places=places)})
                    continue
                unbacked = self._acted(turn) == mark and bool(CLAIM.search(text))
                if unbacked and not nudged and not last:
                    nudged = True       # claimed a change without making one: one chance to do it or take it back
                    exchange.append({"role": "assistant", "content": text})
                    exchange.append({"role": "user", "content": CLAIM_NUDGE})
                    continue
                break
        except Exception as error:
            if not turn.acted:
                raise BuilderError(f"모델 서버가 답하지 못했습니다: {error}") from error
            failure = error     # something was already changed: the person must still be told what
            log.warning("builder generation failed after operations: %s", error)

        # the model may imitate the block it saw in earlier replies: only the code writes that block
        prose = text.split(BLOCK_MARK)[0].strip()
        if failure is not None:
            prose = f"연산을 실행한 뒤 답을 끝까지 만들지 못했습니다 ({str(failure)[:160]}). 실행된 내용은 아래와 같습니다."
        elif not prose:
            prose = ("요청을 처리했습니다. 실행된 내용은 아래와 같습니다." if turn.acted
                     else "답을 만들지 못했습니다. 다시 한 번 말씀해 주세요.")
        block, facts = self.change_block(turn)
        if failure is None and unbacked:
            if not turn.acted:
                block = NO_CHANGE_LINE
            else:
                block += "\n" + UNBACKED_LINE
        reply = prose + ("\n\n" + block if block else "")

        if turn.new_agents:
            conversation.agent_id, conversation.creating = turn.new_agents[-1], False
        elif turn.default_agent and mode == "edit":
            conversation.agent_id = turn.default_agent
        conversation.last_run_id = turn.last_run_id
        conversation.history.append({"role": "user", "content": message[:HISTORY_CHARS]})
        conversation.history.append({"role": "assistant", "content": reply[:HISTORY_CHARS]})
        del conversation.history[:-HISTORY_MESSAGES]

        return {
            "reply": reply,
            "conversation_id": conversation.id,
            "mode": mode,
            "agent_id": facts.get("agent_id") or conversation.agent_id,
            "version": facts.get("version"),
            "state": facts.get("state"),
            "based_on": facts.get("based_on"),
            "safety_change": bool(facts.get("safety_change")),
            "safety_change_vs_deployed": facts.get("safety_change_vs_deployed"),
            "changed_fields": facts.get("changed_fields", []),
            "changed": bool(turn.changes),
            "changes": facts.get("changes", []),
            "run_id": turn.runs[-1]["run_id"] if turn.runs else None,
            "runs": list(turn.runs),
            "submitted": [{"agent_id": a, "version": v} for a, v in turn.submitted],
            "operations": [{"name": entry["name"], "arguments": entry["arguments"], "ok": entry["ok"],
                            "result": entry["result"]} for entry in turn.log],
        }

    @staticmethod
    def _acted(turn: Turn) -> int:
        """How many operations that change, test or submit something have succeeded in this turn."""
        return sum(1 for entry in turn.log
                   if entry["ok"] and entry["name"] in MODIFYING + ("run_tests", "submit_for_approval"))

    async def _run_status(self, conversation: Conversation) -> str:
        """One line about the suite this conversation started last, read from the evaluator."""
        if not conversation.last_run_id:
            return ""
        try:
            run = await self.operations.evaluator.load(conversation.last_run_id)
        except Exception:
            run = None
        if not run:
            return ""
        items = run.get("items") or []
        status = run.get("status") or ("finished" if run.get("finished_at") else "running")
        head = f"[이 대화에서 마지막으로 시작한 시험] 실행 번호 {run.get('run_id')} ({run.get('agent_id')} v{run.get('version')}): "
        if status == "running":
            return head + f"아직 도는 중, {len(items)}/{run.get('total') or '?'} 항목 완료. 결과는 아직 없습니다."
        if status != "finished":
            return head + f"끝까지 돌지 못함 ({status})."
        passed = sum(1 for item in items if item.get("passed"))
        return head + (f"끝남. 통과 {passed}/{len(items)}, 안전 기준 실패 {run.get('safety_failures', 0)}건. "
                       "실패 항목과 이유는 check_tests로 봅니다.")

    def _stale(self, turn: Turn) -> dict[str, list[str]]:
        """Values replaced in this turn that other sentences of the same draft still carry."""
        out: dict[str, list[str]] = {}
        for (agent_id, version), values in turn.removed.items():
            if not values:
                continue
            try:
                found = stale_places(self.store.load(agent_id, version), values)
            except Exception:
                continue
            for value, places in found.items():
                out.setdefault(value, []).extend(places)
        return out

    async def _generate(self, messages, tools) -> tuple[str, list[dict]]:
        text, calls = "", []
        kwargs: dict[str, Any] = {"tools": tools, "num_predict": NUM_PREDICT, "num_ctx": NUM_CTX, "temperature": 0.2}
        if self.model:
            kwargs["model"] = self.model
        async for kind, value in self.llm.stream(messages, **kwargs):
            if kind == "text":
                text += value
            elif kind == "tool":
                calls.append({"name": value.get("name", ""), "arguments": value.get("arguments") or {}})
        return text, calls

    # ---------------------------------------------------------------- the code-generated block

    def change_block(self, turn: Turn) -> tuple[str, dict]:
        """(text appended to the reply, facts for the response). Everything is read back from the
        store after the operations ran."""
        lines: list[str] = []
        facts: dict[str, Any] = {"changes": []}
        for (agent_id, version), entry in turn.changes.items():
            try:
                info = self.store.version_info(agent_id, version)
                stable = self.store.deployment(agent_id).stable
            except Exception as error:
                lines.append(f"{agent_id} v{version}: 저장소에서 읽지 못했습니다 ({str(error)[:120]})")
                continue
            safety_parts, versus_stable, stable_fields, stable_parts = [], None, [], []
            definition = None
            try:
                definition = self.store.load(agent_id, version)
                if info.based_on is not None:
                    safety_parts = _safety_parts(self.store.load(agent_id, info.based_on), definition)
                if stable is not None and stable != version and stable != info.based_on:
                    deployed = self.store.load(agent_id, stable)
                    stable_fields = list(self.store.diff(agent_id, stable, version))
                    stable_parts = _safety_parts(deployed, definition)
                    versus_stable = bool(stable_parts)
            except Exception as error:
                log.warning("could not compare %s v%s: %s", agent_id, version, error)
            state = STATE_LABEL.get(info.state, info.state)
            if info.based_on is None:
                lines.append(f"{agent_id} v{version} ({state}): 새 에이전트의 첫 버전" if entry["created"]
                             else f"{agent_id} v{version} ({state}): 있던 초안을 이어서 고침")
                lines.append(f"채워진 항목: {_labels(info.changed_fields) or '없음'}")
                lines.append("안전 규칙: " + ("도구, 넘김 조건, 금지 사항 같은 안전 규칙이 새로 정해졌습니다. 승인 전에 사람이 확인해야 합니다"
                                          if info.safety_change else "안전 규칙에 해당하는 항목이 없습니다"))
            else:
                lines.append(f"{agent_id} v{version} ({state}, v{info.based_on} 바탕): "
                             + ("이번에 새로 만든 초안" if entry["created"] else "있던 초안을 이어서 고침"))
                lines.append(f"바뀐 항목(v{info.based_on} 대비): {_labels(info.changed_fields) or '없음'}")
                lines.append("안전 규칙 변경: " + (f"예 ({_labels(safety_parts) or '안전 규칙'})" if info.safety_change else "아니오"))
                if versus_stable is not None:
                    lines.append(f"운영 중인 v{stable}과 비교하면 바뀐 항목: {_labels(stable_fields) or '없음'}, "
                                 "안전 규칙 변경: " + (f"예 ({_labels(stable_parts)})" if versus_stable else "아니오"))
            done = "; ".join(op if len(op) <= 90 else op[:89] + "…" for op in entry["ops"])
            if done:
                lines.append(f"이번에 한 일: {done}")
            stale = stale_places(definition, turn.removed.get((agent_id, version), ())) if definition else {}
            for value, places in stale.items():
                lines.append(f"확인 필요: 바꾼 값 {value}이(가) 아직 남아 있는 항목: {', '.join(places)}")
            change = {"agent_id": agent_id, "version": version, "state": info.state, "based_on": info.based_on,
                      "created": entry["created"], "changed_fields": list(info.changed_fields),
                      "safety_change": bool(info.safety_change or versus_stable),
                      "safety_change_vs_based_on": bool(info.safety_change),
                      "safety_change_vs_deployed": versus_stable, "safety_fields": safety_parts,
                      "stale_values": stale, "summary": list(entry["ops"])}
            facts["changes"].append(change)
            facts.update({key: change[key] for key in ("agent_id", "version", "state", "based_on", "changed_fields",
                                                       "safety_change", "safety_change_vs_deployed")})
        if turn.changes:
            lines.append("운영 중인 버전은 바뀌지 않았습니다. 초안은 시험과 사람의 승인을 거쳐야 배포됩니다.")
        for run in turn.runs:
            lines.append(f"시험 실행 시작: {run['run_id']} ({run['agent_id']} v{run['version']}, 항목 {run['total']}개). "
                         "지금 돌고 있으며 몇 분 걸립니다. 결과는 아직 없습니다.")
        for agent_id, version in turn.submitted:
            try:
                state = self.store.version_info(agent_id, version).state
            except Exception:
                state = "?"
            lines.append(f"승인 요청: {agent_id} v{version} → 상태 {STATE_LABEL.get(state, state)}. 승인과 배포는 사람이 합니다.")
        failed = [entry for entry in turn.log if not entry["ok"] and entry["name"] in MODIFYING + ("submit_for_approval", "run_tests")]
        if failed and not turn.acted:
            lines.append("실행되지 않은 연산: " + "; ".join(f"{entry['name']} - {entry['result'][:140]}" for entry in failed[-2:]))
            lines.append("정의서는 바뀌지 않았습니다.")
        if not lines:
            return "", facts
        return BLOCK_HEAD + "\n" + "\n".join(lines), facts


def _default_tool_catalog():
    """{name: (effect, description)} from the tool contracts, or None when they cannot be read."""
    try:
        from agentkit.tools import load_contracts
        contracts = load_contracts()
        # an agent writes bare names (looked up in its tool_packs); the ids are accepted too
        known = {c.name: (c.effect, c.description) for c in reversed(list(contracts.values()))}
        known.update({key: (c.effect, c.description) for key, c in contracts.items()})
        return known
    except Exception as error:
        log.warning("tool contracts unavailable: %s", error)
        return None


def _default_trace_store():
    from agentkit.trace import get_store
    return get_store()
