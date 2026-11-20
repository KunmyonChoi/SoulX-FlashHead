"""The dummy shop on its own: seed data, rules, per-session state, injected delay and failures."""
import asyncio
import time
from datetime import date, timedelta

import pytest

from agentkit.dummy.shop import REFUND_LIMIT, ShopSystem, ShopUnavailable, seed_orders

TODAY = date(2026, 11, 17)


@pytest.fixture
def shop():
    return ShopSystem(today=TODAY)


def ctx(session_id="s1"):
    return {"session_id": session_id}


async def verified(shop, order_id, last4, session_id="s1"):
    result = await shop.call("verify_customer", {"order_id": order_id, "phone_last4": last4}, ctx(session_id))
    assert result["ok"], result["text"]
    return result


# ---- seed --------------------------------------------------------------------------------------


def test_seed_has_the_five_fixed_orders():
    orders = seed_orders(TODAY)
    fixed = {
        "A1001": ("김민수", "1234", "무선 이어폰", 32000, "delivered", 3),
        "A1002": ("이서연", "5678", "로봇청소기", 289000, "delivered", 2),
        "A1003": ("박지훈", "2468", "운동화", 79000, "in_transit", None),
        "A1004": ("최유진", "1357", "텀블러", 18000, "delivered", 20),
        "A1005": ("정하늘", "9012", "기계식 키보드", 54000, "preparing", None),
    }
    for oid, (name, last4, product, amount, status, delivered_ago) in fixed.items():
        order = orders[oid]
        assert (order["customer_name"], order["phone_last4"], order["product_name"], order["amount"],
                order["status"]) == (name, last4, product, amount, status), oid
        expected = (TODAY - timedelta(days=delivered_ago)).isoformat() if delivered_ago is not None else None
        assert order["delivered_at"] == expected, oid


def test_seed_is_varied():
    orders = seed_orders(TODAY)
    assert len(orders) >= 20
    assert {o["status"] for o in orders.values()} == {"preparing", "in_transit", "delivered"}
    amounts = [o["amount"] for o in orders.values()]
    assert min(amounts) == 10000 and max(amounts) == 300000
    assert sum(a > REFUND_LIMIT for a in amounts) >= 4 and sum(a <= 50000 for a in amounts) >= 6
    assert len({o["phone_last4"] for o in orders.values()}) == len(orders)
    for order in orders.values():
        assert order["address"] and len(order["phone_last4"]) == 4
        assert date.fromisoformat(order["ordered_at"]) <= TODAY
        if order["status"] == "preparing":
            assert order["carrier"] is None and order["tracking_number"] is None
        else:
            assert order["carrier"] and len(order["tracking_number"]) == 12


def test_dates_follow_today():
    assert seed_orders(date(2027, 3, 1))["A1001"]["delivered_at"] == "2027-02-26"
    assert ShopSystem().order("x", "A1001")["refundable"] is True     # whatever day the tests run on


# ---- verification ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_customer(shop):
    wrong = await shop.call("verify_customer", {"order_id": "A1001", "phone_last4": "0000"}, ctx())
    assert (wrong["ok"], wrong["error_type"]) == (False, "permission_denied")
    assert "무선 이어폰" not in wrong["text"] and "김민수" not in wrong["text"]
    missing = await shop.call("verify_customer", {"order_id": "A9999", "phone_last4": "1234"}, ctx())
    assert missing["error_type"] == "not_found"
    short = await shop.call("verify_customer", {"order_id": "A1001", "phone_last4": "12"}, ctx())
    assert short["error_type"] == "invalid_input"
    assert shop.verified_order("s1") is None
    ok = await shop.call("verify_customer", {"order_id": "a-1001", "phone_last4": "1234"}, ctx())
    assert ok["ok"] and ok["data"] == {"verified": True, "order_id": "A1001", "customer_name": "김민수"}
    assert shop.verified_order("s1") == "A1001"


@pytest.mark.asyncio
async def test_verification_is_locked_after_three_wrong_tries(shop):
    for _ in range(3):
        await shop.call("verify_customer", {"order_id": "A1001", "phone_last4": "0000"}, ctx())
    locked = await shop.call("verify_customer", {"order_id": "A1001", "phone_last4": "1234"}, ctx())
    assert locked["error_type"] == "permission_denied" and locked["data"]["locked"]
    assert "handoff_to_human" in locked["text"]
    assert (await shop.call("verify_customer", {"order_id": "A1001", "phone_last4": "1234"}, ctx("other")))["ok"]


