"""
The dummy shop: an order system that behaves like a real one, kept in memory.

- 22 seeded orders (A1001...). Dates are stored as "days ago" and turned into real dates when a
  session first touches the shop, so the refund rules stay meaningful on any day.
- State is per session: every conversation gets its own copy of the orders, its own verification
  and its own refunds, so tests and simulated customers never see each other's changes.
- The business rules live here and not in the gateway: verification is bound to one order, refunds
  are only taken within 7 days of delivery and up to 100000 won.
- `options` (from the tool contract's adapter.options, `ShopSystem.inject()` or AGENTKIT_SHOP_*
  environment variables) add delay and failures (R-TOOL-8).

Every method answers with a dict {"ok", "text", "data", "error_type"}: `text` is a Korean sentence
the model can relay or act on, `data` the structured result.
"""
from __future__ import annotations

import asyncio
import copy
import os
import random
import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .. import config

REFUND_DAYS = 7            # refunds are taken up to this many days after delivery
REFUND_LIMIT = 100000      # above this amount the shop refuses and a person has to handle it
MAX_VERIFY_FAILURES = 3    # wrong phone digits per session before the shop stops answering
RECORD_LIMIT = 2000        # refunds and handoffs remembered for inspection after a session ended

STATUS_TEXT = {"preparing": "상품 준비 중", "in_transit": "배송 중", "delivered": "배송 완료"}
METHODS = ("verify_customer", "get_order", "track_delivery", "request_refund", "handoff_to_human")

# order_id, name, phone last 4, product, amount, ordered days ago, status, delivered days ago,
# carrier, tracking number, address.  The first five are referred to by test personas: do not change.
_SEED = [
    ("A1001", "김민수", "1234", "무선 이어폰", 32000, 6, "delivered", 3, "한진택배", "451200317845", "서울특별시 강남구 테헤란로 152"),
    ("A1002", "이서연", "5678", "로봇청소기", 289000, 5, "delivered", 2, "CJ대한통운", "680031245519", "경기도 성남시 분당구 판교역로 235"),
    ("A1003", "박지훈", "2468", "운동화", 79000, 2, "in_transit", None, "롯데택배", "237745100926", "부산광역시 해운대구 센텀중앙로 79"),
    ("A1004", "최유진", "1357", "텀블러", 18000, 23, "delivered", 20, "우체국택배", "690112340087", "대전광역시 유성구 대학로 99"),
    ("A1005", "정하늘", "9012", "기계식 키보드", 54000, 1, "preparing", None, None, None, "인천광역시 연수구 송도과학로 32"),
    ("A1006", "강도윤", "3141", "블루투스 스피커", 45000, 4, "delivered", 1, "한진택배", "451200551203", "서울특별시 마포구 월드컵북로 396"),
    ("A1007", "윤서아", "2718", "공기청정기", 199000, 8, "delivered", 5, "CJ대한통운", "680031778821", "서울특별시 송파구 올림픽로 300"),
    ("A1008", "임준서", "1618", "전기포트", 27000, 12, "delivered", 9, "롯데택배", "237745393410", "광주광역시 서구 상무중앙로 61"),
    ("A1009", "한지민", "4242", "요가 매트", 10000, 3, "in_transit", None, "우체국택배", "690112771536", "대구광역시 수성구 동대구로 364"),
    ("A1010", "오세훈", "7531", "게이밍 모니터", 300000, 3, "in_transit", None, "CJ대한통운", "680032004467", "경기도 수원시 영통구 광교로 107"),
    ("A1011", "신예은", "8642", "무선 충전기", 15000, 1, "preparing", None, None, None, "울산광역시 남구 삼산로 277"),
    ("A1012", "조민재", "1122", "백팩", 62000, 9, "delivered", 6, "한진택배", "451200824390", "서울특별시 종로구 종로 33"),
    ("A1013", "배수지", "3344", "에어프라이어", 98000, 5, "delivered", 2, "롯데택배", "237745618852", "경기도 고양시 일산동구 중앙로 1286"),
    ("A1014", "문태양", "5566", "캠핑 의자", 38000, 7, "delivered", 4, "우체국택배", "690113025941", "강원특별자치도 춘천시 중앙로 1"),
    ("A1015", "서지우", "7788", "스마트워치", 250000, 2, "preparing", None, None, None, "제주특별자치도 제주시 첨단로 242"),
    ("A1016", "권나연", "9900", "전동 칫솔", 43000, 34, "delivered", 30, "CJ대한통운", "680029113075", "충청북도 청주시 상당구 상당로 82"),
    ("A1017", "황현우", "1470", "노트북 거치대", 22000, 2, "in_transit", None, "한진택배", "451201102764", "경상남도 창원시 성산구 중앙대로 151"),
    ("A1018", "안소희", "2580", "무선 청소기", 159000, 13, "delivered", 10, "롯데택배", "237745207718", "전북특별자치도 전주시 완산구 노송광장로 10"),
    ("A1019", "송민호", "3690", "보조배터리", 29000, 3, "delivered", 0, "CJ대한통운", "680032190358", "서울특별시 성동구 왕십리로 222"),
    ("A1020", "류다은", "4820", "커피 그라인더", 71000, 10, "delivered", 7, "우체국택배", "690112904423", "경기도 용인시 수지구 포은대로 435"),
    ("A1021", "남궁진", "5930", "사무용 의자", 129000, 1, "preparing", None, None, None, "세종특별자치시 한누리대로 2130"),
    ("A1022", "하은별", "6041", "가습기", 36000, 11, "delivered", 8, "한진택배", "451200690012", "충청남도 천안시 서북구 번영로 156"),
]
_HUBS = {"한진택배": "대전 허브", "CJ대한통운": "옥천 허브", "롯데택배": "이천 터미널", "우체국택배": "대전교환센터"}


