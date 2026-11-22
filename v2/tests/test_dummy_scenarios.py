"""The dummy clinic, burger shop and airline, and their write tools through the gateway (two phases)."""
import asyncio
from datetime import timedelta

import pytest

from agentkit.contracts import ToolContext
from agentkit.dummy import airline, burger, clinic
from agentkit.dummy.common import birth_key, now, read_date, read_time
from agentkit.tools.adapters import ToolFailure
from agentkit.tools.gateway import build_gateway


@pytest.fixture(autouse=True)
def fresh():
    clinic.reset(), burger.reset(), airline.reset()
    yield
    clinic.reset(), burger.reset(), airline.reset()


def refused(fn, arguments, ctx):
    with pytest.raises(ToolFailure) as e:
        fn(arguments, ctx)
    return e.value.error_type


def test_readers():
    assert [read_time(t) for t in ("14:30", "오후 2시 30분", "3시 반", "오전 10시", "9시")] == ["14:30", "14:30", "15:30", "10:00", "09:00"]
    assert read_time("언제든") is None
    today = now().date()
    assert read_date("내일") == today + timedelta(days=1) and read_date("모레") == today + timedelta(days=2)
    assert read_date("2026-12-03").isoformat() == "2026-12-03" and read_date("아무 때나") is None
    assert {birth_key(b) for b in ("1991-06-20", "1991년 6월 20일", "910620", "19910620")} == {"19910620"}
    assert birth_key("050102") == "20050102" and birth_key("모름") == ""


# ---- clinic ---------------------------------------------------------------------------------------

KIM = {"name": "김하늘", "birth_date": "1991-06-20"}


def test_clinic_books_and_cancels_per_conversation():
    a, b = {"session_id": "a"}, {"session_id": "b"}
    assert clinic.find_patient(KIM, a)["data"]["visit_type"] == "재진"
    assert clinic.find_patient({"name": "이새봄", "birth_date": "2001-02-03"}, a)["data"] == {"found": False, "visit_type": "초진", "name": "이새봄"}
    day = next(d for d in (now().date() + timedelta(days=n) for n in range(1, 8)) if clinic.free_slots(d))
    slots = clinic.list_available_slots({"date": day.isoformat()}, a)["data"]["slots"]
    request = dict(KIM, date=day.isoformat(), time=slots[0], reason="감기")
    preview = clinic.preview_book_appointment(request, a)["data"]
    assert preview["name"] == "김하늘" and str(day.day) + "일" in preview["when"]
    done = clinic.book_appointment(request, a)
    assert done["executed"] is True and done["data"]["visit_type"] == "재진"
    assert slots[0] not in clinic.list_available_slots({"date": day.isoformat()}, a)["data"]["slots"]
    assert refused(clinic.book_appointment, request, a) == "precondition_failed"          # taken, and already booked
    assert slots[0] in clinic.list_available_slots({"date": day.isoformat()}, b)["data"]["slots"]   # another conversation
    assert refused(clinic.cancel_appointment, KIM, b) == "not_found"
    assert clinic.cancel_appointment(KIM, a)["data"]["cancelled"] is True


def test_clinic_refuses_what_it_cannot_book():
    ctx = {"session_id": "c"}
    sunday = next(d for d in (now().date() + timedelta(days=n) for n in range(1, 8)) if d.weekday() == 6)
    assert clinic.list_available_slots({"date": sunday.isoformat()}, ctx)["data"]["closed"] is True
    weekday = next(d for d in (now().date() + timedelta(days=n) for n in range(1, 8)) if d.weekday() < 5)
    assert refused(clinic.book_appointment, dict(KIM, date=weekday.isoformat(), time="13:00"), ctx) == "precondition_failed"   # lunch
    assert refused(clinic.book_appointment, dict(KIM, date="", time="10:00"), ctx) == "invalid_input"
    assert refused(clinic.find_patient, {"name": "김하늘"}, ctx) == "invalid_input"
    assert refused(clinic.list_available_slots, {"date": (now().date() - timedelta(days=1)).isoformat()}, ctx) == "invalid_input"


# ---- burger shop ----------------------------------------------------------------------------------

