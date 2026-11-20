"""
The dummy clinic: an internal-medicine practice's appointment book, kept in memory.

- Three registered patients (others are first visits, which is not an error).
- Slots every 30 minutes, weekdays 09:00-17:30 without 12:30-14:00, Saturdays 09:00-12:30, closed on
  Sundays. Some slots are taken (fixed by date, so tests can rely on them).
- book_appointment and cancel_appointment have previews: the sentence read to the patient names the
  day and time the system will actually use.

Tool bodies for adapters of kind "python": `fn(arguments, ctx)` -> the wire dict; refusals are raised.
"""
from __future__ import annotations

from datetime import date, timedelta

from .common import PerSession, birth_key, name_key, now, ok, read_date, read_time, refuse, spoken_date, spoken_time

PATIENTS = [
    {"id": "P1001", "name": "김하늘", "birth": "19910620", "last_visit": "2026-08-12"},
    {"id": "P1002", "name": "박준", "birth": "19680109", "last_visit": "2026-10-30"},
    {"id": "P1003", "name": "최영수", "birth": "19590930", "last_visit": "2026-05-03"},
]
DEPARTMENT = "내과"
BOOKS = PerSession(list)   # per conversation: [{"id", "name", "birth", "date", "time", "reason", "visit_type"}]
_counter = [0]


def reset():
    BOOKS.clear()
    _counter[0] = 0


def _patient(name, birth):
    return next((p for p in PATIENTS if p["name"] == name_key(name) and p["birth"] == birth_key(birth)), None)


def _day_slots(day):
    if day.weekday() == 6:
        return []
    times = [f"{h:02d}:{m:02d}" for h in range(9, 18) for m in (0, 30)]
    times = [t for t in times if not ("12:30" <= t < "14:00")]
    if day.weekday() == 5:
        times = [t for t in times if t < "12:30"]
    return times


