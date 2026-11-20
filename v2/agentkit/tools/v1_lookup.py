"""
The seven lookup tools of v1, behind adapter kind "python".

Six of them are methods of realtime_tools.Toolbox (repository root; imported, never edited).
The seventh, list_knowledge_by_date, lives in v1's realtime_mic_app.py, which loads GPU models on
import, so that one small method (KnowledgeClient.list_by_date) is written out again here.

Each function has the adapter signature `await fn(arguments, ctx)`, returns the text for the model
and raises ToolFailure / AdapterUnavailable for the two kinds of failure.

Options through the environment: AGENTKIT_DEFAULT_LOCATION (weather when no place is given,
default 서울), AGENTKIT_WEKNORA_KB (knowledge base id).
"""
import asyncio
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from .. import config
from .adapters import AdapterUnavailable, ToolFailure

DEFAULT_KB_ID = "29f24b56-2379-4a82-96c7-e1b01963206c"
WEEKDAY_NAMES = "월화수목금토일"
LIST_ITEMS = 5              # documents described in one tool result (as in v1)
DESCRIPTION_CHARS = 260
KNOWLEDGE_TIMEOUT = 4.0

_toolboxes = {}             # event loop -> Toolbox (its httpx client belongs to one loop)


def _v1():
    root = str(config.REPO_ROOT)
    if root not in sys.path:
        sys.path.append(root)
    import realtime_tools
    return realtime_tools


def _toolbox():
    loop = asyncio.get_running_loop()
    for old in [old for old in _toolboxes if old.is_closed()]:
        del _toolboxes[old]
    if loop not in _toolboxes:
        _toolboxes[loop] = _v1().Toolbox(location=os.environ.get("AGENTKIT_DEFAULT_LOCATION", "서울"),
                                         tz_name=config.TIMEZONE)
    return _toolboxes[loop]


def _classify(message):
    """v1 raises one ToolError for everything; the wording tells which kind of failure it is."""
    if re.search(r"조회 실패|요청을 받지 않습니다|가져오지 못했습니다|응답하지 않습니다", message):
        return "system_error"
    if re.search(r"다시 호출하세요|알 수 없습니다|검색어가 없습니다|까지만 조회할 수", message):
        return "invalid_input"
    return "not_found"


async def _run(method_name, *args):
    v1 = _v1()
    try:
        return await getattr(_toolbox(), method_name)(*args)
    except v1.ToolError as e:
        raise ToolFailure(_classify(str(e)), str(e))
    except (httpx.HTTPError, ET.ParseError) as e:
        raise AdapterUnavailable(f"{type(e).__name__}: {e}")


async def get_weather(arguments, ctx):
    return await _run("get_weather", arguments.get("location"), arguments.get("day_offset"))


async def search_news(arguments, ctx):
    return await _run("search_news", arguments.get("query"))


async def get_news_detail(arguments, ctx):
    return await _run("get_news_detail", arguments.get("topic"))


async def search_geeknews(arguments, ctx):
    return await _run("search_geeknews", arguments.get("query"))


async def read_geeknews(arguments, ctx):
    return await _run("read_geeknews", arguments.get("title"))


async def search_wikipedia(arguments, ctx):
    return await _run("search_wikipedia", arguments.get("query"), getattr(ctx, "language", "ko") or "ko")


# ---- knowledge base listing (WeKnora) ---------------------------------------------------------


def _weknora_key():
    try:
        with open(os.path.expanduser(str(config.WEKNORA_KEY_FILE))) as f:
            return f.read().strip() or None
    except OSError:
        return None