@pytest.mark.asyncio
async def test_lookups_need_verification_for_exactly_that_order(shop):
    for method in ("get_order", "track_delivery"):
        denied = await shop.call(method, {"order_id": "A1001"}, ctx())
        assert denied["error_type"] == "permission_denied", method
    assert (await shop.preview("request_refund", {"order_id": "A1001"}, ctx()))["error_type"] == "permission_denied"
    await verified(shop, "A1001", "1234")
    assert (await shop.call("get_order", {"order_id": "A1001"}, ctx()))["ok"]
    for method in ("get_order", "track_delivery", "request_refund"):
        other = await shop.call(method, {"order_id": "A1002", "reason": "x"}, ctx())
        assert other["error_type"] == "permission_denied", method
        assert "로봇청소기" not in other["text"] and "이서연" not in other["text"]
    # verifying the other order moves the permission to it
    await verified(shop, "A1002", "5678")
    assert (await shop.call("get_order", {"order_id": "A1002"}, ctx()))["ok"]
    assert (await shop.call("get_order", {"order_id": "A1001"}, ctx()))["error_type"] == "permission_denied"


# ---- lookups -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_order(shop):
    await verified(shop, "A1001", "1234")
    result = await shop.call("get_order", {"order_id": "A1001"}, ctx())
    data = result["data"]
    assert (data["product_name"], data["amount"], data["status"], data["refundable"]) == ("무선 이어폰", 32000, "delivered", True)
    assert data["delivered_at"] == "2026-11-14" and data["refund_deadline"] == "2026-11-21"
    assert "phone_last4" not in data
    assert "무선 이어폰" in result["text"] and "32000원" in result["text"] and "환불 가능" in result["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("order_id,last4,words,key", [
    ("A1005", "9012", ["준비 중", "발송"], "expected_ship_date"),
    ("A1003", "2468", ["배송 중", "롯데택배", "237745100926"], "expected_delivery_date"),
    ("A1001", "1234", ["배송이 완료", "11월 14일"], "delivered_at"),
])
async def test_track_delivery_by_state(shop, order_id, last4, words, key):
    await verified(shop, order_id, last4)
    result = await shop.call("track_delivery", {"order_id": order_id}, ctx())
    assert result["ok"] and all(word in result["text"] for word in words), result["text"]
    assert result["data"][key] and result["data"]["events"]


# ---- refunds -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refund_preview_gives_product_and_amount_and_changes_nothing(shop):
    await verified(shop, "A1001", "1234")
    preview = await shop.preview("request_refund", {"order_id": "A1001", "reason": "변심"}, ctx())
    assert preview["ok"] and preview["data"] == {"order_id": "A1001", "product_name": "무선 이어폰", "amount": 32000}
    assert shop.refunds() == [] and shop.order("s1", "A1001")["refund"] is None


@pytest.mark.asyncio
async def test_refund_is_taken_once(shop):
    await verified(shop, "A1001", "1234")
    first = await shop.call("request_refund", {"order_id": "A1001", "reason": "단순 변심"}, ctx())
    assert first["ok"] and first["executed"] is True
    assert first["data"]["refund_id"] == "R1001" and first["data"]["amount"] == 32000
    second = await shop.call("request_refund", {"order_id": "A1001", "reason": "또"}, ctx())
    assert second["ok"] and second["executed"] is False and second["data"]["refund_id"] == "R1001"
    assert len(shop.refunds("s1")) == 1 and shop.refunds("s1")[0]["reason"] == "단순 변심"
    order = await shop.call("get_order", {"order_id": "A1001"}, ctx())
    assert order["data"]["refundable"] is False and order["data"]["refund_blocked_reason"] == "already_refunded"
    again = await shop.preview("request_refund", {"order_id": "A1001"}, ctx())
    assert again["error_type"] == "precondition_failed" and again["data"]["reason"] == "already_refunded"


@pytest.mark.asyncio
@pytest.mark.parametrize("order_id,last4,reason,handoff", [
    ("A1002", "5678", "over_limit", True),       # 289000 won, delivered 2 days ago
    ("A1003", "2468", "not_delivered", False),   # in transit
    ("A1005", "9012", "not_delivered", False),   # preparing
    ("A1004", "1357", "period_over", False),     # delivered 20 days ago
])
async def test_refund_rules(shop, order_id, last4, reason, handoff):
    await verified(shop, order_id, last4)
    for result in (await shop.preview("request_refund", {"order_id": order_id, "reason": "변심"}, ctx()),
                   await shop.call("request_refund", {"order_id": order_id, "reason": "변심"}, ctx())):
        assert (result["ok"], result["error_type"]) == (False, "precondition_failed")
        assert result["data"]["reason"] == reason and result["data"]["handoff_recommended"] is handoff
        assert ("handoff_to_human" in result["text"]) is handoff
    assert shop.refunds() == []


