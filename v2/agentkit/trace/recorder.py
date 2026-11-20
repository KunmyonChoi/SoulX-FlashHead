"""contracts.Recorder: every call puts one item on a queue and returns. A writer thread masks
the items and writes them to the store. Nothing here raises into the caller (R-OBS-7): when
the queue is full or the database cannot be written the records are dropped and a warning is
logged.
"""
from __future__ import annotations

import atexit
import logging
import queue
import threading
import time
from typing import Any, Literal, Optional

from agentkit.contracts import SessionMeta, SessionStatus, Span, TurnRecord

from . import flags as turn_flags
from .masker import replace_secrets
from .store import TraceStore, _dumps

log = logging.getLogger("agentkit.trace")

MAX_QUEUE = 20000                 # items
MAX_AUDIO_BYTES = 64 * 1024 * 1024  # audio waiting in the queue
BATCH = 200
LIVE_PAYLOAD_LIMIT = 4000         # characters of span input/output sent to live subscribers
PURGE_EVERY_S = 6 * 3600.0


class TraceRecorder:
    def __init__(self, store: TraceStore, max_queue: int = MAX_QUEUE, start: bool = True,
                 auto_purge: bool = True) -> None:
        self.store = store
        self._queue: "queue.Queue[tuple]" = queue.Queue(maxsize=max_queue)
        self._start = start
        self._auto_purge = auto_purge
        self._thread: Optional[threading.Thread] = None
        self._thread_lock = threading.Lock()
        self._stop = threading.Event()
        self._idle = threading.Condition()
        self._pending = 0                 # items queued or being written
        self._audio_ok: dict[str, bool] = {}     # session -> may store audio (as far as this process knows)
        self._audio_bytes = 0
        self._last_warning = 0.0
        self._next_purge = time.time() + 30.0
        self.dropped = 0                  # items dropped: queue full, masking or writing failed
        self.written = 0

    # -- contracts.Recorder -----------------------------------------------------------------

    def start_session(self, meta: SessionMeta) -> None:
        try:
            data = meta.model_dump()
            if len(self._audio_ok) > 5000:       # sessions that were never ended
                self._audio_ok.clear()
            self._audio_ok[data["session_id"]] = bool(data.get("test") or data.get("simulated"))
            self._put(("session", data))
        except Exception as exc:
            self._warn("start_session", exc)

    def span(self, span: Span, pii_paths: list[str] | None = None) -> None:
        try:
            # model_dump copies the containers, so later changes by the caller (a message list
            # that keeps growing) do not reach the stored record
            self._put(("span", span.model_dump(), list(pii_paths) if pii_paths else None))
        except Exception as exc:
            self._warn("span", exc)

    def turn(self, turn: TurnRecord) -> None:
        try:
            self._put(("turn", turn.model_dump()))
        except Exception as exc:
            self._warn("turn", exc)

    def audio(self, session_id: str, turn: int, role: Literal["user", "agent"], pcm16: bytes) -> None:
        try:
            if self._audio_ok.get(session_id) is False or not pcm16:
                return                    # a production session: the audio is not even queued
            data = bytes(pcm16)
            if self._audio_bytes + len(data) > MAX_AUDIO_BYTES:
                self.dropped += 1
                self._warn("audio", "too much audio waiting; dropped")
                return
            self._audio_bytes += len(data)
            if not self._put(("audio", session_id, int(turn), role, data)):
                self._audio_bytes -= len(data)
        except Exception as exc:
            self._warn("audio", exc)

    def end_session(self, session_id: str, status: SessionStatus) -> None:
        try:
            self._put(("end", session_id, status, time.time()))
        except Exception as exc:
            self._warn("end_session", exc)

    # -- beyond the contract ----------------------------------------------------------------

    def flush(self, timeout: float = 2.0) -> bool:
        """Waits until everything queued so far is written (or dropped). True if that happened
        within `timeout`."""
        try:
            self._ensure_thread()
            deadline = time.monotonic() + timeout
            with self._idle:
                while self._pending > 0:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        return False
                    self._idle.wait(left)
            return True
        except Exception as exc:
            self._warn("flush", exc)
            return False

    def close(self, timeout: float = 2.0) -> None:
        """Writes what is queued and stops the writer thread."""
        try:
            if self._thread is not None and self._thread.is_alive():
                self.flush(timeout)
                self._stop.set()
                self._thread.join(timeout)
        except Exception as exc:
            self._warn("close", exc)

    def stats(self) -> dict:
        return {"queued": self._queue.qsize(), "pending": self._pending,
                "written": self.written, "dropped": self.dropped}

    # -- queue ------------------------------------------------------------------------------

    def _put(self, item: tuple) -> bool:
        with self._idle:
            self._pending += 1
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            self._done(1)
            self.dropped += 1
            self._warn(item[0], "queue full; record dropped")
            return False
        if self._thread is None:
            self._ensure_thread()
        return True

    def _done(self, n: int) -> None:
        with self._idle:
            self._pending -= n
            if self._pending <= 0:
                self._pending = 0
                self._idle.notify_all()

    def _warn(self, where: str, exc: Any) -> None:
        now = time.monotonic()
        if now - self._last_warning >= 5.0:      # at most one warning every five seconds
            self._last_warning = now
            try:
                log.warning("trace recorder (%s): %s [dropped so far: %d]", where, exc, self.dropped)
            except Exception:
                pass

    def _ensure_thread(self) -> None:
        if not self._start:
            return
        with self._thread_lock:
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, name="trace-writer", daemon=True)
                self._thread.start()

    # -- writer thread ----------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                first = self._queue.get(timeout=0.25)
            except queue.Empty:
                self._maybe_purge()
                continue
            batch = [first]
            while len(batch) < BATCH:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            try:
                self._write(batch)
            except Exception as exc:
                self.dropped += len(batch)
                self._warn("write", exc)
            finally:
                self._done(len(batch))

    def _maybe_purge(self) -> None:
        if not self._auto_purge or time.time() < self._next_purge:
            return
        self._next_purge = time.time() + PURGE_EVERY_S
        try:
            n = self.store.purge()
            if n:
                log.info("trace: purged %d sessions past the retention period", n)
        except Exception as exc:
            self._warn("purge", exc)

    def _mask_item(self, item: tuple, learned: dict[str, list]) -> Optional[tuple]:
        """Returns the item with personal data masked. Raw values found at pii paths are added
        to learned[session_id]."""
        masker = self.store.masker
        kind = item[0]
        if kind == "session":
            meta = dict(item[1])
            meta["label"] = masker.mask_text(meta.get("label") or "")
            meta["settings"] = masker.mask(meta.get("settings") or {})
            return ("session", meta)
        if kind == "span":
            span, paths = dict(item[1]), item[2]
            found = learned.setdefault(span["session_id"], [])
            body = masker.mask({"input": span.get("input"), "output": span.get("output")},
                               self._resolve_paths(span, paths), _learned=found)
            span["input"], span["output"] = body["input"], body["output"]
            span["attrs"] = masker.mask(span.get("attrs") or {}, _learned=found)
            span["name"] = masker.mask_text(span.get("name") or "")
            return ("span", span)
        if kind == "turn":
            turn = dict(item[1])
            turn["user_text"] = masker.mask_text(turn.get("user_text") or "")
            turn["agent_text"] = masker.mask_text(turn.get("agent_text") or "")
            return ("turn", turn)
        return item

    @staticmethod
    def _resolve_paths(span: dict, paths: Optional[list[str]]) -> Optional[list[str]]:
        """Paths are meant to start at "input." or "output.". A path that does not is tried in
        the places a tool span keeps its data, so a contract's bare field name still works."""
        if not paths:
            return None
        out = []
        for p in paths:
            p = str(p)
            head = p.split(".", 1)[0]
            if head in ("input", "output"):
                out.append(p)
            else:
                out += [f"input.{p}", f"output.{p}", f"input.arguments.{p}", f"output.data.{p}"]
        return out

    def _apply_secrets(self, item: tuple) -> tuple:
        kind = item[0]
        if kind == "span":
            pattern = self.store.secrets.pattern(item[1]["session_id"])
            if pattern is not None:
                span = item[1]
                for k in ("input", "output", "attrs", "name"):
                    span[k] = replace_secrets(span[k], pattern)
        elif kind == "turn":
            pattern = self.store.secrets.pattern(item[1]["session_id"])
            if pattern is not None:
                turn = item[1]
                turn["user_text"] = replace_secrets(turn["user_text"], pattern)
                turn["agent_text"] = replace_secrets(turn["agent_text"], pattern)
        return item

    def _write(self, batch: list[tuple]) -> None:
        store = self.store
        # 1. mask (nothing unmasked goes past this point)
        learned: dict[str, list] = {}
        masked: list[tuple] = []
        for item in batch:
            if item[0] == "audio":
                self._audio_bytes = max(0, self._audio_bytes - len(item[4]))
            try:
                m = self._mask_item(item, learned)
                if m is not None:
                    masked.append(m)
            except Exception as exc:
                self.dropped += 1
                self._warn("mask", exc)
        fresh = {sid: store.secrets.learn(sid, values) for sid, values in learned.items() if values}
        fresh = {sid: values for sid, values in fresh.items() if values}
        masked = [self._apply_secrets(m) for m in masked]

        # 2. write sessions, turns and spans in one transaction
        events: list[dict] = []
        records = [m for m in masked if m[0] != "audio"]
        if records or fresh:
            with store.connect() as conn:
                for sid in fresh:
                    pattern = store.secrets.pattern(sid)
                    if pattern is not None:
                        store.rewrite_secrets(conn, sid, pattern)
                for m in records:
                    kind = m[0]
                    if kind == "session":
                        store.write_session(conn, m[1])
                        events.append({"type": "session", "session_id": m[1]["session_id"], "meta": m[1],
                                       "status": "active"})
                    elif kind == "span":
                        span_id = store.write_span(conn, m[1])
                        events.append(self._span_event(m[1], span_id))
                    elif kind == "turn":
                        store.write_turn(conn, m[1])
                        t = m[1]
                        ms = (None if t.get("t_agent_start") is None
                              else round((t["t_agent_start"] - t["t_user_end"]) * 1000.0, 1))
                        events.append({"type": "turn", "session_id": t["session_id"], "turn": t["turn"],
                                       "record": {**t, "response_ms": ms}})
                    elif kind == "end":
                        store.write_session_end(conn, m[1], m[2], m[3])
                        events.append({"type": "session_end", "session_id": m[1], "status": m[2],
                                       "ended_at": m[3]})
            self.written += len(records)

        # 3. audio, only for test or simulated sessions
        for m in masked:
            if m[0] != "audio":
                continue
            _, sid, turn, role, pcm = m
            try:
                allowed = self._audio_ok.get(sid)
                if allowed is None:
                    flags = store.session_flags(sid)
                    allowed = bool(flags and (flags[0] or flags[1]))
                if allowed:
                    store.save_audio(sid, turn, role, pcm)
                    self.written += 1
                    events.append({"type": "audio", "session_id": sid, "turn": turn, "role": role})
            except Exception as exc:
                self.dropped += 1
                self._warn("audio", exc)

        for m in masked:
            if m[0] == "end":
                self._audio_ok.pop(m[1], None)
                store.secrets.forget(m[1])

        # 4. tell live subscribers (never blocks: see bus.py)
        for event in events:
            store.bus.publish(event)

        # 5. the automatic turn flags of the sessions that just ended (docs/ASR_QUALITY.md)
        for m in masked:
            if m[0] == "end":
                try:
                    turn_flags.refresh(store, m[1])
                except Exception as exc:
                    self._warn("turn flags", exc)

    @staticmethod
    def _span_event(span: dict, span_id: int) -> dict:
        record = dict(span)
        record["id"] = span_id
        record["ms"] = round((span["t_end"] - span["t_start"]) * 1000.0, 1)
        for k in ("input", "output"):
            if record.get(k) is not None and len(_dumps(record[k])) > LIVE_PAYLOAD_LIMIT:
                record[k] = None
                record["truncated"] = True
        return {"type": "span", "session_id": span["session_id"], "turn": span["turn"], "span": record}


def install_exit_flush(recorder: TraceRecorder) -> None:
    """A short-lived process (a test run, a script) still gets its last records written."""
    atexit.register(lambda: recorder.close(2.0))
