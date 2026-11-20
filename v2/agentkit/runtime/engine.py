"""The conversation engine: one conversation's history, prompt, knowledge, tools, guards, handoff.

Implements contracts.ReplyEngine. No audio in here: the text channel shows the events, the voice
channel turns "filler" and "piece" into speech. Nothing about a particular agent is written in this
file; persona, rules, guards, tools and knowledge all come from the AgentDefinition.

Generalised from v1's chat() coroutine (realtime_mic_app.py).
"""
import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from .. import config
from ..contracts import ReplyEvent, SessionMeta, Span, ToolContext, ToolResult, TurnRecord
from .guards import GuardSet, empty_wait_guard, is_empty_wait
from .templates import Missing, fill as fill_template
from .knowledge import ASKS_ELSEWHERE, ASKS_KNOWLEDGE, BACK_REFERENCE, knowledge_first
from .situation import situation as situation_note
from .prompt import (HANDOFF_TOOL, REPLY_LENGTHS, build_system_prompt, clock_text, line, reply_length,
                     speakable, take_piece, text_language, touch_sentence)

logger = logging.getLogger("agentkit.runtime")

MAX_TOOL_ROUNDS = 3        # times one reply may go back to the LLM with tool results (ARCHITECTURE 5)
HISTORY_MESSAGES = 40      # about this many messages are kept, never starting mid-exchange
FIRST_PIECE_CHARS = 15     # a short first piece gets the voice talking sooner
PIECE_CHARS = 40
RESULT_CHARS = 1200        # of a tool result shown in the "tool" event


class NullRecorder:
    """Stands in when there is no trace module: records nothing."""

    def start_session(self, meta): pass

    def span(self, span, pii_paths=None): pass

    def turn(self, turn): pass

    def audio(self, session_id, turn, role, pcm16): pass

    def end_session(self, session_id, status): pass


class _Turn:
    """What is known about the reply in progress, so that it can be undone or cut later."""

    def __init__(self, number, user_text, speculative, t_user_end, previous):
        self.number = number
        self.user_message = {"role": "user", "content": user_text}
        self.reply = {"role": "assistant", "content": ""}
        self.tool_entries = []       # the tool exchange of this reply; in the history just before it
        self.speculative = speculative
        self.t_user_end = t_user_end
        self.t_first = None          # first text of the reply, by the engine's own clock
        self.t_output = None         # ...as reported by the channel (mark_output)
        self.t_end = None
        self.marked = set()          # output phases already reported
        self.pending_spans = []      # a speculative reply holds its spans back until it completes
        self.called = set()          # tools called for this reply
        self.done = set()            # ...that count as done for the guards: reads, and writes that really ran
        self.asked = set()           # writes that only asked the customer to agree (needs_confirmation)
        self.fillered = False
        self.handoff = False
        self.interrupted = False
        self.finished = False        # completed or aborted: nothing more will happen to it
        self.recorded = False        # a TurnRecord has been sent
        self.discarded = False
        self.previous = previous
        self.opening = False         # the agent's opening (turn 0): never discarded, its tool results stay