def test_burger_order_sheet():
    ctx = {"session_id": "car-1"}
    assert refused(burger.add_to_cart, {"item": "불고기버거", "set": True}, ctx) == "invalid_input"        # the drink is missing
    burger.add_to_cart({"item": "불고기버거 세트", "drink": "콜라"}, ctx)
    burger.add_to_cart({"item": "치즈버거", "quantity": 2}, ctx)
    burger.remove_from_cart({"item": "치즈버거", "quantity": 1}, ctx)
    burger.add_to_cart({"item": "치킨버거"}, ctx)
    burger.add_to_cart({"item": "감자튀김", "size": "large"}, ctx)
    cart = burger.get_cart({}, ctx)["data"]
    assert [(l["name"], l["quantity"], l["line_total"]) for l in cart["lines"]] == [
        ("불고기버거", 1, 7900), ("치즈버거", 1, 4900), ("치킨버거", 1, 5900), ("감자튀김", 1, 3200)]
    assert cart["total"] == 21900
    assert "합계 21900원" in burger.preview_submit_order({}, ctx)["data"]["summary"]
    order = burger.submit_order({}, ctx)
    assert order["executed"] is True and order["data"]["total"] == 21900 and burger.ORDERS[-1]["order_no"] == order["data"]["order_no"]
    assert refused(burger.submit_order, {}, ctx) == "precondition_failed"                                 # the sheet is empty again
    assert burger.get_cart({}, {"session_id": "car-2"})["data"]["lines"] == []


def test_burger_menu_facts():
    ctx = {"session_id": "car-3"}
    assert refused(burger.add_to_cart, {"item": "치킨너겟"}, ctx) == "precondition_failed"                 # sold out
    assert refused(burger.add_to_cart, {"item": "피자"}, ctx) == "not_found"
    assert refused(burger.add_to_cart, {"item": "감자튀김", "set": True, "drink": "콜라"}, ctx) == "invalid_input"
    truffle = burger.search_menu({"query": "트러플"}, ctx)
    assert truffle["data"]["items"][0]["allergens"] is None and "확인 불가" in truffle["text"]
    assert "새우" in burger.search_menu({"query": "새우버거"}, ctx)["text"]
    assert burger.search_menu({"query": "피자"}, ctx)["data"]["items"] == []
    assert len(burger.search_menu({"query": "음료"}, ctx)["data"]["items"]) == 4
    burger.add_to_cart({"item": "불고기버거", "quantity": 30}, ctx)
    burger.add_to_cart({"item": "트러플머쉬룸버거", "quantity": 10}, ctx)
    assert refused(burger.submit_order, {}, ctx) == "precondition_failed"                                 # over 200000원: staff


# ---- airline --------------------------------------------------------------------------------------

HONG = {"booking_ref": "B7K2QF", "last_name": "홍", "birth_date": "1988-03-14"}


def test_airline_needs_verification_per_conversation():
    a, b = {"session_id": "a"}, {"session_id": "b"}
    assert refused(airline.get_booking, {"booking_ref": "B7K2QF"}, a) == "permission_denied"
    assert refused(airline.verify_booking, dict(HONG, last_name="김"), a) == "permission_denied"
    assert refused(airline.verify_booking, {"booking_ref": "B7K2QF"}, a) == "invalid_input"
    assert airline.verify_booking(dict(HONG, booking_ref="b7k 2qf"), a)["data"]["passenger_name"] == "홍민재"
    assert airline.get_booking({"booking_ref": "B7K2QF"}, a)["data"]["flight_no"] == "KE 1202"
    assert refused(airline.get_booking, {"booking_ref": "B7K2QF"}, b) == "permission_denied"
    assert refused(airline.get_booking, {"booking_ref": "M4T9ZD"}, a) == "permission_denied"             # not this booking


def test_airline_change_and_fee():
    ctx = {"session_id": "a"}
    airline.verify_booking(HONG, ctx)
    booked = airline.STATE.of(ctx)["bookings"]["B7K2QF"]["departure"]
    day = (booked + timedelta(days=1)).date().isoformat()
    found = airline.search_alternative_flights({"booking_ref": "B7K2QF", "date": day, "period": "오후"}, ctx)["data"]["flights"]
    assert found and all("12:00" <= f["time"] < "18:00" and f["seats"] > 0 for f in found)
    target = {"booking_ref": "B7K2QF", "flight_no": found[0]["flight_no"], "date": day}
    quote = airline.quote_change_fee(target, ctx)["data"]
    assert quote["fee"] == 10000 and quote["total"] == quote["fee"] + quote["fare_difference"] == 20000
    assert airline.preview_change_flight(target, ctx)["data"]["total"] == "20000원"
    done = airline.change_flight(target, ctx)
    assert done["executed"] is True and done["data"]["charged"] == 20000
    assert airline.get_booking({"booking_ref": "B7K2QF"}, ctx)["data"]["flight_no"] == found[0]["flight_no"]
    assert refused(airline.change_flight, target, ctx) == "invalid_input"                                # the same flight now
    assert airline.STATE.of({"session_id": "other"})["bookings"]["B7K2QF"]["flight_no"] == "KE 1202"     # others see the seed


