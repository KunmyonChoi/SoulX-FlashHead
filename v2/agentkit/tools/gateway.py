"""
The tool gateway: the only way a tool is called (contracts.ToolGateway, ARCHITECTURE.md section 4).

Order of one call:
  1. arguments are checked against the contract's input_schema        -> invalid_input
  2. the contract's `requires` facts must be set for the session      -> precondition_failed
  3. read tool:  called within timeout_s (one budget for all tries); a system that could not be
                 reached is tried again `retries` times                -> system_error / timeout
  4. write tool: never retried, and
       - a request that already ran returns the first result, executed=False
       - a speculative context never runs it                           -> not_confirmed
       - with a confirm_template it runs in two phases: the first call only proposes
         (needs_confirmation=True); the same call runs when it comes in the NEXT turn and the
         customer's utterance of that turn reads as consent            -> otherwise not_confirmed
  5. success sets the contract's `grants` facts
  6. the screens the customer is shown are put in ToolResult.display (docs/DISPLAY.md):
       - success: the contract's `display` (kind, and `path` into the data), then the displays of
         its `refresh` read tools, each called without arguments in the same session and turn
       - a write tool's proposal: {"kind": "confirm"} with the sentence, the tool and the preview
       - failure: nothing, unless the system sent data["display_notice"] -> {"kind": "notice"}
  7. every call becomes a Span(kind="tool"), handed to the recorder with the pii paths
     (a refresh call is a span of its own, attrs.refresh_of names the tool that caused it)

The model is not trusted with any of this: it may call a write tool whenever it likes, and the
rules above decide what happens.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path

import httpx
import yaml

from ..contracts import AdapterSpec, Span, ToolContext, ToolContract, ToolResult
from .adapters import Adapter, AdapterError, AdapterUnavailable, Reply, build_adapter
from .consent import read_consent
from .loader import PLACEHOLDER, ContractError, UnknownTool, check_contract, check_refresh, load_contracts, resolve, sibling
from .validate import InputError, validate

log = logging.getLogger("agentkit.tools")

MASK = "[가려짐]"
NUMBERED = re.compile(r"^\s*\d+\. ", re.M)

# What the model is told when a system gives no sentence of its own.
DEFAULT_TEXT = {
    "not_found": "요청한 대상을 찾지 못했습니다. 고객에게 정보를 다시 확인해 달라고 하세요.",
    "permission_denied": "이 요청은 허용되지 않아 처리하지 않았습니다. 고객에게 처리할 수 없다고 안내하세요.",
    "invalid_input": "입력이 올바르지 않습니다. 값을 확인해 다시 호출하세요.",
    "precondition_failed": "이 요청을 처리할 조건이 아직 갖춰지지 않았습니다.",
    "not_confirmed": "고객의 동의가 확인되지 않아 실행하지 않았습니다.",
    "system_error": "지금은 시스템에 연결되지 않아 처리하지 못했습니다. 고객에게 지금은 확인이 어렵다고 말하고, "
                    "잠시 뒤 다시 시도하거나 상담원 연결을 안내하세요.",
    "timeout": "시스템 응답이 너무 늦어 중단했습니다. 고객에게 지금은 확인이 어렵다고 말하고, "
               "잠시 뒤 다시 시도하거나 상담원 연결을 안내하세요.",
}
# Session facts in words, for "what is needed first" sentences.
FACT_WORDS = {"verified": "본인 확인"}


class _TimedOut(Exception):
    pass


class _Proposal:
    def __init__(self, turn, arguments, confirmation_text, data):
        self.turn = turn                        # the turn in which the confirmation was (last) asked
        self.arguments = arguments
        self.confirmation_text = confirmation_text
        self.data = data


class _SessionState:
    def __init__(self):
        self.facts = set()
        self.proposals = {}                     # request key -> _Proposal
        self.done = {}                          # request key -> first ToolResult of an executed request
        self.unknown = {}                       # request key -> failure of a run whose outcome is not known
        self.lock = asyncio.Lock()              # write tools run one at a time per session


def _fail(error_type, text=None, **more):
    return ToolResult(ok=False, text=text or DEFAULT_TEXT[error_type], error_type=error_type, **more)


def _norm(value):
    return value.strip().casefold() if isinstance(value, str) else value


def _path_values(data, path):
    """Values at a dotted path; lists are walked through."""
    nodes = [data]
    for part in path.split("."):
        found = []
        for node in nodes:
            if isinstance(node, list):
                node_items = node
            else:
                node_items = [node]
            for item in node_items:
                if isinstance(item, dict) and part in item:
                    found.append(item[part])
        nodes = found
    out = []
    for node in nodes:
        out.extend(node if isinstance(node, list) else [node])
    return out


def _at(data, path):
    """The value at a dotted path of the data; "" is the data itself; None if it is not there."""
    node = data
    for part in [p for p in (path or "").split(".") if p]:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _entry(kind, data, source):
    return {"kind": kind, "data": copy.deepcopy(data), "source": source}


class Gateway:
    """contracts.ToolGateway.

    consent_window: how many turns after the confirmation was read the consent may arrive.
        1 (default) is R-TOOL-4 to the letter: "the customer agrees in their next utterance".
        A proposal older than that is asked again instead of executed.
    consent_reader: text -> "yes" | "no" | "unclear".
    """

    def __init__(self, contracts, recorder=None, consent_window=1, consent_reader=read_consent, adapters=None):
        # by id ("pack.name"; the bare name outside a pack)
        self._contracts: dict[str, ToolContract] = {getattr(c, "id", key): c for key, c in dict(contracts).items()}
        self._recorder = recorder
        self._window = max(1, int(consent_window))
        self._consent = consent_reader
        self._sessions: dict[str, _SessionState] = {}
        self._adapters: dict[str, Adapter] = dict(adapters or {})
        problems = []
        for name, contract in self._contracts.items():
            if name in self._adapters:
                continue
            try:
                self._adapters[name] = build_adapter(contract)
            except AdapterError as e:
                problems.append(f"{name}.yaml: {e}")
        problems += check_refresh(self._contracts)
        if problems:
            raise ContractError(problems)

    # ---- contracts.ToolGateway ---------------------------------------------------------------

    def names(self) -> list[str]:
        """The ids of all contracts."""
        return sorted(self._contracts)

    def resolve(self, name: str, packs=None) -> str:
        """The id an agent with these tool packs means by `name` (an id passes through; a bare name
        must be in exactly one of the packs, or in exactly one pack at all). KeyError otherwise."""
        return resolve(self._contracts, name, packs)

    def contract(self, name: str) -> ToolContract:
        return self._contracts[self.resolve(name)]

    def specs(self, tool_names: list[str]) -> list[dict]:
        """For the model: the bare names (they are unique inside one agent)."""
        return [{"type": "function", "function": {
            "name": c.name, "description": c.description, "parameters": c.input_schema}}
            for c in (self.contract(name) for name in tool_names)]

    def facts(self, session_id: str) -> set[str]:
        state = self._sessions.get(session_id)
        return set(state.facts) if state else set()

    def end_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        seen = set()
        for adapter in self._adapters.values():
            target = getattr(adapter, "system", adapter)
            if id(target) in seen:
                continue
            seen.add(id(target))
            try:
                adapter.end_session(session_id)
            except Exception:
                log.warning("end_session failed in adapter of %s", adapter.contract.name, exc_info=True)

    async def call(self, ctx: ToolContext, name: str, arguments: dict) -> ToolResult:
        try:
            name = self.resolve(name)       # a bare name of one pack, or an id
        except UnknownTool:
            pass                            # _dispatch answers "no such tool"
        return await self._call(ctx, name, arguments)

    async def _call(self, ctx, name, arguments, refresh_of=None):
        """refresh_of: (tool name, call id) when this call redraws the screen after that tool."""
        t_start, started = time.time(), time.perf_counter()
        call_id = uuid.uuid4().hex[:12]
        info = {"attempts": 0, "phase": "refused", "arguments": arguments}
        if refresh_of:
            info["refresh_of"], info["parent_call_id"] = refresh_of
        contract = self._contracts.get(name)
        try:
            result = await self._dispatch(ctx, name, contract, arguments, info)
        except asyncio.CancelledError:
            info["phase"] = "cancelled"
            cancelled = _fail("system_error", "호출이 취소되었습니다.")
            cancelled.call_id = call_id
            cancelled.ms = (time.perf_counter() - started) * 1000
            self._record(ctx, name, contract, cancelled, t_start, info)
            raise
        except Exception as e:      # a bug in an adapter must not break the conversation
            log.exception("tool %s failed unexpectedly", name)
            info["error"] = f"{type(e).__name__}: {e}"
            result = _fail("system_error")
        result.call_id = call_id
        result.ms = round((time.perf_counter() - started) * 1000, 1)
        if not result.items and result.ok:
            result.items = len(NUMBERED.findall(result.text))
        if contract is not None:
            try:
                result.display = await self._displays(ctx, contract, result, follow=refresh_of is None)
            except asyncio.CancelledError:
                self._record(ctx, name, contract, result, t_start, info)
                raise
            except Exception:           # the screen never breaks the call
                log.warning("could not build the display of %s", name, exc_info=True)
        self._record(ctx, name, contract, result, t_start, info)
        return result

    # ---- screens -----------------------------------------------------------------------------

    async def _displays(self, ctx, contract, result, follow=True):
        """ToolResult.display of one finished call: [{"kind", "data", "source"}]."""
        data = result.data
        if not result.ok:
            notice = data.get("display_notice") if isinstance(data, dict) else None
            if isinstance(notice, str) and notice.strip():
                notice = {"level": "info", "text": notice.strip()}
            if isinstance(notice, dict) and notice.get("text"):
                return [_entry("notice", dict({"level": "info"}, **notice), contract.name)]
            return []
        if result.needs_confirmation:
            card = dict(data) if isinstance(data, dict) else {}
            card.update(text=result.confirmation_text, tool=contract.name)
            return [_entry("confirm", card, contract.name)]
        spec = contract.display
        if spec is None:
            return []
        own = []
        payload = _at(data, spec.path)
        if payload is not None:
            own.append(_entry(spec.kind, payload, contract.name))
        if not follow:
            return own
        more = []
        for other in spec.refresh:
            other = sibling(contract, other)
            target = self._contracts.get(other)
            if target is None or target.effect != "read" or other == contract.id:
                continue
            refreshed = await self._call(ctx, other, {}, refresh_of=(contract.name, result.call_id))
            if refreshed.ok:                # a refresh that failed is skipped: the call itself stands
                more += refreshed.display
        # the refreshed screen is the system's state now: it replaces this call's own copy of it
        fresh = {entry["kind"] for entry in more}
        return [entry for entry in own if entry["kind"] not in fresh] + more

    # ---- extras ------------------------------------------------------------------------------

    def pending(self, session_id: str) -> list[dict]:
        """Write requests waiting for the customer's consent: [{tool, arguments, confirmation_text, turn}]."""
        state = self._sessions.get(session_id)
        if not state:
            return []
        return [{"tool": key[0], "arguments": dict(p.arguments), "confirmation_text": p.confirmation_text,
                 "turn": p.turn} for key, p in state.proposals.items()]

    # ---- one call ----------------------------------------------------------------------------

    def _state(self, session_id):
        if session_id not in self._sessions:
            self._sessions[session_id] = _SessionState()
        return self._sessions[session_id]

    async def _dispatch(self, ctx, name, contract, arguments, info):
        if contract is None:
            return _fail("invalid_input", f"'{name}'이라는 도구는 없습니다. 쓸 수 있는 도구만 호출하세요.")
        info["effect"] = contract.effect
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except ValueError:
                return _fail("invalid_input", "인자를 읽지 못했습니다. 이름과 값의 묶음(JSON 객체)으로 다시 호출하세요.")
        if arguments is None:
            arguments = {}
        try:
            arguments = validate(contract.input_schema, arguments)
        except InputError as e:
            names = [n for n, _ in e.problems if n]
            return _fail("invalid_input",
                         "입력이 올바르지 않아 호출하지 않았습니다. " + " ".join(text for _, text in e.problems)
                         + " 필요한 값을 고객에게 확인한 뒤 다시 호출하세요.",
                         data={"fields": names})
        info["arguments"] = arguments

        state = self._state(ctx.session_id)
        missing = [fact for fact in contract.requires if fact not in state.facts]
        if missing:
            return _fail("precondition_failed", self._needs_first(contract, missing), data={"missing": missing})

        adapter = self._adapters[contract.id]
        if contract.effect == "read":
            info["phase"] = "read"
            result = await self._guarded(contract, lambda: self._attempts(
                adapter.call, arguments, ctx, contract, contract.retries, info), info)
            if isinstance(result, Reply):
                result = self._result(contract, result)
            if result.ok:
                state.facts.update(contract.grants)
            return result
        async with state.lock:
            return await self._write(ctx, contract, adapter, arguments, state, info)

    def _needs_first(self, contract, missing):
        parts = []
        for fact in missing:
            word = FACT_WORDS.get(fact, f"'{fact}' 조건")
            givers = [c.name for c in self._contracts.values() if fact in c.grants]
            if givers:
                parts.append(f"먼저 {word}이 필요합니다. {' 또는 '.join(givers)} 도구를 먼저 호출해 성공해야 합니다.")
            else:
                parts.append(f"먼저 {word}이 필요합니다.")
        return (f"{contract.name} 도구는 아직 쓸 수 없습니다. " + " ".join(parts)
                + " 그 전에는 이 도구가 알려 줄 내용을 추측해서 말하지 마세요.")

    async def _attempts(self, fn, arguments, ctx, contract, retries, info):
        """Calls within one time budget; a system that was not reached is tried again."""
        deadline = time.perf_counter() + contract.timeout_s
        last = None
        for _ in range(max(0, retries) + 1):
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise _TimedOut()
            info["attempts"] += 1
            try:
                return await asyncio.wait_for(fn(arguments, ctx), remaining)
            except (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException):
                raise _TimedOut()
            except AdapterUnavailable as e:
                last = e
                info["error"] = str(e)
        raise last

    async def _guarded(self, contract, run, info):
        """Runs an adapter step; turns the ways it can break into failure results."""
        try:
            return await run()
        except _TimedOut:
            info["error"] = "timeout"
            return _fail("timeout")
        except AdapterUnavailable as e:
            info["error"] = str(e)
            return _fail("system_error")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("adapter of %s raised", contract.name)
            info["error"] = f"{type(e).__name__}: {e}"
            return _fail("system_error")

    def _result(self, contract, reply: Reply) -> ToolResult:
        if reply.ok:
            text = reply.text
            if not text:
                text = ("처리되었습니다." if reply.data is None
                        else "결과: " + json.dumps(reply.data, ensure_ascii=False, default=str))
            return ToolResult(ok=True, text=text, data=reply.data, items=reply.items or 0)
        error_type = reply.error_type if reply.error_type in DEFAULT_TEXT else "system_error"
        return ToolResult(ok=False, text=reply.text or DEFAULT_TEXT[error_type], data=reply.data, error_type=error_type)

    # ---- write tools -------------------------------------------------------------------------

    def _key(self, contract, arguments):
        fields = contract.idempotency_fields or sorted(arguments)
        return (contract.id, json.dumps({f: _norm(arguments.get(f)) for f in fields},
                                          sort_keys=True, ensure_ascii=False, default=str))

    async def _write(self, ctx, contract, adapter, arguments, state, info):
        key = self._key(contract, arguments)
        if key in state.done:               # R-TOOL-5: never twice
            info["phase"] = "duplicate"
            first = state.done[key]
            state.facts.update(contract.grants)
            return first.model_copy(update={
                "executed": False, "needs_confirmation": False,
                "text": "이 요청은 이미 처리되었습니다. 다시 실행하지 않았습니다. 처음 결과: " + first.text})
        if key in state.unknown:
            info["phase"] = "blocked_unknown"
            return state.unknown[key].model_copy()
        if ctx.speculative:
            info["phase"] = "speculative_refused"
            return _fail("not_confirmed",
                         "고객의 말이 아직 끝나지 않아 실행하지 않았습니다. 아무것도 바뀌지 않았습니다. "
                         "고객의 말이 확정된 뒤에 다시 호출하세요.")
        if not contract.confirm_template:   # a write tool without a confirmation step runs at once
            return await self._execute(ctx, contract, adapter, arguments, state, key, info)

        if getattr(ctx, "touch_consent", False):
            # the customer pressed the button that sends / runs it, under what the screen showed: that is consent
            # every open proposal of this tool goes: the card may have been edited, and the older wording must not
            # be sent too by a later "응" (review 2026-11-20)
            for old in [k for k in state.proposals if k[0] == contract.id]:
                state.proposals.pop(old, None)
            info["consent"] = "touch"
            return await self._execute(ctx, contract, adapter, arguments, state, key, info)
        proposal = state.proposals.get(key)
        if proposal is not None and not self._same_request(contract, proposal, arguments):
            proposal = None                 # what the customer was read no longer matches: ask again
            info["reproposed"] = "arguments_changed"
        if proposal is not None and ctx.turn <= proposal.turn:
            info["phase"] = "same_turn_refused"
            return _fail("not_confirmed",
                         f"아직 고객의 답을 듣지 않아 실행하지 않았습니다. 지금은 고객에게 \"{proposal.confirmation_text}\"라고 "
                         f"묻기만 하고, 고객이 동의하면 다음 턴에 같은 인자로 {contract.name} 도구를 다시 호출하세요.",
                         confirmation_text=proposal.confirmation_text)
        if proposal is not None and ctx.turn - proposal.turn > self._window:
            proposal = None                 # the confirmation is stale: consent must follow it directly
            info["reproposed"] = "stale"
        if proposal is None:
            return await self._propose(ctx, contract, adapter, arguments, state, key, info)

        verdict = self._consent(ctx.last_user_text)
        info["consent"] = verdict
        info["proposed_turn"] = proposal.turn
        if verdict == "no":
            del state.proposals[key]
            info["phase"] = "declined"
            return _fail("not_confirmed",
                         "고객이 동의하지 않아 실행하지 않았습니다. 아무것도 바뀌지 않았습니다. "
                         "고객이 원하는 것을 다시 물어보세요.")
        if verdict != "yes":
            proposal.turn = ctx.turn        # asked again now; the answer may come in the next turn
            info["phase"] = "unclear_refused"
            return _fail("not_confirmed",
                         f"고객의 동의가 분명하지 않아 실행하지 않았습니다. 고객의 말에 먼저 답한 뒤 "
                         f"\"{proposal.confirmation_text}\"라고 다시 물어보세요. 고객이 분명히 동의하면 다음 턴에 "
                         f"같은 인자로 {contract.name} 도구를 다시 호출하세요.",
                         confirmation_text=proposal.confirmation_text)
        return await self._execute(ctx, contract, adapter, arguments, state, key, info)

    def _same_request(self, contract, proposal, arguments):
        """The arguments that were read to the customer in the confirmation must not have changed."""
        for field in set(PLACEHOLDER.findall(contract.confirm_template or "")):
            if field in proposal.arguments or field in arguments:
                if _norm(proposal.arguments.get(field)) != _norm(arguments.get(field)):
                    return False
        return True

    async def _propose(self, ctx, contract, adapter, arguments, state, key, info):
        info["phase"] = "proposed"
        state.proposals.pop(key, None)
        preview = await self._guarded(contract, lambda: self._attempts(
            adapter.preview, arguments, ctx, contract, contract.retries, info), info)
        if isinstance(preview, ToolResult):         # the preview could not be made
            info["phase"] = "preview_failed"
            return preview
        if preview is not None and not preview.ok:  # the system already knows it would refuse
            info["phase"] = "preview_refused"
            return self._result(contract, preview)
        data = preview.data if preview is not None and isinstance(preview.data, dict) else {}
        values = dict(arguments)
        values.update(data)
        missing = [f for f in PLACEHOLDER.findall(contract.confirm_template) if values.get(f) in (None, "")]
        if missing:
            info["phase"] = "preview_failed"
            info["error"] = "confirm_template placeholders without a value: " + ", ".join(missing)
            return _fail("system_error",
                         "확인 문장을 만들 정보를 받지 못해 진행하지 않았습니다. 아무것도 바뀌지 않았습니다. "
                         "고객에게 지금은 처리가 어렵다고 말하고 상담원 연결을 안내하세요.")
        sentence = PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), contract.confirm_template)
        state.proposals[key] = _Proposal(ctx.turn, dict(arguments), sentence, data)
        return ToolResult(
            ok=True, needs_confirmation=True, confirmation_text=sentence, data=data or None,
            text=f"아직 실행하지 않았습니다. 고객에게 \"{sentence}\"라고 묻고 답을 기다리세요. "
                 f"고객이 동의하면 다음 턴에 같은 인자로 {contract.name} 도구를 다시 호출해야 실제로 처리됩니다. "
                 f"동의하지 않으면 호출하지 마세요. 처리되었다고 말하면 안 됩니다.")

    async def _execute(self, ctx, contract, adapter, arguments, state, key, info):
        info["phase"] = "executed"
        info["attempts"] += 1
        uncertain = None
        try:
            reply = await asyncio.wait_for(adapter.call(arguments, ctx), contract.timeout_s)
        except (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException):
            uncertain = "timeout"
        except AdapterUnavailable as e:
            info["error"] = str(e)
            if getattr(e, "uncertain", False):
                uncertain = "system_error"
            else:                           # never reached the system: nothing changed, the proposal stands
                info["phase"] = "execute_failed"
                return _fail("system_error",
                             "지금은 시스템에 연결되지 않아 처리하지 못했습니다. 아무것도 바뀌지 않았습니다. "
                             "고객에게 접수되지 않았다고 알리고, 잠시 뒤 다시 시도하거나 상담원 연결을 안내하세요.")
        except asyncio.CancelledError:
            # the request may or may not have gone through: the same request must not run again blindly
            state.proposals.pop(key, None)
            state.unknown[key] = _fail("system_error", self._unknown_text())
            raise
        except Exception as e:
            log.exception("adapter of %s raised during a write", contract.name)
            info["error"] = f"{type(e).__name__}: {e}"
            uncertain = "system_error"
        if uncertain:
            # A write is never retried blindly: we do not know whether it went through.
            info["phase"] = "outcome_unknown"
            state.proposals.pop(key, None)
            state.unknown[key] = _fail(uncertain, self._unknown_text())
            return state.unknown[key].model_copy()

        result = self._result(contract, reply)
        if not result.ok:
            info["phase"] = "execute_refused"
            if result.error_type != "system_error":
                state.proposals.pop(key, None)
            return result
        result.executed = True if reply.executed is None else bool(reply.executed)
        if not result.executed:
            info["phase"] = "duplicate"     # the system itself says this request had already been done
        state.proposals.pop(key, None)
        if not reply.repeatable:            # "one more americano" is a new request every time
            state.done[key] = result.model_copy()
        state.facts.update(contract.grants)
        return result

    @staticmethod
    def _unknown_text():
        return ("요청을 보냈지만 처리 결과를 확인하지 못했습니다. 두 번 처리되는 일을 막기 위해 다시 시도하지 않습니다. "
                "고객에게 접수 여부를 확인 중이라고 말하고 상담원 연결을 안내하세요.")

    # ---- record ------------------------------------------------------------------------------

    def _record(self, ctx, name, contract, result, t_start, info):
        if self._recorder is None:
            return
        try:
            arguments = info.get("arguments")
            output = result.model_dump()
            # what was shown is in the engine's display spans; here only which screens, so that the
            # data is not stored twice and no unmasked copy of it sits beside output.data
            output["display"] = [{"kind": d.get("kind"), "source": d.get("source")} for d in result.display]
            pii_paths, secrets = [], []
            if contract is not None:
                pii_paths = (["input.arguments." + f for f in contract.pii_input]
                             + ["output.data." + p for p in contract.pii_output])
                if isinstance(arguments, dict):
                    secrets += [arguments.get(f) for f in contract.pii_input]
                for path in contract.pii_output:
                    secrets += _path_values(result.data, path)
            # the sentences repeat values of the marked fields ("김민수 고객님의 주문"): hide them there too
            for secret in sorted({str(s) for s in secrets if isinstance(s, (str, int)) and not isinstance(s, bool)
                                  and len(str(s)) >= 2}, key=len, reverse=True):
                for field in ("text", "confirmation_text"):
                    if output.get(field):
                        output[field] = output[field].replace(secret, MASK)
            attrs = {"effect": contract.effect if contract else None, "executed": result.executed,
                     "error_type": result.error_type, "needs_confirmation": result.needs_confirmation,
                     "adapter": contract.adapter.kind if contract else None, "phase": info.get("phase"),
                     "attempts": info.get("attempts", 0), "speculative": bool(ctx.speculative),
                     "call_id": result.call_id}
            for extra in ("consent", "proposed_turn", "reproposed", "error", "refresh_of", "parent_call_id"):
                if extra in info:
                    attrs[extra] = info[extra]
            # The span carries the name the agent called (one conversation is one agent, so that is
            # unambiguous, and metrics and older records use it); the pack says whose tool it was.
            if contract is not None and contract.pack:
                attrs.update(pack=contract.pack, tool_id=contract.id)
            span = Span(session_id=ctx.session_id, turn=ctx.turn, kind="tool", name=contract.name if contract else name,
                        t_start=t_start, t_end=t_start + result.ms / 1000.0, ok=result.ok,
                        input={"arguments": arguments}, output=output, attrs=attrs)
            self._recorder.span(span, pii_paths=pii_paths)
        except Exception:                   # recording never breaks a conversation (R-OBS-7)
            log.warning("could not record the tool span of %s", name, exc_info=True)


