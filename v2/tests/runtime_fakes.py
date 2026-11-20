"""Fakes for the runtime tests: a scripted LLM, a tool gateway, a recorder, a small shop.

Nothing here depends on the definition, tools or trace modules; only on contracts.py.
"""
import asyncio
import os
import re
import tempfile

# before agentkit.config is read: the tests never write into v2/data
os.environ.setdefault("AGENTKIT_DATA_DIR", tempfile.mkdtemp(prefix="agentkit-runtime-test-"))

from datetime import datetime
from zoneinfo import ZoneInfo

from agentkit.contracts import AdapterSpec, AgentDefinition, ToolContract, ToolResult

FIXED_NOW = datetime(2026, 11, 17, 15, 5, tzinfo=ZoneInfo("Asia/Seoul"))   # a Tuesday


def fixed_now():
    return FIXED_NOW


def text(value):
    return ("text", value)


def tool(name, **arguments):
    return ("tool", {"name": name, "arguments": arguments})


class ScriptedLLM:
    """stream() plays the next script: a list of ("text", ...) / ("tool", {...}) items, or a
    callable(messages, tools) returning such a list. Every call is kept in `calls`."""

    def __init__(self, scripts=None, responder=None, delay=0.0, tools_supported=True):
        self.scripts = list(scripts or [])
        self.responder = responder
        self.delay = delay
        self.tools_supported = tools_supported
        self.calls = []

    async def supports(self, capability, model=None):
        return self.tools_supported if capability == "tools" else False

    async def stream(self, messages, *, model=None, tools=None, num_predict=1500, num_ctx=8192, temperature=None):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools, "model": model,
                           "num_predict": num_predict, "num_ctx": num_ctx})
        if self.responder is not None:
            script = self.responder(messages, tools)
        elif self.scripts:
            script = self.scripts.pop(0)
            if callable(script):
                script = script(messages, tools)
        else:
            script = [text("더 드릴 말씀이 없습니다.")]
        if isinstance(script, Exception):
            raise script
        for item in script:
            if isinstance(item, (int, float)):
                await asyncio.sleep(item)
                continue
            if self.delay:
                await asyncio.sleep(self.delay)
            yield item
        yield "stats", {"total_s": 0.01, "load_s": 0.0, "prompt_tokens": 10, "output_tokens": 5}

    async def complete(self, messages, **kwargs):
        return "".join(value for kind, value in (self.scripts.pop(0) if self.scripts else []) if kind == "text")

    def system(self, call=-1):
        return self.calls[call]["messages"][0]["content"]

    def last_user(self, call=-1):
        return [m for m in self.calls[call]["messages"] if m["role"] == "user"][-1]["content"]


class FakeRecorder:
    def __init__(self):
        self.sessions, self.spans, self.turns, self.ended = [], [], [], []

    def start_session(self, meta):
        self.sessions.append(meta)

    def span(self, span, pii_paths=None):
        self.spans.append(span)

    def turn(self, turn):
        self.turns.append(turn)

    def audio(self, session_id, turn, role, pcm16):
        pass

    def end_session(self, session_id, status):
        self.ended.append((session_id, status))

    def kinds(self, session_id=None):
        return [s.kind for s in self.spans if session_id in (None, s.session_id)]

    def of(self, kind):
        return [s for s in self.spans if s.kind == kind]

    def last_turns(self):
        """The newest record of each (session, turn): the store keeps one per turn."""
        latest = {}
        for record in self.turns:
            latest[(record.session_id, record.turn)] = record
        return list(latest.values())


def contract(name, effect="read", confirm=None, filler=None, properties=None, required=None, description=""):
    return ToolContract(
        name=name, description=description or f"{name} 도구", effect=effect,
        input_schema={"type": "object", "properties": properties or {}, "required": required or []},
        confirm_template=confirm, filler=filler, adapter=AdapterSpec(kind="dummy", target=f"fake:{name}"))


class FakeGateway:
    """handlers: {name: callable(ctx, arguments) -> ToolResult} (sync or async). Calls are kept."""

    def __init__(self, contracts, handlers=None):
        self.contracts = {c.name: c for c in contracts}
        self.handlers = handlers or {}
        self.calls = []            # (ctx, name, arguments)
        self.ended = []

    def names(self):
        return list(self.contracts)

    def contract(self, name):
        return self.contracts[name]

    def specs(self, tool_names):
        return [{"type": "function", "function": {
            "name": name, "description": self.contracts[name].description,
            "parameters": self.contracts[name].input_schema}} for name in tool_names]

    async def call(self, ctx, name, arguments):
        self.calls.append((ctx, name, dict(arguments)))
        handler = self.handlers.get(name)
        if handler is None:
            return ToolResult(ok=True, text=f"{name} 결과", ms=1.0)
        result = handler(ctx, arguments)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    def facts(self, session_id):
        return set()

    def end_session(self, session_id):
        self.ended.append(session_id)