def _taken(day, time, book=()):
    booked = any(a["date"] == day.isoformat() and a["time"] == time for a in book)
    hour, minute = int(time[:2]), int(time[3:])
    return booked or (day.toordinal() * 7 + hour * 2 + minute // 30) % 3 == 0     # the practice's other patients


def free_slots(day, book=()):
    current = now()
    return [t for t in _day_slots(day) if not _taken(day, t, book)
            and (day > current.date() or (day == current.date() and t > current.strftime("%H:%M")))]


def _need_patient(arguments):
    name, birth = name_key(arguments.get("name")), birth_key(arguments.get("birth_date"))
    if not name or not birth:
        refuse("invalid_input", "이름과 생년월일이 모두 필요합니다. 환자에게 이름과 생년월일을 다시 물어보세요.")
    return name, birth


def find_patient(arguments, ctx):
    name, birth = _need_patient(arguments)
    patient = _patient(name, birth)
    upcoming = [a for a in BOOKS.of(ctx) if a["name"] == name and a["birth"] == birth and a["date"] >= now().date().isoformat()]
    booked = ""
    if upcoming:
        a = upcoming[0]
        booked = f" 잡혀 있는 예약: {spoken_date(date.fromisoformat(a['date']))} {spoken_time(a['time'])}."
    if patient is None:
        return ok(f"{name} 님은 등록된 기록이 없습니다. 처음 오시는 분으로 접수하면 됩니다(초진)." + booked,
                  {"found": False, "visit_type": "초진", "name": name})
    return ok(f"{name} 님은 재진 환자입니다. 마지막 방문은 {patient['last_visit']}입니다." + booked,
              {"found": True, "visit_type": "재진", "patient_id": patient["id"], "name": name, "last_visit": patient["last_visit"]})


def list_available_slots(arguments, ctx):
    day = read_date(arguments.get("date"), now().date() + timedelta(days=1))
    if day is None:
        refuse("invalid_input", "날짜를 알아듣지 못했습니다. 2026-11-19처럼 다시 넘기세요.")
    if day < now().date():
        refuse("invalid_input", "지난 날짜입니다. 오늘 이후 날짜를 물어보세요.")
    if day > now().date() + timedelta(days=30):
        refuse("invalid_input", "예약은 30일 뒤까지만 받습니다.")
    period = str(arguments.get("period") or "")
    slots = free_slots(day, BOOKS.of(ctx))
    if "오전" in period or period.lower() == "am":
        slots = [t for t in slots if t < "12:30"]
    elif "오후" in period or period.lower() == "pm":
        slots = [t for t in slots if t >= "14:00"]
    if not _day_slots(day):
        return ok(f"{spoken_date(day)}은 휴진입니다. 다른 날짜를 물어보세요.", {"date": day.isoformat(), "slots": [], "closed": True})
    if not slots:
        return ok(f"{spoken_date(day)}{' ' + period if period else ''}에는 빈 시간이 없습니다. 다른 날이나 시간대를 물어보세요.",
                  {"date": day.isoformat(), "slots": []})
    return ok(f"{spoken_date(day)} {DEPARTMENT} 빈 시간: " + ", ".join(f"{spoken_time(t)}({t})" for t in slots[:6])
              + (f" 외 {len(slots) - 6}개" if len(slots) > 6 else "") + ". 한 번에 세 개까지만 안내하세요.",
              {"date": day.isoformat(), "slots": slots})


def _booking_request(arguments, ctx):
    book = BOOKS.of(ctx)
    name, birth = _need_patient(arguments)
    day, time = read_date(arguments.get("date")), read_time(arguments.get("time"))
    if day is None or time is None:
        refuse("invalid_input", "예약 날짜와 시각이 필요합니다. 날짜는 2026-11-19, 시각은 14:30처럼 넘기세요.")
    if time not in _day_slots(day):
        refuse("precondition_failed", f"{spoken_date(day)} {spoken_time(time)}에는 진료가 없습니다. list_available_slots로 빈 시간을 확인하세요.")
    if time not in free_slots(day, book):
        nearby = free_slots(day, book)[:3]
        refuse("precondition_failed", f"{spoken_date(day)} {spoken_time(time)} 시간은 이미 찼거나 지났습니다."
               + (" 가까운 빈 시간: " + ", ".join(spoken_time(t) for t in nearby) + "." if nearby else " 그날은 빈 시간이 없습니다."))
    if any(a["name"] == name and a["birth"] == birth and a["date"] == day.isoformat() for a in book):
        refuse("precondition_failed", "이 환자는 그날 이미 예약이 있습니다. 바꾸려면 먼저 취소해야 합니다.")
    return name, birth, day, time


def preview_book_appointment(arguments, ctx):
    name, birth, day, time = _booking_request(arguments, ctx)
    return ok("", {"name": name, "when": f"{spoken_date(day)} {spoken_time(time)}", "department": DEPARTMENT})


def book_appointment(arguments, ctx):
    name, birth, day, time = _booking_request(arguments, ctx)
    _counter[0] += 1
    appointment = {"id": f"C-{_counter[0]:04d}", "name": name, "birth": birth, "date": day.isoformat(), "time": time,
                   "reason": str(arguments.get("reason") or "").strip(), "visit_type": "재진" if _patient(name, birth) else "초진"}
    BOOKS.of(ctx).append(appointment)
    return ok(f"예약되었습니다. {name} 님, {spoken_date(day)} {spoken_time(time)} {DEPARTMENT} 진료({appointment['visit_type']}), "
              f"예약 번호 {appointment['id']}. 신분증을 가지고 10분 일찍 오시라고 안내하세요.",
              {"appointment_id": appointment["id"], "date": appointment["date"], "time": time, "visit_type": appointment["visit_type"]},
              executed=True)


def _own_appointment(arguments, ctx):
    name, birth = _need_patient(arguments)
    found = [a for a in BOOKS.of(ctx) if a["name"] == name and a["birth"] == birth and a["date"] >= now().date().isoformat()]
    if not found:
        refuse("not_found", f"{name} 님 이름으로 잡힌 예약이 없습니다.")
    return found[0]


def preview_cancel_appointment(arguments, ctx):
    a = _own_appointment(arguments, ctx)
    return ok("", {"name": a["name"], "when": f"{spoken_date(date.fromisoformat(a['date']))} {spoken_time(a['time'])}"})


def cancel_appointment(arguments, ctx):
    a = _own_appointment(arguments, ctx)
    BOOKS.of(ctx).remove(a)
    return ok(f"{spoken_date(date.fromisoformat(a['date']))} {spoken_time(a['time'])} 예약을 취소했습니다.",
              {"appointment_id": a["id"], "cancelled": True}, executed=True)
