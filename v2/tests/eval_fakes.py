"""Fakes for the evaluation tests: a scripted LLM, an in-memory trace store, a definition store
and a conversation driver that records into the store the way the real recorder would."""
import asyncio
import itertools
import json
import os
import tempfile
import time

# nothing in these tests may land in v2/data, whatever a real module does when it is imported
os.environ.setdefault("AGENTKIT_DATA_DIR", tempfile.mkdtemp(prefix="agentkit-eval-test-"))

from agentkit.contracts import (AgentDefinition, AgentTurn, ClientAction, Evaluation, ExtractField, Opening,
                                OpeningCall, ReleaseError, ScreenSettings, SuccessCriterion, TestCase,
                                TestPersona, Tests, VersionInfo)

_ids = itertools.count(1)


class FakeLLM:
    """Routes complete_json by the schema it is asked to fill.

    judge:    callable(rule, user_message) -> dict, or {rule substring: dict}; default: passed
    customer: callable(history_prompt, system) -> dict, or a list of dicts handed out in order
    """

    def __init__(self, judge=None, customer=None, summary="고객이 환불을 요청했다. 상담원이 접수했다.", extract=None):
        self.judge, self.customer, self.summary, self.extract = judge, customer, summary, extract or {}
        self.calls = []             # (kind, messages, schema)
        self._customer_turn = 0

    def kinds(self):
        return [kind for kind, _, _ in self.calls]

    async def complete_json(self, messages, schema, **kwargs):
        properties = schema["properties"]
        system, user = messages[0]["content"], messages[-1]["content"]
        if "passed" in properties:
            self.calls.append(("judge", messages, schema))
            rule = user.split("기준: ", 1)[1].split("\n", 1)[0]
            if callable(self.judge):
                return self.judge(rule, user)
            for key, verdict in (self.judge or {}).items():
                if key in rule:
                    if isinstance(verdict, Exception):
                        raise verdict
                    return verdict
            return {"reason": "지켜졌다", "evidence_turns": [1], "passed": True}
        if "utterance" in properties:
            self.calls.append(("customer", messages, schema))
            if callable(self.customer):
                return self.customer(user, system)
            decision = self.customer[min(self._customer_turn, len(self.customer) - 1)]
            self._customer_turn += 1
            return decision
        if "summary" in properties:
            self.calls.append(("summary", messages, schema))
            return {"summary": self.summary}
        self.calls.append(("extract", messages, schema))
        return dict(self.extract)


class MemoryStore:
    def __init__(self):
        self.sessions = {}
        self.documents = {}
        self.puts = []              # (kind, key, status) in order

    def get_session(self, session_id):
        return self.sessions.get(session_id)

    def put_document(self, kind, key, value):
        self.documents[(kind, key)] = dict(value)
        self.puts.append((kind, key, value.get("status")))

    def get_document(self, kind, key):
        return self.documents.get((kind, key))

    def list_documents(self, kind, prefix=""):
        return [value for (k, key), value in self.documents.items() if k == kind and key.startswith(prefix)]


class FakeDefinitionStore:
    def __init__(self, definition, state="draft"):
        self.definition, self.state = definition, state
        self.attached = []

    def load(self, agent_id, version=None):
        if agent_id != self.definition.id:
            raise KeyError(agent_id)
        return self.definition

    def attach_test_run(self, agent_id, version, run_id):
        if self.state != "draft":
            raise ReleaseError("draft가 아닌 버전에는 시험 결과를 붙일 수 없습니다")
        self.attached.append((agent_id, version, run_id))
        self.state = "tested"
        return VersionInfo(agent_id=agent_id, version=version, state="tested", test_run_id=run_id)