class ShopError(Exception):
    """A refusal by the shop. error_type is one of contracts.ToolErrorType."""

    def __init__(self, error_type, text, data=None):
        super().__init__(text)
        self.error_type, self.text, self.data = error_type, text, data


class ShopUnavailable(Exception):
    """The shop could not be reached at all (injected outage). The gateway may try again."""


def _won(amount):
    return f"{int(amount)}원"


def _spoken(day):
    if isinstance(day, str):
        day = date.fromisoformat(day)
    return f"{day.month}월 {day.day}일"


def normalize_order_id(value):
    """ "a-1001", "A 1001" -> "A1001" """
    return re.sub(r"[\s\-_.#]", "", str(value or "")).upper()


def seed_orders(today=None):
    """The orders as dicts, with dates worked out relative to `today`."""
    today = today or datetime.now(ZoneInfo(config.TIMEZONE)).date()
    orders = {}
    for oid, name, phone, product, amount, ordered, status, delivered, carrier, tracking, address in _SEED:
        orders[oid] = {
            "order_id": oid, "customer_name": name, "phone_last4": phone,
            "product_name": product, "amount": amount,
            "ordered_at": (today - timedelta(days=ordered)).isoformat(),
            "status": status, "status_text": STATUS_TEXT[status],
            "delivered_at": (today - timedelta(days=delivered)).isoformat() if delivered is not None else None,
            "carrier": carrier, "tracking_number": tracking, "address": address,
            "refund": None,
        }
    return orders


class _Session:
    def __init__(self, today):
        self.today = today
        self.orders = seed_orders(today)
        self.verified_order = None
        self.verify_failures = 0
        self.handoff = None
        self.calls = {}           # method -> number of calls, for "fail the first n times"