class FakeKnowledge:
    def __init__(self, hits, name="시험 지식"):
        self.hits = hits
        self.name = name
        self.queries = []
        self.closed = False

    async def search(self, query, timeout=None):
        self.queries.append(query)
        if isinstance(self.hits, Exception):
            raise self.hits
        return list(self.hits), 3.0

    def context(self, hits, threshold, language="ko"):
        from agentkit.runtime.knowledge import knowledge_context
        return knowledge_context(hits, threshold, self.name, language)

    async def close(self):
        self.closed = True


SHOP_PERSONA = ("당신은 온라인 쇼핑몰의 주문 상담원입니다. 고객과 음성으로 대화합니다.\n"
                "한국어 구어체로 정중하고 간결하게 말합니다.")


def make_definition(**overrides):
    """A shop-support-like definition, written here so the tests do not need the definition module."""
    values = dict(
        id="test-shop", version=3, name="시험 상담", persona=SHOP_PERSONA, language="ko", reply_length="short",
        tools=["verify_customer", "get_order", "request_refund", "handoff_to_human"],
        flow=[{"id": "verify_first", "rule": "주문 내용을 말하기 전에 주문번호와 휴대폰 뒤 4자리로 본인을 확인한다"},
              {"id": "confirm_refund", "rule": "환불은 상품명과 금액을 읽어 주고 동의를 받은 뒤 접수한다"}],
        handoff=[{"id": "large_refund", "when": "환불 금액이 100000원을 넘는다"},
                 {"id": "angry", "when": "고객이 상담원을 바꿔 달라고 요구한다"}],
        prohibitions=["본인 확인 전에 주문 내용, 주소, 결제 정보를 말하지 않는다"],
        guards=[],
    )
    values.update(overrides)
    return AgentDefinition.model_validate(values)


SHOP_CONTRACTS = [
    contract("verify_customer", filler="본인 확인을 하고 있어요.",
             description="주문번호와 휴대폰 뒤 4자리로 본인을 확인한다. 주문 내용을 조회하기 전에 먼저 사용한다.",
             properties={"order_id": {"type": "string", "description": "주문번호"},
                         "phone_last4": {"type": "string", "description": "휴대폰 번호 뒤 4자리"}},
             required=["order_id", "phone_last4"]),
    contract("get_order", filler="주문을 확인해 볼게요.",
             description="주문의 상품명, 금액, 배송 상태를 조회한다. 본인 확인이 끝난 뒤에만 쓸 수 있다.",
             properties={"order_id": {"type": "string", "description": "주문번호"}}, required=["order_id"]),
    contract("request_refund", effect="write", confirm="{product_name} {amount}원 환불을 접수할까요?",
             filler="환불을 접수하고 있어요.",
             description="주문의 환불을 접수한다. 고객이 환불을 요청했을 때 사용한다.",
             properties={"order_id": {"type": "string", "description": "주문번호"},
                         "reason": {"type": "string", "description": "환불 사유"}},
             required=["order_id", "reason"]),
    contract("handoff_to_human", effect="write",
             description="대화를 사람 상담원에게 넘긴다.",
             properties={"reason": {"type": "string", "description": "넘기는 이유"}}, required=["reason"]),
]

CONSENT = re.compile(r"(네|예|응|맞아요|그래요|좋아요|해\s?주세요|접수해|yes)", re.I)
REFUSAL = re.compile(r"(아니|취소|잠깐|하지\s?마|말고)")


