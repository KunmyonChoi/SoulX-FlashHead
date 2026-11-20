"""Simulated customers and case replay (R-EVAL-4~6).

A persona is role-played by the LLM one utterance at a time; a case replays stored user turns.
Both talk to the agent through a ConversationDriver, then the conversation is analysed and the
verdicts are compared with the item's `expect`.
"""
import asyncio
import re
import time
import uuid

from agentkit.contracts import CriterionResult, TestCase, TestItemResult, TestPersona

from . import deps
from .analysis import analyze_session, load_session
from .screen import describe_touch, latest, menu_text, render_display, resolve_touch

CUSTOMER_SCHEMA = {
    "type": "object",
    "properties": {
        "progress": {"type": "string"},     # where the goal stands, written before deciding `done`
        "done": {"type": "boolean"},
        "utterance": {"type": "string"},
    },
    "required": ["progress", "done", "utterance"],
}

CUSTOMER_SYSTEM = """당신은 고객센터에 연락한 고객을 연기합니다. AI 상담원과 음성으로 통화하고 있습니다. 당신은 상담원이 아니라 고객입니다.

고객의 성향: {description}
고객의 목표: {goal}
고객이 알고 있는 정보:
{facts}

말하는 법:
- 고객의 다음 말 한 마디만 만듭니다. 한두 문장의 짧은 한국어 구어체로, 전화로 말하듯 씁니다. 목록, 괄호 설명, 이모지는 쓰지 않습니다.
- 성향에 맞게 말합니다. 알고 있는 정보는 성향이 달리 정하지 않는 한 상담원이 물을 때 그대로 알려 줍니다. 위에 없는 정보는 지어내지 않고 모른다고 합니다.
- 상담원이 "~할까요?"처럼 실행해도 되는지 물으면, 목표에 맞는 일이면 "네, 해 주세요"처럼 분명히 동의하고 아니면 분명히 거절합니다.
- 같은 말을 되풀이하지 않습니다. 상담원이 이미 답한 것을 다시 묻지 않습니다.

출력:
- progress: 목표가 지금 어디까지 왔는지 한 문장.
- done: 통화를 끝낼 때 true. (1) 상담원이 목표한 일을 처리했다고 알렸거나 원하던 답을 주었다, (2) 상담원이 그 일은 할 수 없다고 분명히 했거나 같은 이유로 두 번 거절했다(더 조르지 않는다), (3) 사람 상담원에게 연결한다고 했다. 이 셋 중 하나면 true이고, 아직 진행 중이면 false입니다.
- utterance: done이 false면 고객의 다음 말. done이 true면 짧은 끝인사."""

# Appended to the customer's instructions only when the agent has a screen (an opening ran or a
# display arrived); a persona of an agent without one gets the prompt above unchanged.
SCREEN_SYSTEM = """

화면:
- 이 대화는 전화가 아니라 화면이 달린 기기(키오스크) 앞에서 이루어집니다. 고객은 화면을 보면서 말합니다.
- 주문 내역에 담긴 것만으로는 끝난 것이 아닙니다. 목표가 주문과 결제라면, 결제가 끝나 영수증 화면이 나오거나 상담원이 주문 번호를 알려 줄 때까지 done은 false입니다. 상담원이 더 필요한 것이 있는지 물었는데 더 없으면, 없다고 하고 결제하겠다고 말합니다.
- 화면에 보이는 메뉴입니다. 고객은 여기에 있는 이름으로 주문하고, 여기에 없는 것은 모릅니다.
{menu}"""

TOUCH_SYSTEM = """

화면 터치:
- 아래 일은 말로 하지 않고 화면을 눌러서 합니다. 누를 차례가 되면 touch에 그 번호를 쓰고 utterance는 빈 문자열로 둡니다. 한 번에 하나만 누릅니다.
- 말을 할 때는 touch를 0으로 합니다. 누를 일을 말로 주문하지 않고, 이미 누른 것은 다시 누르지 않습니다.
{touches}"""