class ShopSystem:
    """One shop. `today` pins the calendar (tests); `options` are system-wide injection defaults."""

    name = "shop"
    methods = METHODS

    def __init__(self, today=None, options=None):
        self._today = today
        self._sessions = {}
        self._records = []        # refunds and handoffs, kept after end_session for inspection
        self.options = dict(_env_options())
        self.options.update(options or {})

    # ---- state ------------------------------------------------------------------------------

    def today(self):
        return self._today or datetime.now(ZoneInfo(config.TIMEZONE)).date()

    def _session(self, session_id):
        if session_id not in self._sessions:
            self._sessions[session_id] = _Session(self.today())
        return self._sessions[session_id]

    def end_session(self, session_id):
        """Forgets the session's orders and verification. Its refunds stay in records()."""
        self._sessions.pop(session_id, None)

    def reset(self):
        self._sessions.clear()
        self._records.clear()

    def inject(self, **options):
        """System-wide delay and failures: delay_s, fail, fail_times, fail_rate, fail_methods.
        inject() with no arguments clears them."""
        self.options = dict(options)

    def records(self, session_id=None, kind=None):
        """What was changed: [{"kind": "refund"|"handoff", "session_id", ...}], oldest first."""
        return [copy.deepcopy(r) for r in self._records
                if (session_id is None or r["session_id"] == session_id) and (kind is None or r["kind"] == kind)]

    def refunds(self, session_id=None):
        return self.records(session_id, "refund")

    def order(self, session_id, order_id):
        """The session's view of one order (a copy), for tests and inspection. None if unknown."""
        found = self._session(session_id).orders.get(normalize_order_id(order_id))
        return self._public(found, self._session(session_id)) if found else None

    def verified_order(self, session_id):
        state = self._sessions.get(session_id)
        return state.verified_order if state else None

    # ---- entry points -----------------------------------------------------------------------

    async def call(self, method, arguments, ctx=None, options=None):
        """Runs one method. Returns {"ok", "text", "data", "error_type"} (+"executed" for writes).
        Raises ShopUnavailable for an injected outage."""
        session_id = _session_id(ctx)
        if method not in METHODS:
            return _fail("invalid_input", f"쇼핑몰 시스템에 '{method}' 기능은 없습니다.")
        try:
            await self._inject(method, session_id, options)
            return getattr(self, "_" + method)(self._session(session_id), dict(arguments or {}), session_id)
        except ShopError as e:
            return _fail(e.error_type, e.text, e.data)

    async def preview(self, method, arguments, ctx=None, options=None):
        """What a write would do, without doing it. For request_refund: the product name and the
        amount that go into the confirmation sentence, or the refusal the request would meet.
        Returns None for methods that have nothing to preview."""
        if method != "request_refund":
            return None
        session_id = _session_id(ctx)
        try:
            await self._inject(method, session_id, options, preview=True)
            state = self._session(session_id)
            order = self._authorized(state, (arguments or {}).get("order_id"))
            self._refundable(state, order)
            return {"ok": True, "text": "", "error_type": None,
                    "data": {"order_id": order["order_id"], "product_name": order["product_name"],
                             "amount": order["amount"]}}
        except ShopError as e:
            return _fail(e.error_type, e.text, e.data)

    async def _inject(self, method, session_id, options, preview=False):
        opts = dict(self.options)
        opts.update(options or {})
        only = opts.get("fail_methods")
        delay = float(opts.get("delay_s") or 0)
        if delay > 0:
            await asyncio.sleep(delay)
        fail = opts.get("fail")
        if not fail or (only and method not in only) or (preview and not opts.get("fail_preview")):
            return
        state = self._session(session_id)
        count = state.calls[method] = state.calls.get(method, 0) + 1
        times = opts.get("fail_times")
        if times is not None and count > int(times):
            return
        rate = opts.get("fail_rate")
        if rate is not None and random.random() >= float(rate):
            return
        if fail == "unavailable":
            raise ShopUnavailable("쇼핑몰 시스템에 연결할 수 없습니다 (주입된 장애).")
        if fail == "timeout":
            await asyncio.sleep(float(opts.get("hang_s", 30)))
            return
        if fail == "system_error":
            raise ShopError("system_error", "쇼핑몰 시스템에 장애가 있어 지금은 처리할 수 없습니다. "
                                            "고객에게 잠시 뒤 다시 시도해 달라고 하거나 상담원 연결을 안내하세요.")
        raise ShopError(str(fail), f"쇼핑몰 시스템이 요청을 거절했습니다 ({fail}, 주입된 실패).")

    # ---- rules ------------------------------------------------------------------------------

    def _find(self, state, order_id):
        oid = normalize_order_id(order_id)
        if not oid:
            raise ShopError("invalid_input", "주문번호가 없습니다. 고객에게 주문번호를 물어보세요.")
        order = state.orders.get(oid)
        if order is None:
            raise ShopError("not_found", f"주문번호 {oid}를 찾지 못했습니다. 번호를 다시 확인해 달라고 하세요.",
                            {"order_id": oid})
        return order

    def _authorized(self, state, order_id):
        """The order, if this session verified itself for exactly that order."""
        oid = normalize_order_id(order_id)
        if not oid:
            raise ShopError("invalid_input", "주문번호가 없습니다. 고객에게 주문번호를 물어보세요.")
        if state.verified_order is None:
            raise ShopError("permission_denied",
                            "아직 본인 확인이 되지 않았습니다. 주문번호와 휴대폰 뒤 4자리로 먼저 본인 확인을 하세요.")
        if oid != state.verified_order:
            raise ShopError("permission_denied",
                            f"본인 확인이 된 주문은 {state.verified_order}뿐이라 주문 {oid}의 내용은 알려 줄 수 없습니다. "
                            f"그 주문을 보려면 그 주문번호와 휴대폰 뒤 4자리로 다시 본인 확인을 하세요.",
                            {"verified_order": state.verified_order})
        return state.orders[oid]

    def _refund_state(self, state, order):
        """(refundable, reason code, sentence, refund deadline or None)"""
        if order["refund"]:
            return False, "already_refunded", f"이미 환불이 접수된 주문입니다 (접수번호 {order['refund']['refund_id']}).", None
        if order["status"] != "delivered":
            return (False, "not_delivered",
                    f"아직 {order['status_text']}이라 환불을 접수할 수 없습니다. 환불은 배송 완료 후 {REFUND_DAYS}일 이내에 가능합니다.",
                    None)
        delivered = date.fromisoformat(order["delivered_at"])
        deadline = delivered + timedelta(days=REFUND_DAYS)
        if state.today > deadline:
            return (False, "period_over",
                    f"배송 완료({_spoken(delivered)}) 후 {REFUND_DAYS}일이 지나 환불 기간이 끝났습니다.", deadline)
        if order["amount"] > REFUND_LIMIT:
            return (False, "over_limit",
                    f"결제 금액 {_won(order['amount'])}은 {_won(REFUND_LIMIT)}을 넘어 여기서 환불을 접수할 수 없습니다. "
                    f"상담원이 처리해야 합니다.", deadline)
        return True, None, f"환불할 수 있습니다 ({_spoken(deadline)}까지).", deadline

    def _refundable(self, state, order):
        ok, reason, sentence, _ = self._refund_state(state, order)
        if ok:
            return
        data = {"order_id": order["order_id"], "reason": reason, "handoff_recommended": reason == "over_limit"}
        if reason == "over_limit":
            sentence += " handoff_to_human 도구로 상담원에게 연결하고, 고객에게 상담원이 이어서 도와드린다고 안내하세요."
        elif reason == "already_refunded":
            sentence += " 다시 접수하지 않았습니다."
            data["refund"] = copy.deepcopy(order["refund"])
        else:
            sentence += " 고객에게 이 사유를 설명하세요."
        raise ShopError("precondition_failed", sentence, data)

    def _public(self, order, state):
        ok, reason, sentence, deadline = self._refund_state(state, order)
        view = {k: copy.deepcopy(v) for k, v in order.items() if k != "phone_last4"}
        view.update(refundable=ok, refund_blocked_reason=reason,
                    refund_deadline=deadline.isoformat() if deadline else None)
        return view

    # ---- methods ----------------------------------------------------------------------------

    def _verify_customer(self, state, arguments, session_id):
        digits = re.sub(r"\D", "", str(arguments.get("phone_last4") or ""))
        oid = normalize_order_id(arguments.get("order_id"))
        if len(digits) != 4:
            raise ShopError("invalid_input", "휴대폰 번호 뒤 4자리가 필요합니다. 고객에게 숫자 4자리를 다시 물어보세요.")
        if state.verify_failures >= MAX_VERIFY_FAILURES:
            raise ShopError("permission_denied",
                            "본인 확인에 여러 번 실패해 더 진행할 수 없습니다. handoff_to_human 도구로 상담원에게 연결하세요.",
                            {"locked": True, "handoff_recommended": True})
        order = self._find(state, oid)
        if order["phone_last4"] != digits:
            state.verify_failures += 1
            raise ShopError("permission_denied",
                            "주문번호와 휴대폰 뒤 4자리가 일치하지 않습니다. 본인 확인이 되지 않았으니 주문 내용을 말하지 말고, "
                            "고객에게 번호를 다시 확인해 달라고 하세요.",
                            {"verified": False, "failures": state.verify_failures})
        state.verified_order = oid
        state.verify_failures = 0
        return _ok(f"본인 확인이 끝났습니다. {order['customer_name']} 고객님의 주문 {oid}입니다. "
                   f"이제 이 주문의 내용, 배송, 환불을 도와드릴 수 있습니다.",
                   {"verified": True, "order_id": oid, "customer_name": order["customer_name"]})

    def _get_order(self, state, arguments, session_id):
        order = self._authorized(state, arguments.get("order_id"))
        view = self._public(order, state)
        where = STATUS_TEXT[order["status"]]
        if order["status"] == "delivered":
            where += f"({_spoken(order['delivered_at'])})"
        if order["refund"]:
            refund = f"환불 접수됨 (접수번호 {order['refund']['refund_id']})."
        elif view["refundable"]:
            refund = f"환불 가능 ({_spoken(view['refund_deadline'])}까지)."
        else:
            refund = "환불 불가: " + self._refund_state(state, order)[2]
        text = (f"주문 {order['order_id']}: {order['product_name']}, 결제 금액 {_won(order['amount'])}, "
                f"{_spoken(order['ordered_at'])} 주문, 현재 {where}. {refund} 배송지: {order['address']}.")
        return _ok(text, view)

    def _track_delivery(self, state, arguments, session_id):
        order = self._authorized(state, arguments.get("order_id"))
        ordered = date.fromisoformat(order["ordered_at"])
        events = [{"date": ordered.isoformat(), "where": "판매자", "what": "주문 접수"}]
        data = {"order_id": order["order_id"], "status": order["status"], "status_text": order["status_text"],
                "carrier": order["carrier"], "tracking_number": order["tracking_number"],
                "delivered_at": order["delivered_at"], "address": order["address"], "events": events}
        if order["status"] == "preparing":
            ships = ordered + timedelta(days=2)
            data["expected_ship_date"] = ships.isoformat()
            text = (f"주문 {order['order_id']}({order['product_name']})은 상품 준비 중이고 아직 발송 전입니다. "
                    f"{_spoken(ships)}쯤 발송될 예정입니다.")
            return _ok(text, data)
        shipped = ordered + timedelta(days=1)
        hub = _HUBS.get(order["carrier"], "물류 센터")
        events.append({"date": shipped.isoformat(), "where": "판매자", "what": "상품 발송"})
        events.append({"date": shipped.isoformat(), "where": hub, "what": "간선 상차"})
        if order["status"] == "in_transit":
            arrives = max(state.today + timedelta(days=1), shipped + timedelta(days=2))
            data["expected_delivery_date"] = arrives.isoformat()
            data["current_location"] = hub
            text = (f"주문 {order['order_id']}({order['product_name']})은 배송 중입니다. {order['carrier']} "
                    f"운송장 {order['tracking_number']}, 지금 {hub}에 있고 {_spoken(arrives)}에 도착할 예정입니다.")
            return _ok(text, data)
        events.append({"date": order["delivered_at"], "where": "배송지", "what": "배송 완료"})
        text = (f"주문 {order['order_id']}({order['product_name']})은 {_spoken(order['delivered_at'])}에 배송이 완료되었습니다. "
                f"{order['carrier']} 운송장 {order['tracking_number']}.")
        return _ok(text, data)

    def _request_refund(self, state, arguments, session_id):
        order = self._authorized(state, arguments.get("order_id"))
        if order["refund"]:      # the same request again: answer with the first receipt, change nothing
            refund = order["refund"]
            out = _ok(f"이미 접수된 환불입니다 (접수번호 {refund['refund_id']}, {order['product_name']} "
                      f"{_won(refund['amount'])}). 다시 접수하지 않았습니다.", dict(copy.deepcopy(refund), duplicate=True))
            out["executed"] = False
            return out
        self._refundable(state, order)
        refund = {"refund_id": "R" + order["order_id"][1:], "order_id": order["order_id"],
                  "product_name": order["product_name"], "amount": order["amount"],
                  "reason": str(arguments.get("reason") or "").strip(), "status": "requested",
                  "requested_at": state.today.isoformat()}
        order["refund"] = refund
        self._record("refund", session_id, refund)
        out = _ok(f"{order['product_name']} {_won(order['amount'])} 환불이 접수되었습니다. 접수번호는 {refund['refund_id']}입니다. "
                  f"환불 금액은 영업일 기준 3일 안에 결제하신 수단으로 돌려드립니다.", copy.deepcopy(refund))
        out["executed"] = True
        return out

    def _handoff_to_human(self, state, arguments, session_id):
        if state.handoff:        # one ticket per conversation
            out = _ok(f"이미 상담원 연결을 요청했습니다 (접수번호 {state.handoff['ticket_id']}). "
                      f"고객에게 상담원이 곧 이어서 도와드린다고 안내하세요.", dict(copy.deepcopy(state.handoff), duplicate=True))
            out["executed"] = False
            return out
        ticket = {"ticket_id": f"H{len([r for r in self._records if r['kind'] == 'handoff']) + 1:04d}",
                  "reason": str(arguments.get("reason") or "").strip(), "order_id": state.verified_order,
                  "status": "waiting", "requested_at": time.time()}
        state.handoff = ticket
        self._record("handoff", session_id, ticket)
        out = _ok(f"상담원 연결을 요청했습니다 (접수번호 {ticket['ticket_id']}). "
                  f"고객에게 상담원이 이어서 도와드린다고 안내하고 대화를 마무리하세요.", copy.deepcopy(ticket))
        out["executed"] = True
        return out

    def _record(self, kind, session_id, payload):
        self._records.append(dict(copy.deepcopy(payload), kind=kind, session_id=session_id, at=time.time()))
        del self._records[:-RECORD_LIMIT]


