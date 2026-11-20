"""
The dummy airline behind the flight-rebooking agent. Bookings are dated relative to today so the same
personas work on any day.

- verify_booking (booking reference + family name + birth date) opens that one booking to the
  conversation; every other tool refuses a booking that was not verified in this conversation.
- Six departures a day on each route. Change fee 10000원 (0 when the airline cancelled the flight),
  plus a fare difference by time of day. Flights leaving within two hours, group bookings and bookings
  with special assistance are refused with "hand over".
- change_flight has a preview: the sentence read back carries the flight, the departure and the amount
  the airline will actually charge.

Tool bodies for adapters of kind "python": `fn(arguments, ctx)` -> the wire dict; refusals are raised.
"""
from __future__ import annotations

import copy
import re
from datetime import datetime, timedelta

from .cafe import _session_id
from .common import PerSession, birth_key, name_key, now, ok, read_date, refuse, spoken_date, spoken_time, won

CHANGE_FEE = 10000
CUTOFF = timedelta(hours=2)
HANDOFF_OVER = 300000
TIMES = ["07:10", "09:10", "11:40", "14:20", "16:50", "19:30"]
FARE_STEP = {"07:10": 0, "09:10": 0, "11:40": 5000, "14:20": 10000, "16:50": 10000, "19:30": 20000}
ROUTES = {("김포", "제주"): 1200, ("제주", "김포"): 1250, ("부산", "김포"): 1800, ("김포", "부산"): 1850}


def _seed():
    """booking -> days ahead, time; `soon` leaves in an hour."""
    return {
        "B7K2QF": {"family": "홍", "name": "홍민재", "birth": "19880314", "route": ("김포", "제주"), "days": 3, "time": "09:10"},
        "M4T9ZD": {"family": "이", "name": "이정숙", "birth": "19751102", "route": ("부산", "김포"), "days": 5, "time": "14:20"},
        "Q2W8ER": {"family": "박", "name": "박서진", "birth": "19930725", "route": ("제주", "김포"), "days": 2, "time": "16:50",
                   "disrupted": "항공사 사정으로 결항"},
        "L5H1NC": {"family": "정", "name": "정우성", "birth": "19810418", "route": ("김포", "부산"), "soon": True},
        "G3R7TY": {"family": "최", "name": "최다은", "birth": "19900909", "route": ("김포", "제주"), "days": 6, "time": "11:40",
                   "group": True},
        "W9C4KS": {"family": "한", "name": "한복순", "birth": "19480215", "route": ("김포", "제주"), "days": 4, "time": "09:10",
                   "assistance": "휠체어"},
    }


CHANGES = []


def _fresh():
    """One conversation's view of the airline: the seeded bookings and which of them it verified."""
    current, bookings = now(), {}
    for ref, b in _seed().items():
        b = dict(b)
        if b.pop("soon", False):
            b["departure"] = (current + timedelta(hours=1)).replace(second=0, microsecond=0)
            b["flight_no"] = f"KE {ROUTES[b['route']] + 9}"
        else:
            day = current.date() + timedelta(days=b.pop("days"))
            time = b.pop("time")
            b["departure"] = datetime.fromisoformat(f"{day.isoformat()}T{time}")
            b["flight_no"] = _flight_no(b["route"], time)
        bookings[ref] = b
    return {"bookings": bookings, "verified": set()}


STATE = PerSession(_fresh)


def reset():
    STATE.clear()
    CHANGES.clear()


def _flight_no(route, time):
    return f"KE {ROUTES[route] + TIMES.index(time) + 1}"