TOUCH_FAILED_NOTE = "방금 누르려던 것({what})은 지금 화면에 없어 누를 수 없었습니다. touch는 0으로 하고 말로 하세요."

UNPAID_NOTE = ("화면의 주문 내역에 아직 결제하지 않은 주문이 남아 있습니다. 고객의 목표가 결제까지라면 대화는 끝나지 않았습니다. "
               "done은 false로 하고 결제하겠다고 말하세요. 결제하지 않고 떠나는 것이 목표일 때만 done을 true로 둡니다.")

PENDING_NOTE = ("상담원이 방금 실행해도 되는지 물었고 고객의 대답을 기다리고 있습니다. 통화는 아직 끝나지 않았습니다. "
                "done은 false로 하고, 목표에 맞게 분명히 동의하거나 거절하는 말을 utterance에 쓰세요.")
EMPTY_NOTE = "utterance가 비어 있었습니다. 통화를 끝내려면 done을 true로, 아니면 고객의 다음 말을 utterance에 쓰세요."

MARKER_FACTS = {
    "[주문번호]": ("order_id", "order_number", "order"),
    "[전화번호]": ("phone_last4", "phone", "phone_number"),
    "[이름]": ("name", "customer_name"),
    "[이메일]": ("email",),
    "[주소]": ("address",),
    "[카드번호]": ("card_number", "card"),
    "[주민번호]": ("rrn", "resident_number"),
}
MARKER = re.compile(r"\[(?:주문번호|전화번호|이름|이메일|주소|카드번호|주민번호|가려짐)\]")


def verdict(expect, criteria):
    """(passed, mismatches). Passed iff every id in `expect` got that verdict and no criterion with
    safety=True failed. A criterion that is missing or could not be judged (None) is a mismatch."""
    by_id = {result.id: result for result in criteria}
    mismatches = [criterion_id for criterion_id, expected in expect.items()
                  if criterion_id not in by_id or by_id[criterion_id].passed is not bool(expected)]
    safety_failed = any(result.safety and result.passed is False for result in criteria)
    return (not mismatches and not safety_failed), mismatches


def fill_facts(text, facts):
    """Put real values back into a masked user turn: "[주문번호]" -> facts["order_id"], and
    "{order_id}" style placeholders."""
    for marker, names in MARKER_FACTS.items():
        if marker in text:
            for name in names:
                if facts.get(name) not in (None, ""):
                    text = text.replace(marker, str(facts[name]))
                    break
    for name, value in facts.items():
        text = text.replace("{" + str(name) + "}", str(value))
    return text


def unresolved_markers(case):
    """Masking markers that `facts` cannot fill; a case with any of these cannot be replayed faithfully."""
    found = []
    for turn in case.user_turns:
        for marker in MARKER.findall(fill_facts(turn, case.facts)):
            if marker not in found:
                found.append(marker)
    return found


def has_opening(definition):
    opening = getattr(definition, "opening", None)
    return bool(opening and (opening.say.strip() or opening.tools))


def persona_touches(persona, definition=None):
    """The touches a persona performs: `facts["touch"]`, a list of {action, item | line | arguments,
    options, quantity} (see screen.resolve_touch). Entries naming an action the definition does not
    have are dropped; without a definition every well-formed entry is kept."""
    entries = persona.facts.get("touch")
    entries = [entries] if isinstance(entries, dict) else entries if isinstance(entries, list) else []
    known = None if definition is None else {action.name: action for action in definition.actions}
    touches = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("action"):
            continue
        if known is not None and entry["action"] not in known:
            deps.log.warning("persona %s: touch names unknown action %r; ignored", persona.id, entry["action"])
            continue
        touches.append(entry)
    return touches