class MiniShop(FakeGateway):
    """A tool gateway with the rules of ARCHITECTURE section 4, small enough to read at a glance.
    Used when the real tools module is not there: verification first, two-phase refund, idempotency."""

    ORDERS = {
        "A1001": {"phone_last4": "1234", "product_name": "무선 이어폰", "amount": 30000, "status": "배송 완료"},
        "A1002": {"phone_last4": "5678", "product_name": "로봇 청소기", "amount": 250000, "status": "배송 완료"},
    }

    def __init__(self):
        super().__init__(SHOP_CONTRACTS)
        self.verified = {}       # session -> order id
        self.proposals = {}      # (session, order) -> turn of the proposal
        self.refunds = []        # executed refunds: (session, order)
        self.handoffs = []

    async def call(self, ctx, name, arguments):
        self.calls.append((ctx, name, dict(arguments)))
        order_id = str(arguments.get("order_id") or "").upper()
        order = self.ORDERS.get(order_id)
        if name == "verify_customer":
            if not order:
                return ToolResult(ok=False, error_type="not_found",
                                  text=f"주문번호 {order_id}를 찾지 못했습니다. 번호를 다시 확인해 달라고 하세요.")
            if str(arguments.get("phone_last4")) != order["phone_last4"]:
                return ToolResult(ok=False, error_type="permission_denied",
                                  text="휴대폰 뒤 4자리가 주문 정보와 다릅니다. 다시 확인해 달라고 하세요.")
            self.verified[ctx.session_id] = order_id
            return ToolResult(ok=True, text="본인 확인이 완료되었습니다.")
        if name == "handoff_to_human":
            self.handoffs.append((ctx.session_id, arguments.get("reason")))
            return ToolResult(ok=True, executed=True, text="상담원 연결을 접수했습니다.")
        if self.verified.get(ctx.session_id) != order_id or not order:
            return ToolResult(ok=False, error_type="precondition_failed",
                              text="본인 확인이 먼저 필요합니다. 주문번호와 휴대폰 뒤 4자리를 물어 verify_customer를 호출하세요.")
        if name == "get_order":
            return ToolResult(ok=True, data=order, text=(
                f"주문 {order_id}: 상품 {order['product_name']}, 금액 {order['amount']}원, 배송 상태 {order['status']}."))
        if name == "request_refund":
            key = (ctx.session_id, order_id)
            if order["amount"] > 100000:
                return ToolResult(ok=False, error_type="permission_denied", text=(
                    "환불 금액이 100000원을 넘어 여기서 접수할 수 없습니다. 상담원에게 연결하세요."))
            if key in self.refunds:
                return ToolResult(ok=True, executed=False, text="이미 접수된 환불입니다. 접수번호 R-1.")
            sentence = f"{order['product_name']} {order['amount']}원 환불을 접수할까요?"
            if key not in self.proposals or ctx.speculative:
                self.proposals.setdefault(key, ctx.turn)
                return ToolResult(ok=True, needs_confirmation=True, confirmation_text=sentence,
                                  text="환불 접수 전 고객 확인이 필요합니다.")
            agreed = CONSENT.search(ctx.last_user_text) and not REFUSAL.search(ctx.last_user_text)
            if ctx.turn <= self.proposals[key] or not agreed:
                return ToolResult(ok=False, error_type="not_confirmed", needs_confirmation=False, text=(
                    f"고객의 동의가 아직 없습니다. 다음 문장을 읽어 주고 동의를 받으세요: {sentence}"))
            self.refunds.append(key)
            return ToolResult(ok=True, executed=True, text=(
                f"환불이 접수되었습니다. 접수번호 R-1. {order['product_name']} {order['amount']}원."))
        return ToolResult(ok=False, error_type="invalid_input", text="알 수 없는 도구입니다.")


async def collect(engine, user_text, **options):
    """All events of one reply."""
    return [event async for event in engine.reply(user_text, **options)]


def types(events):
    return [event.type for event in events]


def done_text(events):
    return next(event.text for event in events if event.type == "done")


# ------------------------------------------------------------------------------------ a small cafe
# For the screen, opening and touch tests (docs/DISPLAY.md), without the tools module or the dummy cafe.

CAFE_CONTRACTS = [
    contract("get_menu", description="전체 메뉴를 조회한다."),
    contract("get_cart", description="지금 주문 내역을 조회한다."),
    contract("add_to_cart", effect="write", description="메뉴를 주문 내역에 담는다.",
             properties={"item_id": {"type": "string"}, "quantity": {"type": "integer"}}, required=["item_id"]),
    contract("request_payment", effect="write", confirm="합계 {total}원 결제를 진행할까요?",
             description="결제를 요청한다."),
    contract("handoff_to_human", effect="write", description="직원을 부른다.",
             properties={"reason": {"type": "string"}}, required=["reason"]),
]
CAFE_MENU = {"americano": ("아메리카노", 4500), "vanilla_latte": ("바닐라 라떼", 5500), "choco_cake": ("초코 케이크", 6000)}
SOLD_OUT = {"choco_cake"}


