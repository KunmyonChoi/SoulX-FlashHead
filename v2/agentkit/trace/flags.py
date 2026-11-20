"""Turn flags (docs/ASR_QUALITY.md): what the system suspects about a turn and what a person said
about it. One document per flagged turn, kind "turn_flag", key "<session_id>/<turn>":

    {"session_id", "turn", "auto": [...], "misheard": bool | None, "actual_text", "note", "updated_at"}

`auto` is computed from the stored turns and spans (auto_signals): when a session ends (the
recorder calls refresh) and again whenever a session is read, so sessions recorded before this
existed, and sessions still running, show their signals too.
"""
from __future__ import annotations

import logging
import re
import time
from difflib import SequenceMatcher
from typing import Any, Iterable, Optional

log = logging.getLogger("agentkit.trace")

KIND = "turn_flag"
AUTO_SIGNALS = ("low_confidence", "repeat", "reask", "complaint", "empty_wait")
# Signals that say "the recogniser may have got this turn wrong". empty_wait is about the agent.
SUSPECT_SIGNALS = frozenset({"low_confidence", "repeat", "reask", "complaint"})

REPEAT_RATIO = 0.6
REPEAT_MIN_CHARS = 4
REASK = re.compile(r"다시\s*한\s*번\s*말씀|잘\s*전달되지|못\s*알아|다시\s*말씀")
COMPLAINT = re.compile(r"아까\s*말했|말했잖|몇\s*번을|언제까지\s*기다")
_NOT_TEXT = re.compile(r"[\W_]+", re.UNICODE)

EMPTY = {"auto": [], "misheard": None}


def _letters(text: Any) -> str:
    return _NOT_TEXT.sub("", str(text or "")).lower()


def similar(a: str, b: str) -> bool:
    """The "same thing said again" test: both at least 4 characters without spaces and punctuation,
    and the characters they share in order make up 0.6 or more of the two (difflib's ratio)."""
    a, b = _letters(a), _letters(b)
    if len(a) < REPEAT_MIN_CHARS or len(b) < REPEAT_MIN_CHARS:
        return False
    return SequenceMatcher(None, a, b, autojunk=False).ratio() >= REPEAT_RATIO


def _is_empty_wait(text: str) -> bool:
    try:
        from agentkit.runtime.guards import is_empty_wait
    except Exception as e:      # the trace module also runs without the runtime
        log.debug("no runtime guards (%s); empty_wait not computed", e)
        return False
    return is_empty_wait(text)


WITHHELD = "(내용 저장 안 함"     # what the recorder stores instead of a text (definition.privacy, trace/recorder.py)


def withheld(text) -> bool:
    return str(text or "").startswith(WITHHELD)


def auto_signals(turns: Iterable[dict], spans: Iterable[dict]) -> dict[int, list[str]]:
    """{turn number: [signal, ...]} for every turn given (an empty list when nothing is suspected).

    turns: dicts with turn, user_text, agent_text; spans: dicts with turn, kind, attrs."""
    low: set[int] = set()
    tools: set[int] = set()
    for span in spans:
        kind = span.get("kind")
        if kind == "stt":
            attrs = span.get("attrs") if isinstance(span.get("attrs"), dict) else {}
            if attrs.get("low_confidence"):
                low.add(int(span.get("turn") or 0))
        elif kind == "tool":
            tools.add(int(span.get("turn") or 0))
    out: dict[int, list[str]] = {}
    previous = None
    for turn in sorted(turns, key=lambda t: int(t.get("turn") or 0)):
        number = int(turn.get("turn") or 0)
        user, agent = str(turn.get("user_text") or ""), str(turn.get("agent_text") or "")
        found = []
        if number in low:
            found.append("low_confidence")
        if withheld(user) or withheld(agent):
            # the text was not kept: "(내용 저장 안 함 · 10자)" twice is not the customer repeating himself
            out[number] = found
            previous = None
            continue
        if previous is not None and similar(user, previous):
            found.append("repeat")
        if REASK.search(agent):
            found.append("reask")
        if COMPLAINT.search(user):
            found.append("complaint")
        if number not in tools and _is_empty_wait(agent):
            found.append("empty_wait")
        out[number] = found
        previous = user
    return out