class SimulatedCustomer:
    """The LLM as a customer. The persona's facts go only into this prompt, never to the agent.

    With a screen (docs/DISPLAY.md): `greeted()` gives it the agent's opening words and the menu on
    screen, `heard(..., displays)` keeps the order it sees, and a persona with `facts["touch"]` may
    answer a turn with a touch instead of an utterance (`take_touch()` after `next()`).
    """

    def __init__(self, persona: TestPersona, llm, definition=None, touch=True):
        self.persona = persona
        self.llm = llm
        self.history = []           # [(customer utterance or touch note, agent reply)]
        self.last_progress = ""
        self.greeting = None        # the agent's opening words, when it spoke first
        self.menu = None            # data of the menu display
        self.cart = None            # data of the latest cart display
        self.screen = ""            # the latest confirm / receipt / notice line
        self.touches = persona_touches(persona, definition) if touch else []
        self.touched = set()        # indexes into self.touches already performed (or given up)
        self._touch = None
        self._actions = {} if definition is None else {action.name: action for action in definition.actions}

    def _system(self):
        facts = "\n".join(f"- {name}: {value}" for name, value in self.persona.facts.items()
                          if name != "touch") or "- (없음)"
        system = CUSTOMER_SYSTEM.format(description=self.persona.description, goal=self.persona.goal, facts=facts)
        if self.menu:
            system += SCREEN_SYSTEM.format(menu=menu_text(self.menu))
        if self.touches:
            rows = []
            for number, touch in enumerate(self.touches, start=1):
                action = self._actions.get(touch["action"])
                state = "이미 눌렀음" if number - 1 in self.touched else "아직 안 눌렀음"
                rows.append(f"{number}. {describe_touch(touch, action.description if action else '')} ({state})")
            system += TOUCH_SYSTEM.format(touches="\n".join(rows))
        return system

    def _schema(self):
        if not self.touches:
            return CUSTOMER_SCHEMA
        properties = dict(CUSTOMER_SCHEMA["properties"], touch={"type": "integer"})
        return {"type": "object", "properties": properties, "required": ["progress", "done", "touch", "utterance"]}

    def _screen_now(self):
        lines = []
        if self.cart is not None:
            lines.append(render_display({"kind": "cart", "data": self.cart}))
        if self.screen:
            lines.append(self.screen)
        return ("\n\n지금 화면:\n" + "\n".join(lines)) if lines else ""

    def _prompt(self, note=""):
        if note:
            return self._prompt() + "\n\n" + note
        if not self.history:
            if self.greeting is None:
                return "통화가 방금 연결되었습니다. 고객의 첫 마디를 정하세요. 용건을 짧게 말합니다. done은 false입니다."
            return (f"고객이 화면 앞에 섰고 상담원이 먼저 말했습니다.\n상담원: {self.greeting or '(말 없음)'}"
                    f"{self._screen_now()}\n\n고객의 첫 마디를 정하세요. 용건을 짧게 말합니다. done은 false입니다.")
        lines = []
        if self.greeting:
            lines.append(f"상담원: {self.greeting}")
        for customer, agent in self.history:
            lines.append(f"고객: {customer}")
            lines.append(f"상담원: {agent or '(답 없음)'}")
        return ("지금까지의 통화:\n" + "\n".join(lines) + self._screen_now() +
                "\n\n상담원의 마지막 말을 보고 통화를 끝낼지 정하고, 고객의 다음 말을 만드세요.")

    async def next(self, note=""):
        """-> (utterance, done). `note` is appended to the prompt (used when a decision is asked again).
        When the customer chose to touch the screen instead, the utterance is "" and `take_touch()`
        hands out the touch."""
        messages = [{"role": "system", "content": self._system()}, {"role": "user", "content": self._prompt(note)}]
        decision = await self.llm.complete_json(messages, self._schema(), temperature=0.4, num_predict=200)
        self.last_progress = str(decision.get("progress", ""))
        utterance = " ".join(str(decision.get("utterance", "")).split())
        self._touch = None
        number = decision.get("touch")
        if (self.touches and isinstance(number, int) and not isinstance(number, bool)
                and 1 <= number <= len(self.touches) and number - 1 not in self.touched):
            self._touch, utterance = number - 1, ""
        return utterance, bool(decision.get("done")) and bool(self.history) and self._touch is None

    def take_touch(self):
        """The touch chosen by the last `next()`: (index, entry) or None. Handed out once."""
        index, self._touch = self._touch, None
        return None if index is None else (index, self.touches[index])

    def greeted(self, text, displays=()):
        self.greeting = text or ""
        self._see(displays)

    def heard(self, utterance, agent_text, displays=()):
        self.history.append((utterance, agent_text))
        self._see(displays)

    def _see(self, displays):
        displays = [display for display in displays or [] if isinstance(display, dict)]
        self.menu = latest(displays, "menu") or self.menu
        shown = [display for display in displays if display.get("kind") in ("confirm", "receipt", "notice")]
        if displays:
            self.screen = (render_display(shown[-1]) or "") if shown else ""
        cart = latest(displays, "cart")
        if cart is not None:
            self.cart = cart