def _ok(text, data=None):
    return {"ok": True, "text": text, "data": data, "error_type": None}


def _fail(error_type, text, data=None):
    return {"ok": False, "text": text, "data": data, "error_type": error_type}


def _session_id(ctx):
    if ctx is None:
        return "default"
    if isinstance(ctx, dict):
        return str(ctx.get("session_id") or "default")
    return str(getattr(ctx, "session_id", None) or "default")


def _env_options():
    """AGENTKIT_SHOP_DELAY_S, AGENTKIT_SHOP_FAIL, AGENTKIT_SHOP_FAIL_TIMES, AGENTKIT_SHOP_FAIL_RATE."""
    out = {}
    if os.environ.get("AGENTKIT_SHOP_DELAY_S"):
        out["delay_s"] = float(os.environ["AGENTKIT_SHOP_DELAY_S"])
    if os.environ.get("AGENTKIT_SHOP_FAIL"):
        out["fail"] = os.environ["AGENTKIT_SHOP_FAIL"]
    if os.environ.get("AGENTKIT_SHOP_FAIL_TIMES"):
        out["fail_times"] = int(os.environ["AGENTKIT_SHOP_FAIL_TIMES"])
    if os.environ.get("AGENTKIT_SHOP_FAIL_RATE"):
        out["fail_rate"] = float(os.environ["AGENTKIT_SHOP_FAIL_RATE"])
    return out


_shop = None


def get_shop():
    """The process-wide shop that `adapter: {kind: dummy, target: "shop:..."}` talks to."""
    global _shop
    if _shop is None:
        _shop = ShopSystem()
    return _shop