class Engine:
    """contracts.ReplyEngine.

    definition: the AgentDefinition this conversation runs on.
    gateway:    contracts.ToolGateway, or None for an agent without tools.
    recorder:   contracts.Recorder, or None to record nothing.
    llm:        an object with agentkit.llm.LLM's stream() (and optionally supports()).
    knowledge:  retrieval client (see knowledge.KnowledgeClient: search(), context()), or None.
    now:        callable returning an aware datetime; for tests.
    """

    def __init__(self, definition, gateway=None, recorder=None, llm=None, *, store=None, knowledge=None,
                 channel="text", test=False, simulated=False, label="", session_id=None, now=None,
                 settings=None):
        if llm is None:
            from ..llm import LLM
            llm = LLM(model=definition.llm.model)
        self.definition = definition
        self.gateway = gateway
        self.recorder = recorder or NullRecorder()
        self.llm = llm
        self.store = store
        self.knowledge = knowledge
        self.channel = channel
        self.test = bool(test)
        self.simulated = bool(simulated)
        self.label = label
        self.session_id = session_id or uuid.uuid4().hex[:16]
        self.turn = 0
        self.handed_off = False
        self.closed = False
        self.history = []                       # user / assistant / tool messages; no system prompt
        self.language = None                    # channel override of definition.language ("ko", "en", "auto")
        self.reply_length = None                # channel override of definition.reply_length
        self.guards = GuardSet(definition.guards)
        self._now = now or (lambda: datetime.now(ZoneInfo(config.TIMEZONE)))
        self._last = None                       # the newest turn that was not discarded
        self._reference = ""                    # knowledge text of the turn before, for back-references
        self._can_call = None
        self._contracts = None
        self._tool_ids = {}        # bare tool name -> the gateway's id of it
        self._opening_entries = []              # the opening's messages: head of the history, never trimmed
        self._opened = False
        settings = dict(settings or {})
        # which screens the customer's page can draw ("mail_list,mail,..."); None: not said, every screen counts as
        # seen. The conversation page with the avatar draws no mail table, and the assistant kept saying "화면에
        # 띄워 두었어요" over a list nobody could see (2026-11-20).
        kinds = settings.get("screen")
        self.screen_kinds = None if kinds is None else {k.strip() for k in str(kinds).split(",") if k.strip()}
        self.set_options(language=settings.get("language"), reply_length=settings.get("reply_length"))
        self._safe(self.recorder.start_session, SessionMeta(
            session_id=self.session_id, agent_id=definition.id, version=definition.version, channel=channel,
            test=self.test, simulated=self.simulated, started_at=time.time(), label=label,
            store_content=self.simulated or bool(getattr(getattr(definition, "privacy", None), "store_content", True))
            or settings.get("keep_content") is True,
            settings={"model": definition.llm.model, "language": self.language or definition.language,
                      "reply_length": self.reply_length or definition.reply_length, **settings}))

    # ------------------------------------------------------------------------------ small helpers

    def set_options(self, language=None, reply_length=None):
        """Channel settings that override the definition for this conversation (R-RT-10)."""
        if language in ("ko", "en", "auto"):
            self.language = language
        if reply_length in REPLY_LENGTHS:
            self.reply_length = reply_length

    def _safe(self, function, *args, **kwargs):
        """Recording must never break the conversation (R-OBS-7)."""
        try:
            return function(*args, **kwargs)
        except Exception as e:
            logger.warning(f"Recorder call {getattr(function, '__name__', function)} failed: {e!r}")

    def _span(self, state, kind, t_start, t_end=None, *, name="", ok=True, input=None, output=None, attrs=None):
        span = Span(session_id=self.session_id, turn=state.number if state else 0, kind=kind, name=name,
                    t_start=t_start, t_end=t_end if t_end is not None else t_start, ok=ok, input=input,
                    output=output, attrs=attrs or {})
        if state is not None and state.speculative and not state.finished:
            state.pending_spans.append(span)
        else:
            self._safe(self.recorder.span, span)

    def _record(self, state):
        """Sends (or sends again, corrected) the TurnRecord of a turn; the store keeps one per turn."""
        state.recorded = True
        self._safe(self.recorder.turn, TurnRecord(
            session_id=self.session_id, turn=state.number, user_text=state.user_message["content"],
            agent_text=state.reply["content"].strip(), t_user_end=state.t_user_end,
            t_agent_start=state.t_output or state.t_first, t_agent_end=state.t_end,
            interrupted=state.interrupted, handoff=state.handoff))

    def _forget(self, entry):
        for i, message in enumerate(self.history):
            if message is entry:
                del self.history[i]
                return

    def _pinned(self):
        """How many messages at the head of the history belong to the opening (what its tools returned,
        e.g. the menu, must stay known however long the conversation gets)."""
        if not self._opening_entries:
            return 0
        opening = {id(message) for message in self._opening_entries}
        count = 0
        while count < len(self.history) and id(self.history[count]) in opening:
            count += 1
        return count

    def _trim_history(self):
        pinned = self._pinned()
        excess = len(self.history) - pinned - HISTORY_MESSAGES
        if excess > 0:
            del self.history[pinned:pinned + excess]
        while len(self.history) > pinned and self.history[pinned]["role"] != "user":
            del self.history[pinned]

    def _unseen(self, result):
        """True if the result has a screen the customer's page cannot draw."""
        if self.screen_kinds is None:
            return False
        return any(isinstance(e, dict) and e.get("kind") and str(e["kind"]) not in self.screen_kinds
                   for e in getattr(result, "display", None) or [])

    def _display_events(self, state, result):
        """One "display" event (and span) per screen the gateway attached to a tool result. The payload
        is the system's, passed on as it is: the model has no part in what is shown."""
        events = []
        for entry in getattr(result, "display", None) or []:
            if not isinstance(entry, dict) or not entry.get("kind"):
                continue
            kind, source = str(entry["kind"]), str(entry.get("source") or "")
            self._span(state, "display", time.time(), name=kind, output={"data": entry.get("data")},
                       attrs={"source": source})
            events.append(ReplyEvent(type="display", data={"kind": kind, "data": entry.get("data"), "source": source}))
        return events

    @staticmethod
    def _tool_done(call, result, effect):
        return ReplyEvent(type="tool", data={
            "state": "done", "name": call["name"], "arguments": call["arguments"], "ok": result.ok,
            "ms": result.ms, "result": (result.text or "")[:RESULT_CHARS],
            "items": result.items or len(re.findall(r"^\s*\d+\. ", result.text or "", re.M)),
            "effect": effect, "executed": result.executed,
            "needs_confirmation": result.needs_confirmation,
            "confirmation_text": result.confirmation_text, "error_type": result.error_type,
            "call_id": result.call_id})

    def _tool_contracts(self):
        """{name: contract} of the tools this agent may call: listed in the definition and known to the gateway."""
        if self._contracts is None:
            contracts = {}
            if self.gateway is not None and self.definition.tools:
                try:
                    known = set(self.gateway.names())
                except Exception as e:
                    logger.warning(f"Tool gateway unavailable: {e!r}")
                    known = set()
                # The definition writes bare names and names its packs; the gateway knows ids
                # ("burger.search_menu"). The model, the guards and the screen keep the bare names.
                resolve = getattr(self.gateway, "resolve", None)
                packs = list(getattr(self.definition, "tool_packs", None) or [])
                for name in self.definition.tools:
                    try:
                        tool_id = resolve(name, packs) if resolve is not None else name
                    except KeyError as e:
                        logger.warning(f"Agent {self.definition.id}: tool {name!r} left out: {e}")
                        continue
                    if tool_id not in known:
                        logger.warning(f"Agent {self.definition.id}: tool {name!r} has no contract; left out")
                        continue
                    try:
                        contract = self.gateway.contract(tool_id)
                    except Exception:
                        contract = None
                    short = getattr(contract, "name", None) or name
                    if short in contracts:
                        logger.warning(f"Agent {self.definition.id}: two tools are called {short!r}; "
                                       f"{tool_id!r} left out")
                        continue
                    contracts[short] = contract
                    self._tool_ids[short] = tool_id
            self._contracts = contracts
        return self._contracts

    async def _tools_usable(self):
        if self._can_call is None:
            usable = bool(self._tool_contracts())
            supports = getattr(self.llm, "supports", None)
            if usable and supports is not None:
                try:
                    usable = bool(await supports("tools", self.definition.llm.model))
                except Exception:
                    usable = False
                if not usable:
                    logger.warning(f"Model {self.definition.llm.model} cannot call tools; the agent runs without")
            self._can_call = usable
        return self._can_call

    def _turn_language(self, user_text, heard):
        setting = self.language or self.definition.language
        if setting in ("ko", "en"):
            return setting
        return heard if heard in ("ko", "en") else text_language(user_text)

    def _messages(self, system, state, clock, reference, question, note=""):
        messages = [{"role": "system", "content": system}]
        for message in self.history:
            if message is state.reply:
                continue
            if message is state.user_message and (clock or reference or note):
                # the clock goes here, not in the system prompt: see prompt.py (the same for the note on where
                # the conversation stands: it changes every turn and would spoil the cached system prompt)
                content = f"{reference}\n{question}{message['content']}" if reference else message["content"]
                if note:
                    content = f"{note}\n{content}"
                message = {**message, "content": f"{clock}\n{content}" if clock else content}
            messages.append(dict(message))
        return messages

    def _handoff_rule(self, reason):
        """The id of the handoff rule a reason belongs to, as far as that can be told."""
        rules = self.definition.handoff
        reason = str(reason or "")
        for rule in rules:
            if rule.id and rule.id in reason:
                return rule.id
        if len(rules) == 1:
            return rules[0].id
        words = set(re.findall(r"\w{2,}", reason))
        best, overlap = "", 0
        for rule in rules:
            shared = len(words & set(re.findall(r"\w{2,}", rule.when)))
            if shared > overlap:
                best, overlap = rule.id, shared
        return best

    async def _call_tool(self, state, call, language):
        """One gateway call. Returns (ToolResult, text for the model)."""
        name, arguments = call["name"], call["arguments"]
        contracts = self._tool_contracts()
        if name not in contracts:
            # the definition's tool list is a permission: anything else is refused here, not by the gateway
            text = line("unknown_tool", language, name=name, names=", ".join(contracts) or "-")
            return ToolResult(ok=False, text=text, error_type="invalid_input"), text
        context = ToolContext(
            session_id=self.session_id, agent_id=self.definition.id, version=self.definition.version,
            turn=state.number, language=language, test=self.test or self.simulated, simulated=self.simulated,
            # a notice is not the customer speaking: nothing in it can be read as consent
            last_user_text="" if getattr(state, "notice", False) else state.user_message["content"],
            speculative=state.speculative, touch_consent=bool(getattr(state, "touch_consent", False)))
        started = time.perf_counter()
        try:
            result = await self.gateway.call(context, self._tool_ids.get(name, name), arguments)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Tool {name} raised {e!r}")
            result = ToolResult(ok=False, text=line("tool_crash", language), error_type="system_error")
        if not result.ms:
            result.ms = (time.perf_counter() - started) * 1000
        state.called.add(name)
        # "환불이 접수되었습니다" is allowed only after the refund ran: a write call that only asked for
        # consent, or failed, does not count (it used to, and the false claim got through)
        if getattr(contracts[name], "effect", "read") != "write" or (result.ok and not result.needs_confirmation):
            state.done.add(name)
        elif result.needs_confirmation or (result.error_type == "not_confirmed" and result.confirmation_text):
            # asked, or asked again (the answer was unclear): the question the model reads is the tool's own
            state.asked.add(name)
        text = result.text or ""
        if result.needs_confirmation:
            # the model reads the sentence to the customer; the engine never confirms on its own
            text = (text + "\n" if text else "") + line(
                "confirm", language, text=result.confirmation_text or text, name=name)
        elif not result.ok and result.error_type:
            text = line("failed", language, error=result.error_type, text=text)
        elif name == HANDOFF_TOOL and result.ok:
            # a handoff rule may fix what the customer hears (definition: handoff[].say), e.g. "call 119 now"
            rule_id = str(arguments.get("rule_id") or "") or self._handoff_rule(arguments.get("reason"))
            say = next((rule.say for rule in self.definition.handoff if rule.id == rule_id and rule.say), "")
            text = (text + "\n" if text else "") + (line("handoff_done_say", language, text=say) if say
                                                    else line("handoff_done", language))
        logger.info(f"[{self.session_id}] tool {name}({self._loggable(arguments)}) -> ok={result.ok} executed={result.executed} "
                    f"confirm={result.needs_confirmation} error={result.error_type} in {result.ms:.0f} ms")
        return result, text

    # ------------------------------------------------------------------------------ ending a turn

    def _finish(self, state):
        """The turn is complete: its spans and its record go out."""
        state.finished = True
        state.t_end = time.time()
        for span in state.pending_spans:
            self._safe(self.recorder.span, span)
        state.pending_spans.clear()
        state.previous = None
        self._record(state)

    def _abort(self, state):
        """The reply was cancelled before it was complete."""
        if state.finished:
            return
        state.finished = True
        state.t_end = time.time()
        if state.speculative:
            # as if the turn had never been: no history, no record
            self._discard(state)
            return
        state.interrupted = True
        if not state.reply["content"].strip():
            self._forget(state.reply)
            for entry in state.tool_entries:
                self._forget(entry)
        state.previous = None
        self._record(state)

    def _discard(self, state):
        state.discarded = True
        state.pending_spans.clear()
        for entry in (state.user_message, state.reply, *state.tool_entries):
            self._forget(entry)
        if self.turn == state.number:
            self.turn -= 1
        if self._last is state:
            self._last = state.previous
        state.previous = None

    # ------------------------------------------------------------------------------ the contract

    async def reply(self, user_text, *, language=None, speculative=False, t_user_end=None, _touch=None, _notice=False):
        """Starts the next turn and yields ReplyEvents until "done".

        _touch is act()'s way in, not for callers: (ClientAction, arguments) of a touch on the screen.
        _notice is notify()'s way in: the turn was started by news from outside, not by the customer.

        Cancelling the iteration (cancelling the task that consumes it, or aclose()) cancels the
        reply and its tool call. A cancelled speculative reply leaves nothing behind; any other
        cancelled reply keeps what had been generated and is recorded as interrupted.
        """
        if self._last is not None and not self._last.finished:
            self._abort(self._last)  # the consumer walked away from the reply before
        user_text = str(user_text or "").strip()
        self.turn += 1
        state = _Turn(self.turn, user_text, bool(speculative), t_user_end or time.time(), self._last)
        state.notice = bool(_notice)
        self._last = state
        reply = state.reply
        self.history.append(state.user_message)
        self._trim_history()
        self.history.append(reply)
        turn_language = self._turn_language(user_text, language)
        started = time.perf_counter()
        try:
            if self.handed_off:
                # the conversation belongs to a human now: nothing is generated any more
                state.handoff = True
                reply["content"] = line("handed_off", turn_language)
                state.t_first = time.time()
                yield ReplyEvent(type="delta", text=reply["content"])
                yield ReplyEvent(type="piece", text=reply["content"])
                self._finish(state)
                yield ReplyEvent(type="done", text=reply["content"], data={"handed_off": True, "turn": state.number})
                return

            definition = self.definition
            setting = self.reply_length or definition.reply_length
            length = reply_length(user_text, setting)
            num_predict = max(definition.llm.num_predict, REPLY_LENGTHS[length][2]) \
                if length == "explain" else definition.llm.num_predict
            # the context follows the setting, not this turn: changing it makes Ollama reload the model
            num_ctx = max(definition.llm.num_ctx, REPLY_LENGTHS[setting if setting in REPLY_LENGTHS else "short"][3])
            now = self._now()
            clock = clock_text(now, turn_language) if definition.live_clock else ""

            # --- knowledge: goes in front of the user's message of this turn only
            reference = ""
            mode = getattr(definition.knowledge, "mode", "before_reply")
            if self.knowledge is not None and user_text and _touch is None and not _notice \
                    and not knowledge_first(user_text, mode):
                yield ReplyEvent(type="knowledge", data={"state": "skipped", "mode": mode})
                self._reference = ""
            elif self.knowledge is not None and user_text and _touch is None and not _notice:
                if BACK_REFERENCE.search(user_text) and mode == "before_reply":
                    reference = self._reference
                    yield ReplyEvent(type="knowledge", data={"state": "reused", "has": bool(reference)})
                else:
                    yield ReplyEvent(type="knowledge", data={"state": "start", "query": user_text[:80]})
                    t_search = time.time()
                    threshold = float(definition.knowledge.threshold or 0)
                    try:
                        hits, ms = await self.knowledge.search(user_text)
                        reference, titles = self.knowledge.context(hits, threshold, turn_language)
                        top = hits[0]["score"] if hits else 0.0
                        self._span(state, "retrieval", t_search, time.time(), name=definition.knowledge.name,
                                   input={"query": user_text}, output={"sources": titles, "top_score": top},
                                   attrs={"used": bool(reference), "threshold": threshold, "found": len(hits)})
                        yield ReplyEvent(type="knowledge", data={
                            "state": "done", "ok": True, "ms": ms, "top": top, "threshold": threshold,
                            "sources": titles, "found": len(hits)})
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.warning(f"Knowledge search failed: {e!r}")
                        self._span(state, "retrieval", t_search, time.time(), name=definition.knowledge.name,
                                   ok=False, input={"query": user_text}, output={"sources": [], "top_score": 0.0},
                                   attrs={"used": False, "error": type(e).__name__})
                        yield ReplyEvent(type="knowledge", data={
                            "state": "done", "ok": False,
                            "message": "지식베이스가 제한 시간 안에 답하지 않음" if "Timeout" in type(e).__name__ else str(e)[:80]})
                    self._reference = reference

            # --- prompt
            contracts = self._tool_contracts() if await self._tools_usable() else {}
            if user_text and ASKS_ELSEWHERE.search(user_text) and not ASKS_KNOWLEDGE.search(user_text) \
                    and "search_knowledge" in contracts:
                # "인터넷에서 검색해줘": the model was seen picking the knowledge base anyway (2026-11-20)
                contracts = {name: c for name, c in contracts.items() if name != "search_knowledge"}
            specs = []
            if contracts:
                try:
                    specs = self.gateway.specs([self._tool_ids.get(name, name) for name in contracts])
                except Exception as e:
                    logger.warning(f"Tool specs unavailable: {e!r}")
                    contracts = {}
            system = build_system_prompt(
                definition, language=turn_language, length=length, now=now, contracts=contracts,
                knowledge_name=definition.knowledge.name if self.knowledge is not None else "",
                knowledge_mode=getattr(definition.knowledge, "mode", "before_reply"))
            # What this conversation has already established (gateway facts, e.g. "verified"). Without
            # it the model was seen asking a verified customer for the order number three more times.
            try:
                facts = set(self.gateway.facts(self.session_id)) if self.gateway is not None else set()
            except Exception:
                facts = set()
            if facts:
                done = sorted(name for name, contract in contracts.items()
                              if set(getattr(contract, "grants", None) or []) & facts)
                if done:
                    if turn_language == "en":
                        system += ("\nAlready done in this conversation: " + ", ".join(done) + " succeeded. Do not ask "
                                   "the customer for it again; go straight on with the tools that needed it.")
                    else:
                        system += ("\n이 대화에서 이미 끝난 절차: " + ", ".join(done) + " 성공. 이 절차를 고객에게 다시 요구하지 "
                                   "말고, 이것이 필요했던 도구를 바로 호출해 이어 가세요.")
            question = line("question", turn_language)
            # where the conversation stands, from the history before this turn (situation.py)
            note = ""
            if getattr(definition, "situation_note", True) and not _touch:
                try:
                    before = self.history[:self.history.index(state.user_message)] \
                        if state.user_message in self.history else list(self.history)
                    note = situation_note(before, turn_language)
                except Exception as e:
                    logger.warning(f"Situation note failed: {e!r}")

            # --- generation: text, or tool calls and another pass
            nudge = []            # a one-off correction after a guard; not kept in the history
            regenerated = False
            empty_retry = False
            wait_followed = False   # the empty-wait rule gets one continuation per reply
            confirm_pending = False
            round_results = []    # (call, contract, result) of the latest tool round
            filler_held = False   # the filler was skipped because a reply template may answer at once
            templated = False     # the reply was spoken from tool reply templates, without a model pass
            tool_rounds = 0
            buffer = ""
            answer_started = False
            error = None
            model = definition.llm.model
            pass_number = 0

            def withdraw(sentence, rest):
                """Takes back unspoken text. First time: everything unspoken, and the pass runs again."""
                nonlocal buffer
                again = not regenerated
                unspoken = sentence + rest
                kept = "" if again else rest
                reply["content"] = reply["content"][:len(reply["content"]) - len(unspoken)] + kept
                buffer = kept
                if again and reply["content"].strip():
                    reply["content"] = reply["content"].rstrip() + " "   # the new pass continues after it
                return again

            async def tool_round(calls, note=None):
                """Runs the calls in order: events, screens, handoff; then the exchange goes into the
                history. note(result): text added to what the model is told about each result."""
                nonlocal confirm_pending, still_open
                round_results.clear()
                exchange = [{"role": "assistant", "content": "",
                             "tool_calls": [{"function": dict(call)} for call in calls]}]
                for call in calls:
                    contract = contracts.get(call["name"]) or self._tool_contracts().get(call["name"])
                    effect = getattr(contract, "effect", "read") or "read"
                    yield ReplyEvent(type="tool", data={"state": "start", "name": call["name"],
                                                        "arguments": call["arguments"], "effect": effect})
                    result, text = await self._call_tool(state, call, turn_language)
                    if note is not None:
                        text = (text + "\n" if text else "") + note(result)
                    if self._unseen(result):
                        text = (text + "\n" if text else "") + line("screen_unseen", turn_language)
                    exchange.append({"role": "tool", "tool_name": call["name"], "content": text})
                    round_results.append((call, contract, result))
                    yield self._tool_done(call, result, effect)
                    for event in self._display_events(state, result):
                        yield event
                    if result.needs_confirmation:
                        confirm_pending = True
                        if result.confirmation_text:
                            # the customer must hear the tool's own sentence: the model was seen saying "김민수
                            # 님에게 보낼까요?" over a confirmation addressed to someone else (2026-11-20)
                            still_open = result.confirmation_text
                    if call["name"] == HANDOFF_TOOL and result.ok and not result.needs_confirmation \
                            and not self.handed_off:
                        reason = str(call["arguments"].get("reason") or "")
                        rule_id = str(call["arguments"].get("rule_id") or "") or self._handoff_rule(reason)
                        self.handed_off = True
                        state.handoff = True
                        self._span(state, "handoff", time.time(), name=HANDOFF_TOOL,
                                   attrs={"rule_id": rule_id, "reason": reason})
                        yield ReplyEvent(type="handoff", data={"rule_id": rule_id, "reason": reason})
                # in the history the tool exchange comes before the reply it led to
                position = next((i for i, m in enumerate(self.history) if m is reply), len(self.history))
                self.history[position:position] = exchange
                state.tool_entries.extend(exchange)

            def template_reply():
                """The reply spoken straight from the tools' reply templates (contracts.ToolReply), or None
                when the model has to word it: a call failed or needs consent, a tool has no template, a
                value is missing, something was said already, or the template is in another language."""
                if not round_results or reply["content"].strip() or self.handed_off:
                    return None
                sentences, closing = [], ""
                for call, contract, result in round_results:
                    template = getattr(contract, "reply", None)
                    if template is None or not result.ok or result.needs_confirmation:
                        return None
                    if text_language(template.say) != turn_language:
                        return None
                    try:
                        sentences.append(fill_template(template.say, call["arguments"], result.data))
                    except Missing as missing:
                        logger.info(f"[{self.session_id}] reply template of {call['name']} not used: no {missing}")
                        return None
                    closing = template.closing or closing
                parts = [speakable(part) for part in sentences + ([closing] if closing else [])]
                parts = [part for part in parts if part]
                return parts or None

            def speak_template(parts, t_start):
                """Each sentence is its own piece: the voice channel then synthesises (and starts playing)
                the first one without waiting for the closing question."""
                state.t_first = state.t_first or time.time()
                text = " ".join(parts)
                reply["content"] = text
                self._span(state, "template", t_start, time.time(), name="+".join(c["name"] for c, _, _ in round_results),
                           output={"text": text}, attrs={"tools": [c["name"] for c, _, _ in round_results], "saved_pass": True})
                return [ReplyEvent(type="delta", text=text)] + [ReplyEvent(type="piece", text=part) for part in parts]

            still_open = None     # a confirmation the customer has not answered yet: it must be asked again
            touched = False
            if _touch is not None:
                # The customer did it on the screen: the tool runs now, without the model. The model
                # then says one short thing about it, and gets no tools for that (it was seen calling
                # the same tool again for the same touch).
                action, arguments = _touch
                touched = True
                self._span(state, "action", time.time(), name=action.name, input={"arguments": arguments},
                           attrs={"tool": action.tool})

                def touch_note(result):
                    # the sentence may name things only the result knows ("{name}")
                    state.user_message["content"] = touch_sentence(action, arguments, turn_language, result.data)
                    if result.needs_confirmation:
                        return line("touch_proposed", turn_language)     # a plan or a mail to confirm, not done yet
                    return line("touch_done" if result.ok else "touch_failed", turn_language)

                state.touch_consent = bool(getattr(action, "consent", False))   # for this one call only
                try:
                    async for event in tool_round([{"name": action.tool, "arguments": arguments}], note=touch_note):
                        yield event
                finally:
                    state.touch_consent = False
                text = template_reply()
                if text:
                    for event in speak_template(text, time.time()):
                        yield event
                    templated = True
            elif (answer := self._pending_answer(state)) is not None:
                # the customer is answering a confirmation question: settle it before the model speaks
                tool_rounds += 1

                def answer_note(result):
                    # neither yes nor no (a reason, a question): the request still waits and must be asked again
                    nonlocal still_open
                    if not result.ok and result.error_type == "not_confirmed" and result.confirmation_text:
                        still_open = result.confirmation_text
                        return line("confirm_open", turn_language, text=result.confirmation_text)
                    return ""

                async for event in tool_round([answer], note=answer_note):
                    yield event

            while not templated and pass_number < MAX_TOOL_ROUNDS + 3:
                pass_number += 1
                # the last pass gets no tools, so the reply cannot loop on lookups; nor does the pass
                # after a write tool asked for confirmation: that one has to read the sentence out
                tools = specs if specs and tool_rounds < MAX_TOOL_ROUNDS and not confirm_pending \
                    and not self.handed_off and not touched else None
                messages = self._messages(system, state, clock, reference, question, note) + nudge
                calls = []
                pass_text = ""
                first_token_ms = None
                stats = {}
                retry = False
                failed = None
                t_pass = time.time()
                pass_started = time.perf_counter()
                yield ReplyEvent(type="llm", data={
                    "state": "start", "round": pass_number, "model": model, "tools": len(tools or []),
                    "language": turn_language, "length": length})
                stream = self.llm.stream(messages, model=model, tools=tools, num_predict=num_predict, num_ctx=num_ctx)
                try:
                    async for kind, value in stream:
                        if kind == "stats":
                            stats = value or {}
                            continue
                        if first_token_ms is None:
                            first_token_ms = (time.perf_counter() - pass_started) * 1000
                            yield ReplyEvent(type="llm", data={
                                "state": "first_token", "round": pass_number, "ms": first_token_ms})
                        if kind == "tool":
                            if tools is None:
                                continue  # not offered in this pass
                            arguments = value.get("arguments")
                            if isinstance(arguments, str):
                                try:
                                    arguments = json.loads(arguments)
                                except ValueError:
                                    arguments = {}
                            call = {"name": str(value.get("name") or ""),
                                    "arguments": arguments if isinstance(arguments, dict) else {}}
                            contract = contracts.get(call["name"])
                            template = getattr(contract, "reply", None)
                            if not state.fillered and not answer_started and not filler_held and template is not None \
                                    and text_language(template.say) == turn_language:
                                # its reply template will answer as soon as the call is back: no filler
                                filler_held = True
                            elif not state.fillered and not answer_started and not filler_held:
                                # say something right away; the call and the next pass take a while
                                state.fillered = True
                                filler = getattr(contract, "filler", None)
                                if not filler or text_language(filler) != turn_language:
                                    filler = line("filler", turn_language)  # the contract's line is in another language
                                yield ReplyEvent(type="filler", text=filler, data={"name": call["name"]})
                            calls.append(call)
                            continue
                        if kind != "text" or not value:
                            continue
                        if not reply["content"]:
                            value = value.lstrip()  # some passes open with a line break
                            if not value:
                                continue
                        reply["content"] += value
                        buffer += value
                        pass_text += value
                        if state.t_first is None:
                            state.t_first = time.time()
                        yield ReplyEvent(type="delta", text=value)
                        while True:
                            piece, buffer = take_piece(buffer, PIECE_CHARS if answer_started else FIRST_PIECE_CHARS)
                            if not piece:
                                break
                            hit = self._screen(piece, self._guard_tools(state, calls))
                            if hit is not None:
                                before, sentence, after, guard = hit
                                spoken = speakable(before)
                                if spoken:
                                    answer_started = True
                                    yield ReplyEvent(type="piece", text=spoken)
                                again = withdraw(sentence, after + buffer)
                                action = "regenerated" if again else "dropped"
                                self._span(state, "guard", time.time(), name=guard.id, input={"sentence": sentence.strip()},
                                           attrs={"guard_id": guard.id, "action": action})
                                logger.info(f"[{self.session_id}] guard {guard.id} ({action}): {self._loggable(sentence.strip())!r}")
                                yield ReplyEvent(type="replace", text=reply["content"],
                                                 data={"guard_id": guard.id, "action": action})
                                if again:
                                    regenerated = True
                                    nudge = self._nudge(guard, reply["content"], turn_language)
                                    retry = True
                                    break
                                continue
                            spoken = speakable(piece)
                            if spoken:
                                answer_started = True
                                yield ReplyEvent(type="piece", text=spoken)
                        if retry:
                            break
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    failed = e
                finally:
                    close = getattr(stream, "aclose", None)
                    if close is not None:
                        try:
                            await close()
                        except BaseException:
                            pass
                self._span(state, "llm", t_pass, time.time(), name=model, ok=failed is None,
                           input={"messages": messages},
                           output={"text": pass_text, "tool_calls": [dict(c) for c in calls]},
                           attrs={"model": model, "round": pass_number, "first_token_ms": first_token_ms,
                                  "tools": len(tools or []), "language": turn_language,
                                  **({"error": str(failed)[:200]} if failed else {}),
                                  **{k: v for k, v in stats.items() if isinstance(v, (int, float))}})
                if failed is not None:
                    logger.warning(f"[{self.session_id}] LLM failed: {failed!r}")
                    error = str(failed)[:200]
                    break
                if retry:
                    continue
                while buffer.strip():
                    # the end of the pass is a sentence end too
                    hit = self._screen(buffer, self._guard_tools(state, calls))
                    if hit is None:
                        break
                    before, sentence, after, guard = hit
                    spoken = speakable(before)
                    if spoken:
                        answer_started = True
                        yield ReplyEvent(type="piece", text=spoken)
                    again = withdraw(sentence, after)
                    action = "regenerated" if again else "dropped"
                    self._span(state, "guard", time.time(), name=guard.id, input={"sentence": sentence.strip()},
                               attrs={"guard_id": guard.id, "action": action})
                    logger.info(f"[{self.session_id}] guard {guard.id} ({action}): {self._loggable(sentence.strip())!r}")
                    yield ReplyEvent(type="replace", text=reply["content"], data={"guard_id": guard.id, "action": action})
                    if again:
                        regenerated = True
                        nudge = self._nudge(guard, reply["content"], turn_language)
                        retry = True
                        break
                if retry:
                    continue
                if not calls and not state.called and tools and not wait_followed \
                        and is_empty_wait(reply["content"]):
                    # "잠시만 기다려 주세요" and the reply would end here with nothing running: the
                    # customer would wait for ever. What was said stays; the model is made to go on
                    # in the same reply and either look it up now or ask for what it is missing.
                    wait_followed = True
                    guard = empty_wait_guard()
                    self._span(state, "guard", time.time(), name=guard.id,
                               input={"sentence": reply["content"].strip()[-200:]},
                               attrs={"guard_id": guard.id, "action": "continued"})
                    logger.info(f"[{self.session_id}] guard {guard.id} (continued): {self._loggable(reply['content'].strip()[-80:])!r}")
                    if buffer.strip():
                        spoken = speakable(buffer)
                        if spoken:
                            answer_started = True
                            yield ReplyEvent(type="piece", text=spoken)
                        buffer = ""
                    if reply["content"].strip():
                        reply["content"] = reply["content"].rstrip() + " "
                    nudge = self._nudge(guard, reply["content"], turn_language)
                    continue
                if not calls:
                    if not reply["content"].strip() and not empty_retry:
                        # now and then the model returns nothing at all (mostly after a lookup); ask once more
                        empty_retry = True
                        logger.info(f"[{self.session_id}] empty reply in pass {pass_number}; asking again")
                        continue
                    break

                # --- a tool round. Anything said along with the calls is a sentence of its own.
                spoken = speakable(buffer)
                buffer = ""
                if spoken:
                    answer_started = True
                    yield ReplyEvent(type="piece", text=spoken)
                if reply["content"].strip():
                    reply["content"] = reply["content"].rstrip() + " "
                tool_rounds += 1
                t_round = time.time()
                async for event in tool_round(calls):
                    yield event
                text = template_reply()
                if text:
                    for event in speak_template(text, t_round):
                        yield event
                    break
                if filler_held and not state.fillered and not answer_started:
                    # the template could not answer: the model runs again, so cover that pass after all
                    state.fillered = True
                    first_call = round_results[0][0]["name"] if round_results else ""
                    filler = getattr(contracts.get(first_call), "filler", None)
                    if not filler or text_language(filler) != turn_language:
                        filler = line("filler", turn_language)
                    yield ReplyEvent(type="filler", text=filler, data={"name": first_call})

            # --- the end of the reply
            spoken = speakable(buffer)
            if spoken:
                yield ReplyEvent(type="piece", text=spoken)
            if still_open and still_open not in reply["content"] and not self.handed_off:
                # the model answered but did not ask again: without the question the customer would not know
                # that the request is still waiting for them
                ask = speakable(still_open)
                reply["content"] = (reply["content"].rstrip() + " " + ask).strip()
                yield ReplyEvent(type="delta", text=" " + ask)
                yield ReplyEvent(type="piece", text=ask)
            reply["content"] = reply["content"].strip()
            fallback = False
            if not reply["content"]:
                # silence is worse than an apology; the handoff line if that is what just happened
                fallback = True
                reply["content"] = line("handed_off" if state.handoff else "fallback", turn_language)
                if state.t_first is None:
                    state.t_first = time.time()
                yield ReplyEvent(type="delta", text=reply["content"])
                yield ReplyEvent(type="piece", text=reply["content"])
            yield ReplyEvent(type="llm", data={"state": "done", "chars": len(reply["content"]),
                                               "ms": (time.perf_counter() - started) * 1000,
                                               "rounds": pass_number})
            self._finish(state)   # before "done": a consumer may stop iterating once it has seen it
            data = {"turn": state.number, "handoff": state.handoff}
            if fallback:
                data["fallback"] = True
            if error:
                data["error"] = error
            yield ReplyEvent(type="done", text=reply["content"], data=data)
        finally:
            if not state.finished:
                self._abort(state)

    async def open(self):
        """The agent's opening (definition.opening), once, before the first customer turn.

        Calls the opening tools through the gateway (turn 0, not speculative): "tool" and "display"
        events; then the opening words as "piece" and "done". Without an opening, or when called a
        second time or after the conversation has begun, only "done" is yielded and nothing changes.
        The calls and what they returned stay at the head of the history and are never trimmed away,
        so the model knows, say, the menu it takes orders from.
        """
        opening = self.definition.opening
        say = speakable(opening.say)
        if self._opened or self.turn > 0 or self.closed or not (say or opening.tools):
            yield ReplyEvent(type="done", text="", data={"turn": 0, "opening": False})
            return
        self._opened = True
        setting = self.language or self.definition.language
        language = setting if setting in ("ko", "en") else text_language(say or self.definition.persona)
        state = _Turn(0, "", False, time.time(), None)
        state.opening = True
        state.reply["content"] = ""
        self._last = state
        entries = self._opening_entries
        # a conversation opens with the customer's side in every chat template: a note stands in for it
        note = {"role": "user", "content": line("opening", language)}
        entries.append(note)
        self.history.insert(0, note)
        unseen = False
        try:
            calls = [{"name": call.name, "arguments": dict(call.arguments)} for call in opening.tools]
            if calls:
                exchange = [{"role": "assistant", "content": "",
                             "tool_calls": [{"function": dict(call)} for call in calls]}]
                for call in calls:
                    contract = self._tool_contracts().get(call["name"])
                    effect = getattr(contract, "effect", "read") or "read"
                    yield ReplyEvent(type="tool", data={"state": "start", "name": call["name"],
                                                        "arguments": call["arguments"], "effect": effect})
                    result, text = await self._call_tool(state, call, language)
                    if self._unseen(result):
                        text = (text + "\n" if text else "") + line("screen_unseen", language)
                        unseen = True
                    exchange.append({"role": "tool", "tool_name": call["name"], "content": text})
                    yield self._tool_done(call, result, effect)
                    for event in self._display_events(state, result):
                        yield event
                position = self._pinned()
                self.history[position:position] = exchange
                entries.extend(exchange)
            if unseen and say:
                # "읽지 않은 메일을 화면에 띄워 두었어요" on a page that shows no mail: keep only what is still true
                kept = [s for s in re.split(r"(?<=[.!?。])\s+", say) if not re.search(r"화면|screen", s, re.I)]
                say = " ".join(kept).strip()
            if say:
                state.reply["content"] = say
                position = self._pinned()
                self.history.insert(position, state.reply)
                entries.append(state.reply)
                state.t_first = time.time()
                yield ReplyEvent(type="piece", text=say)
            state.finished = True
            state.t_end = time.time()
            self._record(state)
            yield ReplyEvent(type="done", text=say, data={"turn": 0, "opening": True})
        finally:
            if not state.finished:
                # cancelled on the way: what was called stays known, the words were not said
                state.finished = True
                state.t_end = time.time()
                state.interrupted = True
                self._record(state)

    async def act(self, name, arguments):
        """A touch on the screen (definition.actions): the mapped tool is called through the gateway,
        then the model reacts in one short reply. Counts as a customer turn.

        An unknown action, or one whose tool is not this agent's or would need spoken consent
        (a confirm_template), yields only "done" with data {"error": ...} and changes nothing.
        """
        arguments = dict(arguments) if isinstance(arguments, dict) else {}
        action = next((a for a in self.definition.actions if a.name == name), None)
        error = None
        if action is None:
            error = f"unknown action: {name}"
        else:
            contracts = self._tool_contracts()
            contract = contracts.get(action.tool)
            if action.tool not in contracts:
                error = f"action {name}: tool {action.tool} is not available to this agent"
            elif contract is None or (getattr(contract, "confirm_template", None)
                                      and not (getattr(action, "propose", False) or getattr(action, "consent", False))):
                # consent is given in words, to a sentence read aloud; a touch stands in for it only where the
                # definition says so (action.propose: the touch may only propose; action.consent: it is the consent)
                error = f"action {name}: tool {action.tool} needs spoken consent and cannot be triggered by a touch"
        if error:
            logger.info(f"[{self.session_id}] touch refused: {error}")
            yield ReplyEvent(type="done", text="", data={"error": error, "turn": self.turn})
            return
        setting = self.language or self.definition.language
        language = setting if setting in ("ko", "en") else None
        sentence = touch_sentence(action, arguments, language or "ko")
        events = self.reply(sentence, language=language, t_user_end=time.time(), _touch=(action, arguments))
        try:
            async for event in events:
                yield event
        finally:
            await events.aclose()   # a cancelled touch cancels its turn like any other reply

    async def notify(self, text):
        """News from outside the conversation: a task the agent handed off has finished, needs an
        answer, or stopped before an irreversible step. The model tells the customer about it (and
        may call tools, e.g. to have a confirmation sentence made). Counts as a turn, but it is not
        the customer's: the gateway reads no consent from it, and knowledge is not searched for it.
        """
        text = str(text or "").strip()
        if not text or self.closed or self.handed_off:
            yield ReplyEvent(type="done", text="", data={"turn": self.turn, "notice": False})
            return
        setting = self.language or self.definition.language
        language = setting if setting in ("ko", "en") else None
        note = f"(System notice: {text})" if language == "en" else f"(시스템 알림: {text})"
        events = self.reply(note, language=language, t_user_end=time.time(), _notice=True)
        try:
            async for event in events:
                yield event
        finally:
            await events.aclose()

    def _loggable(self, value):
        """What the server log may show of a text or of tool arguments. With definition.privacy.store_content
        false (a real conversation), the log keeps no content either: argument names and lengths only."""
        private = not self.simulated and not getattr(getattr(self.definition, "privacy", None), "store_content", True)
        if not private:
            return value
        if isinstance(value, dict):
            return {k: f"<{len(str(v))}자>" for k, v in value.items()}
        return f"<{len(str(value or ''))}자>"

    def _guard_tools(self, state, calls):
        """(done, asked): the tools a guard may take as having happened.

        done: reads, and writes that really ran, in this reply, plus reads the model is calling right now.
        asked: writes that only asked for consent, or that the model is calling right now. They excuse a
        question ("환불을 접수할까요?" is the tool's own confirmation) but not a statement ("접수되었습니다"
        beside a call that only asked was a false claim that used to get through)."""
        contracts = self._tool_contracts()
        writes = {c["name"] for c in calls if getattr(contracts.get(c["name"]), "effect", "read") == "write"}
        return (state.done | {c["name"] for c in calls} - writes, state.asked | writes)

    def _guard_check(self, sentence, tools):
        done, asked = tools
        found = self.guards.check(sentence, done)
        if found is None or not asked:
            return found
        question = sentence.rstrip().rstrip("\"')」』 ").endswith(("?", "？"))
        if not question and found.kind == "claims_lookup" and set(found.unless_tools or []) & asked:
            # the sentence splitter may keep "…보낼까요? 내용은 '안녕하세요, ○○ 님." together (quotes): what
            # counts is whether each match is itself a question
            matches = list(re.finditer(found.pattern, sentence))
            question = bool(matches) and all(re.match(r"\s*[?？]", sentence[m.end():m.end() + 3]) for m in matches)
        return self.guards.check(sentence, done | asked) if question else found

    def _pending_answer(self, state):
        """The customer's answer to a confirmation question: the call that asked it, to be made again now.

        A write tool with a confirm_template asks first; the same call in the NEXT turn runs it if the
        customer agreed. Left to the model, that second call was often missing (it read a reason or a
        question as "no", or asked again in its own words), so the request expired and the customer had
        to agree two or three times. The engine makes the call itself; the gateway still decides from
        the customer's words: yes runs it, no cancels it, anything else keeps it open and asks again.
        Only when exactly one request was proposed in the turn before."""
        pending = getattr(self.gateway, "pending", None)
        if not callable(pending) or state.speculative or getattr(state, "notice", False):
            return None
        try:
            waiting = [p for p in pending(self.session_id) if p.get("turn") == state.number - 1]
        except Exception:
            return None
        if len(waiting) != 1:
            return None
        names = {tool_id: short for short, tool_id in self._tool_ids.items()}
        name = names.get(waiting[0]["tool"], waiting[0]["tool"])
        if name not in self._tool_contracts():
            return None
        return {"name": name, "arguments": dict(waiting[0]["arguments"])}

    def _screen(self, text, called):
        """Runs the guards over text that is about to be cleared for speech.

        Returns None, or (before, sentence, after, guard): the first sentence that violates a guard,
        with the innocent text around it, so that only that sentence is withheld.
        """
        if not self.guards:
            return None
        done, asked = called
        if self.guards.check(text, done) is None:   # the strictest reading finds nothing: nothing to withhold
            return None
        sentences, rest = [], text
        while True:
            sentence, rest = take_piece(rest, 1)
            if sentence is None:
                break
            sentences.append(sentence)
        if rest:
            sentences.append(rest)
        for index, sentence in enumerate(sentences):
            found = self._guard_check(sentence, called)
            if found is not None:
                return "".join(sentences[:index]), sentence, "".join(sentences[index + 1:]), found
        guard = self._guard_check(text, called)
        if guard is None:
            return None
        return "", text, "", guard   # the pattern spans sentences: the whole text goes

    def _nudge(self, guard, spoken, language):
        """The messages that go after the conversation for the one regeneration a guard may ask for.

        The nudge is a user-role note in brackets, as in v1: the local model follows it there, and
        a second system message in the middle of a conversation is not reliably honoured by Ollama's
        chat templates.
        """
        if spoken.strip():
            return [{"role": "assistant", "content": spoken.strip()},
                    {"role": "user", "content": guard.nudge + line("continue", language)}]
        return [{"role": "user", "content": guard.nudge}]

    def truncate_last_reply(self, heard_text):
        """After an interruption: keep only what the user actually heard ("" = nothing)."""
        state = self._last
        if state is None or state.discarded:
            return
        if not state.finished:
            self._abort(state)
            if state.discarded:
                return
        heard = str(heard_text or "").strip()
        current = state.reply["content"]
        if heard and len(heard) >= len(current.strip()):
            return  # all of it was heard
        state.interrupted = True
        if heard:
            state.reply["content"] = heard
        else:
            # nothing of the answer was heard, so the lookup behind it never happened either
            state.reply["content"] = ""
            self._forget(state.reply)
            for entry in state.tool_entries:
                self._forget(entry)
        self._span(state, "interrupt", time.time(), attrs={"heard_chars": len(heard)})
        self._record(state)

    def commit_speculation(self):
        """Not in the contract: the words the running speculative reply answers are final now (the
        voice channel committed it). From here on it is an ordinary turn: a write tool it calls later
        runs (with the usual confirmation step) instead of being refused as "not final yet", and its
        held-back spans are recorded. Returns True if a speculative turn was running.

        Without this, a reply committed before it reached a write tool kept speculative=True: the
        tool was refused, and the model went on to tell the customer the order was placed."""
        state = self._last
        if state is None or state.discarded or state.finished or not state.speculative:
            return False
        state.speculative = False
        for span in state.pending_spans:
            self._safe(self.recorder.span, span)
        state.pending_spans.clear()
        return True

    def discard_last_turn(self):
        """Not in the contract: forgets the newest turn altogether (user message, tool exchange, reply).

        For a speculative reply that had already completed when the speaker went on. A TurnRecord
        that went out cannot be recalled; the voice channel should prefer cancelling the iteration.
        """
        state = self._last
        if state is None or state.discarded or state.opening:
            return
        state.finished = True
        self._discard(state)

    def mark_output(self, phase, t):
        """The channel reports when the first sound or text of a phase reached the client."""
        state = self._last
        if state is None or state.discarded or phase in state.marked:
            return
        state.marked.add(phase)
        self._span(state, "output", t, attrs={"phase": phase, "ms": max(0.0, (t - state.t_user_end) * 1000)})
        if phase == "reply":
            state.t_output = t
            if state.recorded:
                self._record(state)

    async def close(self, status="completed"):
        if self.closed:
            return
        self.closed = True
        if self._last is not None and not self._last.finished:
            self._abort(self._last)
        if self.handed_off and status == "completed":
            status = "handoff"
        self._safe(self.recorder.end_session, self.session_id, status)
        if self.gateway is not None:
            self._safe(self.gateway.end_session, self.session_id)
        close = getattr(self.knowledge, "close", None)
        if close is not None:
            try:
                await close()
            except Exception:
                pass