def key(session_id: str, turn: int) -> str:
    return f"{session_id}/{int(turn)}"


def stored(store, session_id: str) -> dict[int, dict]:
    """The stored flag documents of a session, by turn."""
    out = {}
    for doc in store.list_documents(KIND, prefix=f"{session_id}/"):
        try:
            out[int(doc.get("turn"))] = dict(doc)
        except (TypeError, ValueError):
            continue
    return out


def _document(session_id: str, turn: int, auto: list[str], previous: Optional[dict] = None) -> dict:
    previous = previous or {}
    return {"session_id": session_id, "turn": int(turn), "auto": list(auto),
            "misheard": previous.get("misheard"), "actual_text": previous.get("actual_text") or "",
            "note": previous.get("note") or "", "updated_at": previous.get("updated_at") or time.time()}


def session_flags(store, session: dict, persist: Optional[bool] = None) -> dict[int, dict]:
    """{turn: flags} for every turn of a session as store.get_session returns it. A turn without
    a document and without signals gets {"auto": [], "misheard": None}.

    persist: write documents whose `auto` is missing or out of date. Default: only when the session
    has ended (a running session's signals can still change, they are computed on every read)."""
    session_id = session["meta"]["session_id"]
    docs = stored(store, session_id)
    auto = auto_signals(session.get("turns") or [], session.get("spans") or [])
    if persist is None:
        persist = session.get("status") not in (None, "active")
    out: dict[int, dict] = {}
    for turn, signals in auto.items():
        doc = docs.get(turn)
        if doc is None and not signals:
            out[turn] = dict(EMPTY)
            continue
        if doc is None:
            fresh = _document(session_id, turn, signals)
        elif list(doc.get("auto") or []) != signals:
            fresh = {**doc, "auto": signals}
        else:
            out[turn] = doc
            continue
        if persist:
            try:
                store.put_document(KIND, key(session_id, turn), fresh)
            except Exception as e:
                log.warning("turn flag not stored (%s/%s): %s", session_id, turn, e)
        out[turn] = fresh if (doc is not None or persist) else {"auto": signals, "misheard": None}
    return out


def refresh(store, session_id: str) -> dict[int, dict]:
    """Computes and stores the automatic signals of a session (called when it ends)."""
    session = store.get_session(session_id)
    if session is None:
        return {}
    return session_flags(store, session, persist=True)


def attach(store, session: dict) -> dict:
    """Adds `flags` to every turn of a session detail (GET /api/sessions/{sid})."""
    try:
        flags = session_flags(store, session)
    except Exception as e:
        log.warning("turn flags unavailable: %s", e)
        flags = {}
    for turn in session.get("turns") or []:
        turn["flags"] = flags.get(int(turn["turn"]), dict(EMPTY))
    return session


def set_human(store, session: dict, turn: int, misheard: bool, actual_text: Optional[str] = None,
              note: Optional[str] = None) -> dict:
    """Stores what a person said about a turn and returns the document. actual_text / note left
    out (None) keep what was stored. Raises LookupError for a turn the session does not have."""
    session_id = session["meta"]["session_id"]
    turn = int(turn)
    auto = auto_signals(session.get("turns") or [], session.get("spans") or [])
    if turn not in auto:
        raise LookupError(f"{session_id}에 {turn}번 턴이 없습니다")
    doc = _document(session_id, turn, auto[turn], stored(store, session_id).get(turn))
    doc["misheard"] = bool(misheard)
    if actual_text is not None:
        doc["actual_text"] = str(actual_text).strip()
    if note is not None:
        doc["note"] = str(note).strip()
    doc["updated_at"] = time.time()
    store.put_document(KIND, key(session_id, turn), doc)
    return dict(store.get_document(KIND, key(session_id, turn)) or doc)