def _overrides_from_env():
    path = os.environ.get("AGENTKIT_TOOL_OVERRIDES")
    if not path:
        return {}
    loaded = yaml.safe_load(Path(path).expanduser().read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise ContractError([f"{path}: 도구 이름과 adapter 설정의 묶음이어야 합니다"])
    return loaded


def build_gateway(recorder=None, tools_dir=None, adapter_overrides=None, consent_window=1) -> Gateway:
    """Reads every contract of config.TOOLS_DIR (or tools_dir) and builds the gateway.

    recorder: contracts.Recorder or None (nothing is recorded).
    adapter_overrides: {tool name: AdapterSpec or {"kind", "target", "options"}} replaces the
        adapter of those tools without touching the contract files or any agent definition
        (R-TOOL-3: dummy <-> replay <-> http). The same mapping can come from a YAML file named by
        the environment variable AGENTKIT_TOOL_OVERRIDES.
    """
    contracts = load_contracts(tools_dir)
    overrides = dict(_overrides_from_env())
    overrides.update(adapter_overrides or {})
    problems = []
    for name, spec in overrides.items():
        try:
            name = resolve(contracts, name)
        except UnknownTool as e:
            problems.append(f"adapter_overrides: {e}")
            continue
        try:
            spec = spec if isinstance(spec, AdapterSpec) else AdapterSpec.model_validate(spec)
        except Exception as e:
            problems.append(f"adapter_overrides.{name}: {e}")
            continue
        contracts[name] = contracts[name].model_copy(update={"adapter": spec})
        problems += check_contract(contracts[name], f"adapter_overrides.{name}")
    if problems:
        raise ContractError(problems)
    return Gateway(contracts, recorder=recorder, consent_window=consent_window)