@pytest.mark.parametrize("ref,family,birth,outcome", [
    ("L5H1NC", "정", "1981-04-18", "precondition_failed"),       # leaves within two hours
    ("G3R7TY", "최", "1990-09-09", "precondition_failed"),       # group booking
    ("W9C4KS", "한", "1948-02-15", "precondition_failed"),       # special assistance
])
def test_airline_hands_over(ref, family, birth, outcome):
    ctx = {"session_id": "a"}
    assert ref in airline.verify_booking({"booking_ref": ref, "last_name": family, "birth_date": birth}, ctx)["text"]
    day = (now().date() + timedelta(days=3)).isoformat()
    assert refused(airline.search_alternative_flights, {"booking_ref": ref, "date": day}, ctx) == outcome


def test_airline_cancelled_flight_costs_nothing():
    ctx = {"session_id": "a"}
    assert "수수료가 없습니다" in airline.verify_booking({"booking_ref": "Q2W8ER", "last_name": "박", "birth_date": "930725"}, ctx)["text"]
    day = (now().date() + timedelta(days=4)).isoformat()
    flight = airline.search_alternative_flights({"booking_ref": "Q2W8ER", "date": day}, ctx)["data"]["flights"][-1]
    assert airline.quote_change_fee({"booking_ref": "Q2W8ER", "flight_no": flight["flight_no"], "date": day}, ctx)["data"]["total"] == 0


# ---- through the gateway: nothing is written before the customer agrees ---------------------------

def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_write_tools_ask_first_through_the_gateway():
    async def main():
        gateway = build_gateway()

        def ctx(session, turn, said):
            return ToolContext(session_id=session, agent_id="t", version=1, turn=turn, last_user_text=said, test=False)

        # the airline: unverified -> refused by the gateway itself; verified -> proposal -> consent -> change
        # a day on which KE 1204 has seats: the fake airline's seats depend on the date (fixed day+4 broke on 2026-11-22)
        from agentkit.dummy import airline
        day = next(d for d in (now().date() + timedelta(days=n) for n in range(2, 30))
                   if any(f["flight_no"] == "KE 1204" and f["seats"] for f in airline._flights(("김포", "제주"), d))).isoformat()
        target = {"booking_ref": "B7K2QF", "flight_no": "KE 1204", "date": day}
        early = await gateway.call(ctx("f1", 1, "바꿔 주세요"), "change_flight", target)
        assert not early.ok and early.error_type == "precondition_failed"
        assert (await gateway.call(ctx("f1", 1, "홍입니다"), "verify_booking", HONG)).ok
        proposal = await gateway.call(ctx("f1", 2, "2시 20분 편으로요"), "change_flight", target)
        assert proposal.ok and proposal.needs_confirmation and not proposal.executed
        assert "KE 1204편" in proposal.confirmation_text and "20000원" in proposal.confirmation_text
        assert airline.CHANGES == []
        done = await gateway.call(ctx("f1", 3, "네, 변경해 주세요"), "change_flight", target)
        assert done.ok and done.executed and len(airline.CHANGES) == 1

        # the clinic: a "no" leaves the book empty
        day = next(d for d in (now().date() + timedelta(days=n) for n in range(1, 8)) if clinic.free_slots(d))
        request = dict(KIM, date=day.isoformat(), time=clinic.free_slots(day)[0])
        proposal = await gateway.call(ctx("c1", 1, "그 시간으로요"), "book_appointment", request)
        assert proposal.needs_confirmation and "김하늘 님" in proposal.confirmation_text and "내과" in proposal.confirmation_text
        refused_call = await gateway.call(ctx("c1", 2, "아니요, 다른 시간으로 할게요"), "book_appointment", request)
        assert not refused_call.executed and clinic.BOOKS.of({"session_id": "c1"}) == []

        # the burger shop: adding needs no consent, submitting does
        added = await gateway.call(ctx("d1", 1, "치즈버거 하나요"), "burger.add_to_cart", {"item": "치즈버거"})
        assert added.ok and added.executed
        proposal = await gateway.call(ctx("d1", 2, "그게 다예요"), "submit_order", {})
        assert proposal.needs_confirmation and "치즈버거 1개, 합계 4900원" in proposal.confirmation_text and burger.ORDERS == []
        done = await gateway.call(ctx("d1", 3, "네 맞아요"), "submit_order", {})
        assert done.executed and len(burger.ORDERS) == 1

    run(main())