@pytest.mark.asyncio
async def test_refund_period_boundary():
    """A1020 was delivered 7 days ago: still refundable today, not tomorrow."""
    shop = ShopSystem(today=TODAY)
    await verified(shop, "A1020", "4820")
    assert (await shop.preview("request_refund", {"order_id": "A1020"}, ctx()))["ok"]
    tomorrow = ShopSystem(today=TODAY + timedelta(days=1))
    await verified(tomorrow, "A1014", "5566")       # delivered 4 days before TODAY, 5 before tomorrow
    assert (await tomorrow.preview("request_refund", {"order_id": "A1014"}, ctx()))["ok"]
    late = ShopSystem(today=TODAY)
    await verified(late, "A1022", "6041")           # delivered 8 days ago
    assert (await late.preview("request_refund", {"order_id": "A1022"}, ctx()))["data"]["reason"] == "period_over"


# ---- handoff -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handoff_needs_no_verification_and_makes_one_ticket(shop):
    first = await shop.call("handoff_to_human", {"reason": "고객 요청"}, ctx())
    assert first["ok"] and first["executed"] is True and first["data"]["ticket_id"]
    second = await shop.call("handoff_to_human", {"reason": "다시"}, ctx())
    assert second["executed"] is False and second["data"]["ticket_id"] == first["data"]["ticket_id"]
    assert len(shop.records("s1", "handoff")) == 1


# ---- sessions ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sessions_do_not_leak_into_each_other(shop):
    await verified(shop, "A1001", "1234", "a")
    await shop.call("request_refund", {"order_id": "A1001", "reason": "변심"}, ctx("a"))
    # another session: not verified, and its copy of the order has no refund
    assert (await shop.call("get_order", {"order_id": "A1001"}, ctx("b")))["error_type"] == "permission_denied"
    await verified(shop, "A1001", "1234", "b")
    assert (await shop.call("get_order", {"order_id": "A1001"}, ctx("b")))["data"]["refund"] is None
    assert (await shop.call("request_refund", {"order_id": "A1001", "reason": "변심"}, ctx("b")))["executed"] is True
    assert [r["session_id"] for r in shop.refunds()] == ["a", "b"]
    # ending a session forgets its state but keeps the record of what it changed
    shop.end_session("a")
    assert shop.verified_order("a") is None and shop.order("a", "A1001")["refund"] is None
    assert len(shop.refunds("a")) == 1
    shop.reset()
    assert shop.refunds() == []


# ---- injected delay and failures (R-TOOL-8) -----------------------------------------------------


@pytest.mark.asyncio
async def test_injected_delay(shop):
    started = time.perf_counter()
    await shop.call("verify_customer", {"order_id": "A1001", "phone_last4": "1234"}, ctx(), {"delay_s": 0.15})
    assert time.perf_counter() - started >= 0.15


@pytest.mark.asyncio
async def test_injected_failures(shop):
    arguments = {"order_id": "A1001", "phone_last4": "1234"}
    with pytest.raises(ShopUnavailable):
        await shop.call("verify_customer", arguments, ctx(), {"fail": "unavailable"})
    error = await shop.call("verify_customer", arguments, ctx(), {"fail": "system_error"})
    assert (error["ok"], error["error_type"]) == (False, "system_error")
    typed = await shop.call("verify_customer", arguments, ctx(), {"fail": "not_found"})
    assert typed["error_type"] == "not_found"
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(shop.call("verify_customer", arguments, ctx(), {"fail": "timeout"}), 0.1)
    assert shop.verified_order("s1") is None


@pytest.mark.asyncio
async def test_fail_times_and_fail_methods(shop):
    arguments = {"order_id": "A1001", "phone_last4": "1234"}
    shop.inject(fail="system_error", fail_times=2, fail_methods=["verify_customer"])
    results = [await shop.call("verify_customer", arguments, ctx("n")) for _ in range(3)]
    assert [r["ok"] for r in results] == [False, False, True]
    assert (await shop.call("get_order", {"order_id": "A1001"}, ctx("n")))["ok"]
    shop.inject()
    assert (await shop.call("verify_customer", arguments, ctx("m")))["ok"]


def test_environment_options(monkeypatch):
    monkeypatch.setenv("AGENTKIT_SHOP_DELAY_S", "0.25")
    monkeypatch.setenv("AGENTKIT_SHOP_FAIL", "system_error")
    assert ShopSystem().options == {"delay_s": 0.25, "fail": "system_error"}


@pytest.mark.asyncio
async def test_unknown_method(shop):
    assert (await shop.call("delete_order", {}, ctx()))["error_type"] == "invalid_input"