def _ref(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _when(moment):
    return f"{spoken_date(moment.date())} {spoken_time(moment)}"


def _booking_text(ref, b):
    text = (f"예약 {ref}: {b['name']} 님, {b['route'][0]}에서 {b['route'][1]}, {b['flight_no']}편, {_when(b['departure'])} 출발.")
    if b.get("disrupted"):
        text += f" 이 항공편은 {b['disrupted']}되었습니다. 변경 수수료가 없습니다."
    if b.get("group"):
        text += " 단체 예약입니다(상담원이 처리)."
    if b.get("assistance"):
        text += f" 특별 서비스: {b['assistance']}(상담원이 처리)."
    return text


def verify_booking(arguments, ctx):
    state = STATE.of(ctx)
    ref = _ref(arguments.get("booking_ref"))
    family, birth = name_key(arguments.get("last_name")), birth_key(arguments.get("birth_date"))
    if not ref or not family or not birth:
        refuse("invalid_input", "예약번호, 탑승객의 성, 생년월일이 모두 필요합니다. 빠진 것을 고객에게 물어보세요.")
    b = state["bookings"].get(ref)
    if b is None or not (b["name"].startswith(family) or family == b["name"]) or b["birth"] != birth:
        refuse("permission_denied", "본인 확인에 실패했습니다. 예약 내용은 말하지 말고, 예약번호와 성, 생년월일을 다시 확인해 달라고 하세요.")
    state["verified"].add(ref)
    return ok(f"본인 확인이 되었습니다. {_booking_text(ref, b)}", {"verified": True, "booking_ref": ref, "passenger_name": b["name"]})


def _own(arguments, ctx):
    state = STATE.of(ctx)
    ref = _ref(arguments.get("booking_ref"))
    if ref not in state["verified"]:
        refuse("permission_denied", "이 대화에서 본인 확인이 된 예약이 아닙니다. 먼저 verify_booking으로 확인하세요.")
    return ref, state["bookings"][ref]


def get_booking(arguments, ctx):
    ref, b = _own(arguments, ctx)
    return ok(_booking_text(ref, b), {"booking_ref": ref, "passenger_name": b["name"], "from": b["route"][0], "to": b["route"][1],
                                      "flight_no": b["flight_no"], "departure": b["departure"].isoformat(timespec="minutes"),
                                      "disrupted": bool(b.get("disrupted"))})


def _changeable(ref, b):
    if b.get("group"):
        refuse("precondition_failed", "단체 예약은 상담원이 처리합니다. handoff_to_human으로 넘기세요.")
    if b.get("assistance"):
        refuse("precondition_failed", f"특별 서비스({b['assistance']})가 걸린 예약은 상담원이 처리합니다. handoff_to_human으로 넘기세요.")
    if b["departure"] - now() < CUTOFF:
        refuse("precondition_failed", "출발까지 2시간이 남지 않은 항공편은 여기서 변경할 수 없습니다. handoff_to_human으로 넘기세요.")


def _flights(route, day):
    out = []
    for time in TIMES:
        departure = datetime.fromisoformat(f"{day.isoformat()}T{time}")
        seats = (day.toordinal() + TIMES.index(time) * 3 + ROUTES[route]) % 5      # 0: full
        out.append({"flight_no": _flight_no(route, time), "date": day.isoformat(), "time": time, "departure": departure,
                    "seats": seats * 3})
    return out


def _quote(b, flight):
    fee = 0 if b.get("disrupted") else CHANGE_FEE
    difference = 0 if b.get("disrupted") else max(0, FARE_STEP[flight["time"]] - FARE_STEP.get(b["departure"].strftime("%H:%M"), 0))
    return fee, difference, fee + difference


def search_alternative_flights(arguments, ctx):
    ref, b = _own(arguments, ctx)
    _changeable(ref, b)
    day = read_date(arguments.get("date"))
    if day is None:
        refuse("invalid_input", "원하는 날짜가 필요합니다. 고객에게 물은 뒤 2026-11-20처럼 넘기세요.")
    if day < now().date() or day > now().date() + timedelta(days=60):
        refuse("invalid_input", "오늘부터 60일 안의 날짜만 찾을 수 있습니다.")
    period = str(arguments.get("period") or "")
    flights = [f for f in _flights(b["route"], day) if f["seats"] and f["departure"] - now() > CUTOFF
               and f["departure"] != b["departure"]]
    if "오전" in period:
        flights = [f for f in flights if f["time"] < "12:00"]
    elif "오후" in period:
        flights = [f for f in flights if "12:00" <= f["time"] < "18:00"]
    elif "저녁" in period or "밤" in period:
        flights = [f for f in flights if f["time"] >= "18:00"]
    if not flights:
        return ok(f"{spoken_date(day)}{' ' + period if period else ''}에는 바꿀 수 있는 항공편이 없습니다. 다른 날짜나 시간대를 물어보세요.",
                  {"flights": []}, items=0)
    lines = []
    for f in flights:
        fee, difference, total = _quote(b, f)
        lines.append(f"{f['flight_no']}편 {spoken_time(f['time'])} 출발, 남은 좌석 {f['seats']}석, 수수료와 차액 합계 {won(total)}")
    return ok(f"{spoken_date(day)} {b['route'][0]}에서 {b['route'][1]} 대체편: " + "; ".join(lines) + ". 한 번에 세 개까지만 안내하세요.",
              {"date": day.isoformat(), "flights": [{"flight_no": f["flight_no"], "date": f["date"], "time": f["time"], "seats": f["seats"]}
                                                    for f in flights]}, items=len(flights))


def _target(arguments, ctx):
    ref, b = _own(arguments, ctx)
    _changeable(ref, b)
    day = read_date(arguments.get("date"))
    number = re.sub(r"\D", "", str(arguments.get("flight_no") or ""))
    if day is None or not number:
        refuse("invalid_input", "바꿀 항공편의 편명과 날짜가 필요합니다. search_alternative_flights의 결과에서 고르세요.")
    flight = next((f for f in _flights(b["route"], day) if re.sub(r"\D", "", f["flight_no"]) == number), None)
    if flight is None:
        refuse("not_found", f"{spoken_date(day)}에 {arguments.get('flight_no')}편이 없습니다. search_alternative_flights로 다시 찾으세요.")
    if not flight["seats"]:
        refuse("precondition_failed", f"{flight['flight_no']}편은 좌석이 없습니다. 다른 편을 안내하세요.")
    if flight["departure"] - now() < CUTOFF:
        refuse("precondition_failed", "출발까지 2시간이 남지 않은 편으로는 바꿀 수 없습니다.")
    if flight["departure"] == b["departure"]:
        refuse("invalid_input", "지금 예약된 항공편과 같습니다.")
    fee, difference, total = _quote(b, flight)
    if total > HANDOFF_OVER:
        refuse("precondition_failed", f"수수료와 차액의 합이 {won(HANDOFF_OVER)}을 넘습니다. handoff_to_human으로 넘기세요.")
    return ref, b, flight, fee, difference, total


def quote_change_fee(arguments, ctx):
    ref, b, flight, fee, difference, total = _target(arguments, ctx)
    return ok(f"{flight['flight_no']}편 {_when(flight['departure'])} 출발로 바꾸면 변경 수수료 {won(fee)}, 운임 차액 {won(difference)}, "
              f"합계 {won(total)}입니다." + (" 항공사 사정으로 결항된 항공편이라 수수료가 없습니다." if b.get("disrupted") else ""),
              {"flight_no": flight["flight_no"], "departure": flight["departure"].isoformat(timespec="minutes"),
               "fee": fee, "fare_difference": difference, "total": total})


def preview_change_flight(arguments, ctx):
    ref, b, flight, fee, difference, total = _target(arguments, ctx)
    return ok("", {"flight": f"{flight['flight_no']}편", "departure": _when(flight["departure"]), "total": won(total)})


def change_flight(arguments, ctx):
    ref, b, flight, fee, difference, total = _target(arguments, ctx)
    before = copy.deepcopy(b)
    b.update(flight_no=flight["flight_no"], departure=flight["departure"])
    b.pop("disrupted", None)
    CHANGES.append({"booking_ref": ref, "from": before["flight_no"], "to": flight["flight_no"],
                    "departure": flight["departure"].isoformat(timespec="minutes"), "charged": total, "session_id": _session_id(ctx)})
    return ok(f"변경되었습니다. 새 일정: {b['route'][0]}에서 {b['route'][1]}, {flight['flight_no']}편, {_when(flight['departure'])} 출발. "
              f"결제된 금액 {won(total)}. 새 일정을 한 번 더 읽어 주세요.",
              {"booking_ref": ref, "flight_no": flight["flight_no"], "departure": flight["departure"].isoformat(timespec="minutes"),
               "charged": total}, executed=True)