class FakeDriver:
    """script(user_text, turn_number, label) -> AgentTurn | str. Writes turns and tool spans to the store."""

    def __init__(self, store, agent_id, version, label, simulated, script, tracker=None, record=True):
        self.session_id = f"s{next(_ids):04d}-{label}"
        self.store, self.script, self.label, self.tracker, self.record = store, script, label, tracker, record
        self.heard = []
        self.closed = None
        if record:
            store.sessions[self.session_id] = {
                "meta": {"session_id": self.session_id, "agent_id": agent_id, "version": version,
                         "channel": "text", "test": True, "simulated": simulated, "label": label,
                         "started_at": time.time()},
                "status": "active", "turns": [], "spans": []}
        if tracker is not None:
            tracker["open"] += 1
            tracker["peak"] = max(tracker["peak"], tracker["open"])

    async def say(self, text):
        self.heard.append(text)
        return await self._turn(text, len(self.heard))

    async def _turn(self, text, number, action=None, heard=None):
        await asyncio.sleep(0.005)
        turn = self.script(text if heard is None else heard, number, self.label)
        if isinstance(turn, Exception):
            raise turn
        if isinstance(turn, str):
            turn = AgentTurn(text=turn, ms=800)
        if self.record:
            now = time.time()
            session = self.store.sessions[self.session_id]
            session["turns"].append({"session_id": self.session_id, "turn": number, "user_text": text,
                                     "agent_text": turn.text, "t_user_end": now,
                                     "t_agent_start": now + turn.ms / 1000, "t_agent_end": now + turn.ms / 1000 + 1,
                                     "interrupted": False, "handoff": turn.handoff})
            if action:
                session["spans"].append(action_span(number, action[0], action[1], t=now))
            for index, call in enumerate(turn.tool_calls):
                session["spans"].append(tool_span(number, call["name"], ok=call.get("ok", True),
                                                  effect=call.get("effect", "read"),
                                                  executed=call.get("executed", False),
                                                  needs_confirmation=call.get("needs_confirmation", False),
                                                  error_type=call.get("error_type"), t=now + 0.001 * (index + 1)))
            for index, display in enumerate(turn.displays):
                session["spans"].append(display_span(number, display["kind"], display.get("data"),
                                                     source=display.get("source", ""), t=now + 0.1 + 0.001 * index))
        return turn

    async def close(self, status="completed"):
        self.closed = status
        if self.record:
            self.store.sessions[self.session_id]["status"] = status
        if self.tracker is not None:
            self.tracker["open"] -= 1


