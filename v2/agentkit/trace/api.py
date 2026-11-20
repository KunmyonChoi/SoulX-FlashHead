"""REST rows owned by WP3 (ARCHITECTURE.md section 6):

    GET /api/sessions?agent=&status=&limit=        -> {"sessions": [...]}
    GET /api/sessions/{sid}                        -> {"meta", "status", "turns", "spans", ...};
                                                      every turn carries "flags" (flags.py)
    GET /api/sessions/{sid}/audio/{turn}/{role}    -> audio/wav (test and simulated sessions only)
    GET /api/metrics?agent=&version=&days=         -> aggregates, also by version and by day
    GET /api/live                                  -> Server-Sent Events, one JSON object per event
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import AsyncIterator, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from . import flags, get_store

router = APIRouter()

HEARTBEAT_S = 15.0        # a comment line is sent when nothing happened for this long
LIVE_QUEUE = 1000         # events waiting per client; more are dropped for that client


def _flag(value: Optional[str]) -> Optional[bool]:
    if value is None or value == "":
        return None
    return value.lower() in ("1", "true", "yes")


@router.get("/api/sessions")
def list_sessions(agent: Optional[str] = None, status: Optional[str] = None, limit: int = 50,
                  version: Optional[int] = None, test: Optional[str] = None) -> dict:
    rows = get_store().list_sessions(agent=agent or None, status=status or None, limit=limit,
                                     version=version, test=_flag(test))
    return {"sessions": rows}


@router.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> dict:
    session = get_store().get_session(session_id)
    if session is None:
        raise HTTPException(404, "대화를 찾을 수 없습니다")
    return flags.attach(get_store(), session)


@router.get("/api/sessions/{session_id}/audio/{turn}/{role}")
def get_audio(session_id: str, turn: int, role: str):
    if role not in ("user", "agent"):
        raise HTTPException(404, "role은 user 또는 agent입니다")
    path = get_store().audio_path(session_id, turn, role)
    if not path.is_file():
        raise HTTPException(404, "저장된 음성이 없습니다")
    return FileResponse(str(path), media_type="audio/wav")


@router.get("/api/metrics")
def metrics(agent: Optional[str] = None, version: Optional[int] = None, days: float = 7,
            test: Optional[str] = None) -> dict:
    return get_store().metrics(agent=agent or None, version=version, days=days, test=_flag(test))


async def live_events(request: Optional[Request] = None, heartbeat: Optional[float] = None) -> AsyncIterator[str]:
    """The SSE stream: "data: <json>\\n\\n" per event. Event types: hello, session, turn, span,
    session_end, audio, analysis, document. The writer thread only hands events to this loop
    (call_soon_threadsafe), so a slow or gone client cannot hold it up."""
    loop = asyncio.get_running_loop()
    waiting: asyncio.Queue = asyncio.Queue(maxsize=LIVE_QUEUE)

    def offer(event: dict) -> None:
        try:
            waiting.put_nowait(event)
        except asyncio.QueueFull:
            pass

    def on_event(event: dict) -> None:           # runs in the publishing thread
        loop.call_soon_threadsafe(offer, event)  # raises when the loop is closed -> unsubscribed

    unsubscribe = get_store().bus.subscribe(on_event)
    try:
        yield "retry: 2000\n\n"
        yield "data: " + json.dumps({"type": "hello", "t": time.time()}) + "\n\n"
        while True:
            try:
                event = await asyncio.wait_for(waiting.get(), heartbeat or HEARTBEAT_S)
            except asyncio.TimeoutError:
                if request is not None and await request.is_disconnected():
                    break
                yield ": ping\n\n"
                continue
            yield "data: " + json.dumps(event, ensure_ascii=False, default=str) + "\n\n"
    finally:
        unsubscribe()


@router.get("/api/live")
async def live(request: Request) -> StreamingResponse:
    return StreamingResponse(
        live_events(request), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})
