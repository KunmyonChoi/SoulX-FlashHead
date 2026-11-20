"""Small helpers shared by the dummy clinic, burger shop and airline (the newer dummy systems, reached
through adapters of kind "python": every tool body is `fn(arguments, ctx)` returning the wire dict)."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .. import config
from ..tools.adapters import ToolFailure

WEEKDAYS = "월화수목금토일"


def now():
    return datetime.now(ZoneInfo(config.TIMEZONE)).replace(tzinfo=None)


def ok(text, data=None, **more):
    return {"ok": True, "text": text, "data": data, "error_type": None, **more}


def refuse(error_type, text, data=None):
    raise ToolFailure(error_type, text, data)


def won(amount):
    return f"{int(amount)}원"


def spoken_date(day):
    return f"{day.month}월 {day.day}일 {WEEKDAYS[day.weekday()]}요일"


def spoken_time(value):
    """ "14:30" or a time/datetime -> "오후 2시 30분" """
    if isinstance(value, str):
        hour, minute = (int(x) for x in value.split(":")[:2])
    else:
        hour, minute = value.hour, value.minute
    text = f"{'오전' if hour < 12 else '오후'} {hour % 12 or 12}시"
    return text + (f" {minute}분" if minute else "")


def read_date(value, default=None):
    """Lenient: "2026-11-19", "11월 19일", "내일", "모레", "오늘", "20261119". Returns a date or None."""
    text = str(value or "").strip()
    today = now().date()
    if not text:
        return default
    for word, offset in (("오늘", 0), ("내일", 1), ("모레", 2), ("today", 0), ("tomorrow", 1)):
        if word in text.lower():
            return today + timedelta(days=offset)
    m = re.search(r"(\d{4})\D{0,3}(\d{1,2})\D{0,3}(\d{1,2})", text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.search(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일?", text) or re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})", text)
    if m:
        try:
            day = date(today.year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
        return day if day >= today - timedelta(days=30) else day.replace(year=today.year + 1)
    return None


def read_time(value):
    """ "14:30", "오후 2시 30분", "2시 반", "오전 10시" -> "HH:MM" or None."""
    text = str(value or "").strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
    else:
        m = re.search(r"(\d{1,2})\s*시\s*(반|(\d{1,2})\s*분)?", text)
        if not m:
            return None
        hour = int(m.group(1))
        minute = 30 if m.group(2) == "반" else int(m.group(3) or 0)
        if "오후" in text and hour < 12:
            hour += 12
        elif "오전" not in text and "오후" not in text and 1 <= hour <= 6:
            hour += 12                     # "3시" in a daytime business means the afternoon
    return f"{hour:02d}:{minute:02d}" if 0 <= hour < 24 and 0 <= minute < 60 else None


def birth_key(value):
    """ "1991-06-20", "1991년 6월 20일", "910620" -> "19910620" (or "" if it cannot be read)."""
    text = str(value or "")
    digits = re.sub(r"\D", "", text)
    m = re.search(r"(\d{4})\D{1,3}(\d{1,2})\D{1,3}(\d{1,2})", text)
    if m:
        return f"{int(m.group(1)):04d}{int(m.group(2)):02d}{int(m.group(3)):02d}"
    if len(digits) == 6:
        return ("19" if int(digits[:2]) > 30 else "20") + digits
    return digits if len(digits) == 8 else ""


def name_key(value):
    return re.sub(r"\s+|님$|씨$", "", str(value or ""))


class PerSession(dict):
    """State of one conversation each (like the cafe's order sheet): a test run or a second caller never
    sees what another conversation booked. Python adapters are not told when a session ends, so the
    oldest conversations are dropped once there are many."""

    def __init__(self, make, limit=500):
        super().__init__()
        self.make, self.limit = make, limit

    def of(self, ctx):
        if isinstance(ctx, dict):
            key = str(ctx.get("session_id") or "default")
        else:
            key = str(getattr(ctx, "session_id", None) or "default")
        if key not in self:
            while len(self) >= self.limit:
                self.pop(next(iter(self)))
            self[key] = self.make()
        return self[key]