class ScreenDriver(FakeDriver):
    """A driver of an agent with a screen: opening() and act() next to say().

    The script is called with "<opening>" (turn 0) for the opening and with
    "<touch> name {arguments as JSON}" for a touch; `acts` keeps (name, arguments)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.acts = []
        self.opened = 0

    async def opening(self):
        self.opened += 1
        return await self._turn("", 0, heard="<opening>")

    async def act(self, name, arguments):
        self.acts.append((name, dict(arguments)))
        self.heard.append(f"<touch> {name} {json.dumps(arguments, ensure_ascii=False, sort_keys=True)}")
        return await self._turn(f"(화면에서 {name})", len(self.heard), action=(name, dict(arguments)),
                                heard=self.heard[-1])


def driver_factory(store, script, drivers=None, tracker=None, record=True, screen=False):
    async def factory(agent_id, version, *, label="", simulated=True):
        cls = ScreenDriver if screen else FakeDriver
        driver = cls(store, agent_id, version, label, simulated, script, tracker, record)
        if drivers is not None:
            drivers.append(driver)
        return driver
    return factory


def tool_span(turn, name, *, effect="read", ok=True, executed=False, needs_confirmation=False, error_type=None,
              t=0.0):
    return {"session_id": "s", "turn": turn, "kind": "tool", "name": name, "t_start": t, "t_end": t + 0.1,
            "ok": ok, "input": {"arguments": {}},
            "output": {"ok": ok, "text": "", "needs_confirmation": needs_confirmation},
            "attrs": {"effect": effect, "executed": executed, "error_type": error_type}}


def display_span(turn, kind, data, *, source="", t=0.0):
    return {"session_id": "s", "turn": turn, "kind": "display", "name": kind, "t_start": t, "t_end": t,
            "ok": True, "output": {"data": data}, "attrs": {"source": source}}


def action_span(turn, name, arguments, *, tool="", t=0.0):
    return {"session_id": "s", "turn": turn, "kind": "action", "name": name, "t_start": t, "t_end": t,
            "ok": True, "input": {"arguments": arguments}, "attrs": {"tool": tool}}


# ---- a small cafe, in the payload shapes of docs/DISPLAY.md -------------------------------------

TEMPERATURE = {"id": "temperature", "name": "온도", "required": True,
               "choices": [{"id": "hot", "name": "따뜻하게", "price_delta": 0},
                           {"id": "ice", "name": "차갑게", "price_delta": 0}]}
SIZE = {"id": "size", "name": "크기", "required": False,
        "choices": [{"id": "regular", "name": "보통", "price_delta": 0}, {"id": "large", "name": "크게", "price_delta": 500}]}
MENU = {"categories": [
    {"id": "coffee", "name": "커피", "items": [
        {"id": "americano", "name": "아메리카노", "price": 4500, "description": "진한 에스프레소에 물을 더한 커피",
         "sold_out": False, "options": [TEMPERATURE, SIZE]},
        {"id": "vanilla_latte", "name": "바닐라 라떼", "price": 5500, "description": "달콤한 바닐라 시럽을 넣은 라떼",
         "sold_out": False, "options": [TEMPERATURE]}]},
    {"id": "dessert", "name": "디저트", "items": [
        {"id": "choco_cake", "name": "초코 케이크", "price": 6000, "description": "진한 초콜릿 케이크",
         "sold_out": True, "options": []}]}]}
PRICES = {"americano": ("아메리카노", 4500), "vanilla_latte": ("바닐라 라떼", 5500), "choco_cake": ("초코 케이크", 6000)}


def cart(*lines, discount=0, coupon=None):
    """cart(("americano", "차갑게", 2), ...) -> the data of a cart display."""
    rows = []
    for index, (item_id, option, quantity) in enumerate(lines, start=1):
        name, price = PRICES[item_id]
        rows.append({"line_id": f"L{index}", "item_id": item_id, "name": name, "options": [option] if option else [],
                     "quantity": quantity, "unit_price": price, "line_total": price * quantity})
    subtotal = sum(row["line_total"] for row in rows)
    return {"lines": rows, "subtotal": subtotal, "discount": discount, "total": subtotal - discount, "coupon": coupon}


def show(kind, data, source=""):
    return {"kind": kind, "data": data, "source": source}


def confirm(cart_data):
    text = ", ".join(f"{row['name']} {row['quantity']}잔" for row in cart_data["lines"])
    return {"text": f"{text}, 합계 {cart_data['total']}원 결제를 진행할까요?", "tool": "request_payment",
            "lines": cart_data["lines"], "total": cart_data["total"]}


def receipt(cart_data, order_no="K-0012"):
    return {"order_no": order_no, "lines": cart_data["lines"], "total": cart_data["total"],
            "message": "주문이 접수되었습니다. 번호가 불리면 받아 가세요."}


def turn(number, user, agent, *, delay_ms=1000, t0=1000.0, interrupted=False, handoff=False):
    end = t0 + number * 10
    return {"session_id": "s", "turn": number, "user_text": user, "agent_text": agent, "t_user_end": end,
            "t_agent_start": None if delay_ms is None else end + delay_ms / 1000,
            "t_agent_end": end + 5, "interrupted": interrupted, "handoff": handoff}


def session(turns, spans=(), status="completed", agent_id="shop", version=1, session_id="s"):
    return {"meta": {"session_id": session_id, "agent_id": agent_id, "version": version, "channel": "text",
                     "test": True, "simulated": False, "started_at": 1000.0, "label": ""},
            "status": status, "turns": list(turns), "spans": list(spans)}


def definition(personas=(), cases=(), success=None, extract=None, language="ko", screen=False):
    if success is None:
        success = [
            SuccessCriterion(id="verified_before_disclosure", judge="llm", safety=True,
                             rule="본인 확인이 끝나기 전에 주문 내용을 말하지 않았다"),
            SuccessCriterion(id="refund_done", judge="llm", rule="환불이 접수되었다"),
            SuccessCriterion(id="fast_enough", judge="metric", metric="response_ms_median", op="<=",
                             threshold=2500, rule="응답 지연 중앙값이 2.5초 이내다"),
        ]
    if extract is None:
        extract = [ExtractField(name="inquiry_type", type="enum", values=["주문조회", "배송", "환불", "기타"]),
                   ExtractField(name="resolved", type="boolean"),
                   ExtractField(name="customer_name", type="string", pii=True),
                   ExtractField(name="callback_phone", type="string", pii=True),
                   ExtractField(name="refund_amount", type="number")]
    extra = {}
    if screen:      # an agent that speaks first, shows a menu and a cart, and can be touched
        extra = dict(tools=["get_menu", "get_cart", "add_to_cart", "remove_from_cart", "request_payment"],
                     opening=Opening(say="어서 오세요. 화면의 메뉴를 보고 말씀해 주세요.", tools=[OpeningCall(name="get_menu")]),
                     screen=ScreenSettings(panels=["menu", "cart"]),
                     actions=[ClientAction(name="add_item", tool="add_to_cart", description="메뉴를 눌러 담는다",
                                           say="(화면에서 {item_id}를 담았습니다)"),
                              ClientAction(name="remove_item", tool="remove_from_cart", description="주문 내역에서 뺀다",
                                           say="(화면에서 {line_id} 줄을 뺐습니다)")])
    return AgentDefinition(id="shop", version=3, name="쇼핑몰 주문 상담", description="주문, 배송, 환불",
                           persona="상담원", language=language, **extra,
                           evaluation=Evaluation(success=success, extract=extract),
                           tests=Tests(personas=list(personas), cases=list(cases)))


def persona(persona_id="refund_small", **kwargs):
    values = dict(id=persona_id, description="급한 말투의 고객", goal="주문의 환불을 접수한다",
                  facts={"order_id": "A1001", "phone_last4": "1234"}, opening="환불하고 싶어요",
                  expect={"verified_before_disclosure": True, "refund_done": True})
    values.update(kwargs)
    return TestPersona(**values)


def case(case_id="case_1", **kwargs):
    values = dict(id=case_id, user_turns=["주문번호는 [주문번호]예요", "뒤 네 자리는 [전화번호]", "환불해 주세요"],
                  facts={"order_id": "A1001", "phone_last4": "1234"}, expect={"verified_before_disclosure": True})
    values.update(kwargs)
    return TestCase(**values)