class MiniCafe(FakeGateway):
    """Menu, a cart per session, and the `display` lists a real gateway would attach."""

    def __init__(self):
        super().__init__(CAFE_CONTRACTS)
        self.carts = {}
        self.paid = []

    def menu(self):
        return {"categories": [{"id": "all", "name": "전체", "items": [
            {"id": key, "name": name, "price": price, "sold_out": key in SOLD_OUT}
            for key, (name, price) in CAFE_MENU.items()]}]}

    def cart(self, session_id):
        lines = [{"line_id": f"L{i + 1}", "item_id": key, "name": CAFE_MENU[key][0], "quantity": quantity,
                  "unit_price": CAFE_MENU[key][1], "line_total": CAFE_MENU[key][1] * quantity}
                 for i, (key, quantity) in enumerate(self.carts.get(session_id, {}).items())]
        return {"lines": lines, "total": sum(line["line_total"] for line in lines)}

    async def call(self, ctx, name, arguments):
        self.calls.append((ctx, name, dict(arguments)))
        cart = self.cart(ctx.session_id)
        if name == "get_menu":
            listing = ", ".join(f"{n}({key}) {price}원" + (" 품절" if key in SOLD_OUT else "")
                                for key, (n, price) in CAFE_MENU.items())
            return ToolResult(ok=True, text=f"메뉴: {listing}.", data=self.menu(),
                              display=[{"kind": "menu", "data": self.menu(), "source": "get_menu"}])
        if name == "get_cart":
            return ToolResult(ok=True, text=f"주문 내역 {len(cart['lines'])}줄, 합계 {cart['total']}원.", data=cart,
                              display=[{"kind": "cart", "data": cart, "source": "get_cart"}])
        if name == "add_to_cart":
            key = str(arguments.get("item_id") or "")
            if key not in CAFE_MENU:
                return ToolResult(ok=False, error_type="invalid_input", text=f"{key}는 메뉴에 없습니다.")
            if key in SOLD_OUT:
                return ToolResult(ok=False, error_type="precondition_failed", text=f"{CAFE_MENU[key][0]}는 품절입니다.",
                                  display=[{"kind": "notice", "source": "add_to_cart",
                                            "data": {"level": "info", "text": f"{CAFE_MENU[key][0]}는 품절입니다."}}])
            if ctx.speculative:
                return ToolResult(ok=False, error_type="not_confirmed", text="아직 말이 끝나지 않았습니다.")
            quantity = int(arguments.get("quantity") or 1)
            mine = self.carts.setdefault(ctx.session_id, {})
            mine[key] = mine.get(key, 0) + quantity
            cart = self.cart(ctx.session_id)
            return ToolResult(ok=True, executed=True, text=f"{CAFE_MENU[key][0]} {quantity}개를 담았습니다. 합계 {cart['total']}원.",
                              data={"name": CAFE_MENU[key][0], "quantity": quantity},
                              display=[{"kind": "cart", "data": cart, "source": "get_cart"}])
        if name == "request_payment":
            sentence = f"합계 {cart['total']}원 결제를 진행할까요?"
            return ToolResult(ok=True, needs_confirmation=True, confirmation_text=sentence, text="결제 전 확인이 필요합니다.",
                              display=[{"kind": "confirm", "source": "request_payment",
                                        "data": {"text": sentence, "tool": name, **cart}}])
        if name == "handoff_to_human":
            return ToolResult(ok=True, executed=True, text="직원 호출을 접수했습니다.")
        return ToolResult(ok=False, error_type="invalid_input", text="알 수 없는 도구입니다.")


OPENING_WORDS = "어서 오세요. 화면의 메뉴를 보시고 말씀하시거나 눌러서 고르시면 됩니다."


def make_cafe_definition(**overrides):
    values = dict(
        id="test-kiosk", version=1, name="시험 키오스크", persona="당신은 카페 키오스크의 주문 도우미입니다.",
        tools=["get_menu", "get_cart", "add_to_cart", "request_payment", "handoff_to_human"],
        opening={"say": OPENING_WORDS, "tools": [{"name": "get_menu"}, {"name": "get_cart"}]},
        screen={"panels": ["menu", "cart"]},
        actions=[{"name": "add_item", "tool": "add_to_cart", "description": "메뉴를 눌러 담는다",
                  "say": "화면에서 {name} {quantity}개를 담았습니다"},
                 {"name": "pay", "tool": "request_payment", "description": "결제"},
                 {"name": "ghost", "tool": "no_such_tool"}],
    )
    values.update(overrides)
    return make_definition(**values)


async def gather(iterator):
    """All events of open() or act()."""
    return [event async for event in iterator]