def describe_knowledge(items, date, tz):
    """items: WeKnora knowledge entries (dicts with created_at, title/file_name, description),
    newest first. Returns the text for the model. Same wording and parsing as v1."""
    day = datetime.strptime(date[:10], "%Y-%m-%d").date() if date else None
    parsed = []
    for item in items:
        try:
            # Python 3.10 only reads fractions of exactly 3 or 6 digits; WeKnora drops trailing zeros
            stamp = re.sub(r"\.\d+", "", str(item.get("created_at"))).replace("Z", "+00:00")
            created = datetime.fromisoformat(stamp).astimezone(tz)
        except ValueError:
            continue
        parsed.append((created, item.get("title") or item.get("file_name") or "제목 없음",
                       re.sub(r"\s+", " ", item.get("description") or "").strip()))
    if not parsed:
        return "지식베이스에 자료가 없습니다."

    def spoken(d):
        return f"{d.month}월 {d.day}일 {WEEKDAY_NAMES[d.weekday()]}요일"

    def describe(chosen):
        return "\n".join(
            f"{i}. 제목: {title}\n   추가 시각: {created.month}월 {created.day}일 {created.hour}시 {created.minute}분\n"
            f"   요약: {summary[:DESCRIPTION_CHARS] or '요약 없음'}"
            for i, (created, title, summary) in enumerate(chosen, 1))

    if day is None:
        chosen = parsed[:LIST_ITEMS]
        return f"가장 최근에 추가된 자료 {len(chosen)}건 (전체 {len(parsed)}건 중):\n" + describe(chosen)
    matched = [item for item in parsed if item[0].date() == day]
    if matched:
        more = f" 이 중 최근 {LIST_ITEMS}건만 적습니다." if len(matched) > LIST_ITEMS else ""
        return f"{spoken(day)}에 추가된 자료는 {len(matched)}건입니다.{more}\n" + describe(matched[:LIST_ITEMS])
    counts = {}
    for created, _, _ in parsed:
        counts[created.date()] = counts.get(created.date(), 0) + 1
    near = sorted(counts, key=lambda d: abs((d - day).days))[:3]
    return (f"{spoken(day)}에 추가된 자료는 없습니다. 자료가 추가된 가까운 날짜: "
            + ", ".join(f"{spoken(d)} {counts[d]}건" for d in sorted(near)) + ".")


def _created(item, tz):
    stamp = re.sub(r"\.\d+", "", str(item.get("created_at"))).replace("Z", "+00:00")
    return datetime.fromisoformat(stamp).astimezone(tz)


async def list_knowledge_by_date(arguments, ctx):
    date = str(arguments.get("date") or "").strip()
    day = None
    if date:
        try:
            day = datetime.strptime(date[:10], "%Y-%m-%d").date()
        except ValueError:
            raise ToolFailure("invalid_input", f"날짜 '{date}'를 이해하지 못했습니다. YYYY-MM-DD 형식으로 다시 호출하세요.")
    key = _weknora_key()
    if not key:
        raise ToolFailure("system_error", "지식베이스에 연결할 열쇠가 없어 자료 목록을 볼 수 없습니다. 지금은 확인이 어렵다고 말하세요.")
    kb_id = os.environ.get("AGENTKIT_WEKNORA_KB", DEFAULT_KB_ID)
    tz = ZoneInfo(config.TIMEZONE)
    url = f"{str(config.WEKNORA_URL).rstrip('/')}/knowledge-bases/{kb_id}/knowledge"
    items, page = [], 1
    try:
        async with httpx.AsyncClient(headers={"X-API-Key": key}) as client:
            while True:
                response = await client.get(
                    url, params={"page": page, "page_size": 100, "sort_by": "created_at", "sort_order": "desc"},
                    timeout=KNOWLEDGE_TIMEOUT)
                if response.status_code in (401, 403):
                    raise ToolFailure("permission_denied", "지식베이스가 접근을 거절했습니다. 지금은 자료 목록을 확인하기 어렵다고 말하세요.")
                if response.status_code == 404:
                    raise ToolFailure("not_found", "지식베이스를 찾지 못했습니다. 지금은 자료 목록을 확인하기 어렵다고 말하세요.")
                if response.status_code != 200:
                    raise AdapterUnavailable(f"HTTP {response.status_code}")
                body = response.json()
                batch = body.get("data") or []
                items.extend(batch)
                last = None
                for item in reversed(batch):
                    try:
                        last = _created(item, tz)
                        break
                    except ValueError:
                        continue
                # newest first: once a page ends before the wanted day, nothing older is needed
                if not batch or len(items) >= int(body.get("total") or 0) or page >= 5 \
                        or (day and last and last.date() < day):
                    break
                page += 1
    except httpx.HTTPError as e:
        raise AdapterUnavailable(f"{type(e).__name__}: {e}")
    return describe_knowledge(items, date, tz)