def _mask_deep(value):
    if isinstance(value, str):
        return deps.mask_text(value)
    if isinstance(value, dict):
        return {key: _mask_deep(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mask_deep(item) for item in value]
    return value


class _Talk:
    """One conversation through a driver, with a local log kept as a fallback record."""

    def __init__(self, driver):
        self.driver = driver
        self.log = []               # [{user_text, turn: AgentTurn, t_user_end, t_end, action}] customer turns
        self.opened = None          # the same for the agent's opening (turn 0), when it ran
        self.handoff = False

    async def _take(self, user_text, call, action=None):
        t_user_end = time.time()
        turn = await call
        entry = {"user_text": user_text, "turn": turn, "t_user_end": t_user_end, "t_end": time.time(),
                 "action": action}
        self.handoff = self.handoff or bool(turn.handoff)
        return entry

    async def say(self, text):
        entry = await self._take(text, self.driver.say(text))
        self.log.append(entry)
        return entry["turn"]

    async def opening(self):
        """Lets the agent speak first (ConversationDriver.opening). None when the driver cannot."""
        if not callable(getattr(self.driver, "opening", None)):
            deps.log.warning("the definition has an opening but the driver has no opening(); skipped")
            return None
        self.opened = await self._take("", self.driver.opening())
        return self.opened["turn"]

    async def act(self, name, arguments, text=""):
        """A touch on the screen (ConversationDriver.act); counts as a customer turn."""
        entry = await self._take(text, self.driver.act(name, arguments),
                                 {"name": name, "arguments": dict(arguments)})
        self.log.append(entry)
        return entry["turn"]

    def fallback_session(self, agent_id, version, label, simulated, definition=None):
        """What the trace store would hold, rebuilt from the driver's answers (masked here)."""
        session_id = self.driver.session_id
        tools = {} if definition is None else {action.name: action.tool for action in definition.actions}
        turns, spans = [], []
        entries = ([(0, self.opened)] if self.opened else []) + list(enumerate(self.log, start=1))
        for number, entry in entries:
            turn = entry["turn"]
            start = entry["t_user_end"] + turn.ms / 1000.0 if turn.ms else None
            turns.append({"session_id": session_id, "turn": number,
                          "user_text": deps.mask_text(entry["user_text"]), "agent_text": deps.mask_text(turn.text),
                          "t_user_end": entry["t_user_end"], "t_agent_start": start, "t_agent_end": entry["t_end"],
                          "interrupted": False, "handoff": bool(turn.handoff)})
            step = iter(range(1, 10_000))       # keeps the order: touch, tool calls, screens

            def span(kind, name, **fields):
                t = entry["t_user_end"] + next(step) * 1e-4
                return {"session_id": session_id, "turn": number, "kind": kind, "name": name, "t_start": t,
                        "t_end": t, "ok": True, **fields}

            if entry.get("action"):
                action = entry["action"]
                spans.append(span("action", action["name"], input={"arguments": _mask_deep(action["arguments"])},
                                  attrs={"tool": tools.get(action["name"], "")}))
            for call in turn.tool_calls:
                spans.append(span("tool", call.get("name", ""), ok=call.get("ok", True),
                                  attrs={"effect": call.get("effect", ""), "executed": call.get("executed", False),
                                         "needs_confirmation": call.get("needs_confirmation", False),
                                         "error_type": call.get("error_type")}))
            for display in turn.displays:
                if isinstance(display, dict) and display.get("kind"):
                    spans.append(span("display", display["kind"], output={"data": _mask_deep(display.get("data"))},
                                      attrs={"source": display.get("source", "")}))
        return {"meta": {"session_id": session_id, "agent_id": agent_id, "version": version,
                         "channel": "text", "test": True, "simulated": simulated, "label": label},
                "status": "handoff" if self.handoff else "completed", "turns": turns, "spans": spans}


async def _stored_session(talk, store, agent_id, version, label, simulated, definition=None):
    """The stored (masked) record of the conversation; the local log when the store has none."""
    await asyncio.to_thread(deps.flush_recorder, store)
    for attempt in range(3):
        try:
            session = await load_session(talk.driver.session_id, store)
            if len([turn for turn in session.get("turns") or [] if (turn.get("turn") or 0) >= 1]) >= len(talk.log):
                return session
        except LookupError:
            pass
        except Exception as e:
            deps.log.warning("could not read session %s: %s", talk.driver.session_id, e)
            break
        if attempt < 2:
            await asyncio.sleep(0.3)
    deps.log.warning("session %s is not (fully) in the trace store; analysing the driver's own log",
                     talk.driver.session_id)
    return talk.fallback_session(agent_id, version, label, simulated, definition)


async def _run_item(kind, item, agent_id, version, play, *, store, definition_store, llm, driver_factory,
                    definition):
    """Open a conversation, let `play(talk)` hold it, close, analyse, compare with `expect`."""
    session_id = ""
    try:
        store = deps.get_store(store)
        llm = deps.get_llm(llm)
        if definition is None:
            definition = deps.get_definition_store(definition_store).load(agent_id, version)
        version = definition.version if version is None else version
        driver = await deps.get_driver_factory(driver_factory)(
            agent_id, version, label=item.id, simulated=(kind == "persona"))
        session_id = driver.session_id
        talk = _Talk(driver)
        try:
            await play(talk, llm, definition)
        finally:
            try:
                await driver.close("handoff" if talk.handoff else "completed")
            except Exception as e:
                deps.log.warning("closing %s failed: %s", session_id, e)
        session = await _stored_session(talk, store, agent_id, version, item.id, kind == "persona", definition)
        analysis = await analyze_session(session_id, store=store, llm=llm, definition=definition, session=session)
        passed, mismatches = verdict(item.expect, analysis.criteria)
        return TestItemResult(item_id=item.id, kind=kind, session_id=session_id, passed=passed,
                              criteria=analysis.criteria, mismatches=mismatches, metrics=analysis.metrics)
    except Exception as e:
        deps.log.exception("test item %s failed to run", item.id)
        error = CriterionResult(id="_error", passed=None, reason=f"시험을 끝까지 돌리지 못했습니다: {e!r}"[:400])
        return TestItemResult(item_id=item.id, kind=kind, session_id=session_id or f"none-{uuid.uuid4().hex[:8]}",
                              passed=False, criteria=[error], mismatches=["_error"] + list(item.expect))


async def run_persona(agent_id, version, persona, *, store=None, definition_store=None, llm=None,
                      driver_factory=None, definition=None) -> TestItemResult:
    """A simulated customer talks to the agent until its goal is settled, the agent hands off,
    or `max_turns` customer turns (utterances and touches) were taken. When the definition has an
    opening the agent speaks first and the customer reads the menu it put on screen."""
    persona = persona if isinstance(persona, TestPersona) else TestPersona.model_validate(persona)

    async def play(talk, llm, definition):
        can_touch = callable(getattr(talk.driver, "act", None))
        customer = SimulatedCustomer(persona, llm, definition, touch=can_touch)
        if has_opening(definition):
            first = await talk.opening()        # the agent speaks first and puts the menu on screen
            if first is not None:
                customer.greeted(first.text, first.displays)

        async def decide(note=""):
            """-> (utterance, touch, done); touch is (name, arguments, text) ready for the driver."""
            utterance, done = await customer.next(note)
            for _ in range(2):
                chosen = customer.take_touch()
                if chosen is None:
                    return utterance, None, done
                index, entry = chosen
                customer.touched.add(index)     # performed, or given up: either way not offered again
                try:
                    arguments = resolve_touch(entry, customer.menu, customer.cart)
                except LookupError as e:
                    deps.log.info("persona %s could not touch %r: %s", persona.id, entry, e)
                    what = entry.get("line") or entry.get("item") or entry["action"]
                    utterance, done = await customer.next(TOUCH_FAILED_NOTE.format(what=what))
                    continue
                return "", (entry["action"], arguments, f"(화면에서 누름: {describe_touch(entry)})"), False
            return utterance, None, done

        utterance, touch, asked_unpaid = persona.opening, None, False
        if not utterance:
            utterance, touch, _ = await decide()
        for _ in range(max(1, persona.max_turns)):
            if not utterance and touch is None:
                break
            if touch is not None:
                name, arguments, said = touch
                turn = await talk.act(name, arguments, said)
            else:
                said = utterance
                turn = await talk.say(utterance)
            customer.heard(said, turn.text, turn.displays)
            if talk.handoff or len(talk.log) >= persona.max_turns:
                break
            utterance, touch, done = await decide()
            pending = any(call.get("needs_confirmation") for call in turn.tool_calls)
            if done and pending:
                # the agent is waiting for a yes or no to a proposed change: hanging up here would
                # leave the goal undecided, so the customer is asked once more
                deps.log.info("persona %s wanted to end on an open proposal; asked again", persona.id)
                utterance, touch, done = await decide(PENDING_NOTE)
                done = done and not utterance and touch is None
            elif not done and not utterance and touch is None:
                deps.log.info("persona %s gave an empty utterance; asked again", persona.id)
                utterance, touch, done = await decide(EMPTY_NOTE)
            elif done and not asked_unpaid and (customer.cart or {}).get("lines"):
                # leaving with an order on screen that was never paid: right for a customer who
                # changed their mind, a slip for one who came to buy. Asked once per conversation.
                asked_unpaid = True
                deps.log.info("persona %s wanted to end with an unpaid order on screen; asked again", persona.id)
                utterance, touch, done = await decide(UNPAID_NOTE)
            if done:            # the closing remark is not sent: the conversation is over
                break

    return await _run_item("persona", persona, agent_id, version, play, store=store,
                           definition_store=definition_store, llm=llm, driver_factory=driver_factory,
                           definition=definition)


async def run_case(agent_id, version, case, *, store=None, definition_store=None, llm=None,
                   driver_factory=None, definition=None) -> TestItemResult:
    """Replays the stored user turns in order (markers filled from `facts`); stops at a handoff."""
    case = case if isinstance(case, TestCase) else TestCase.model_validate(case)

    async def play(talk, llm, definition):
        if has_opening(definition):
            await talk.opening()    # as on a real connection: the menu is on screen and in the history
        for text in case.user_turns:
            await talk.say(fill_facts(text, case.facts))
            if talk.handoff:
                break

    return await _run_item("case", case, agent_id, version, play, store=store,
                           definition_store=definition_store, llm=llm, driver_factory=driver_factory,
                           definition=definition)
