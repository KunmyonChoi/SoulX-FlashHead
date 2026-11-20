"""The trace store: <DATA_DIR>/trace.sqlite3 and <DATA_DIR>/audio/<session>/<turn>-<role>.wav.

Only masked data is written here. Sessions, turns and spans come in through the recorder's
writer thread (which masks first); documents are masked in put_document.
Every operation opens its own connection, so the store can be used from any thread and keeps
working after the database file was removed (it is created again).
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
import statistics
import threading
import time
import wave
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional

from .bus import Bus
from .masker import RegexMasker, SessionSecrets, replace_secrets

log = logging.getLogger("agentkit.trace")

AUDIO_RATE = 16000

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL,
    version     INTEGER NOT NULL,
    channel     TEXT NOT NULL,
    test        INTEGER NOT NULL DEFAULT 0,
    simulated   INTEGER NOT NULL DEFAULT 0,
    started_at  REAL NOT NULL,
    ended_at    REAL,
    status      TEXT NOT NULL DEFAULT 'active',
    label       TEXT NOT NULL DEFAULT '',
    settings    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS sessions_agent ON sessions(agent_id, version, started_at);
CREATE INDEX IF NOT EXISTS sessions_started ON sessions(started_at);
CREATE TABLE IF NOT EXISTS turns (
    session_id    TEXT NOT NULL,
    turn          INTEGER NOT NULL,
    user_text     TEXT NOT NULL DEFAULT '',
    agent_text    TEXT NOT NULL DEFAULT '',
    t_user_end    REAL NOT NULL,
    t_agent_start REAL,
    t_agent_end   REAL,
    interrupted   INTEGER NOT NULL DEFAULT 0,
    handoff       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, turn)
);
CREATE TABLE IF NOT EXISTS spans (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    turn       INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    name       TEXT NOT NULL DEFAULT '',
    t_start    REAL NOT NULL,
    t_end      REAL NOT NULL,
    ok         INTEGER NOT NULL DEFAULT 1,
    input      TEXT,
    output     TEXT,
    attrs      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS spans_session ON spans(session_id, turn, t_start);
CREATE INDEX IF NOT EXISTS spans_kind ON spans(kind, t_start);
CREATE TABLE IF NOT EXISTS documents (
    kind       TEXT NOT NULL,
    key        TEXT NOT NULL,
    session_id TEXT,
    value      TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (kind, key)
);
CREATE INDEX IF NOT EXISTS documents_session ON documents(session_id);
"""

# Document keys whose values are identifiers, copied as they are (see put_document).
DOCUMENT_ID_KEYS = frozenset({
    "session_id", "agent_id", "run_id", "item_id", "id", "key", "case_id", "base_run", "new_run",
    "test_run_id", "kind", "judge", "metric", "op", "type", "status", "channel", "state",
    "improved", "regressed", "unchanged_pass", "unchanged_fail", "mismatches", "evidence_turns",
})


class Document(dict):
    """A stored document: the value itself (a dict) plus where it came from."""
    kind: str = ""
    key: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _loads(text: Optional[str]) -> Any:
    if text is None:
        return None
    try:
        return json.loads(text)
    except Exception:
        return text


def _safe_name(session_id: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]", "_", str(session_id))[:120]
    return "_" if not name.strip(".") else name


def _median(values: list[float]) -> Optional[float]:
    return round(statistics.median(values), 1) if values else None


def _percentile(values: list[float], q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo), 1)


def _dist(values: list[float]) -> dict:
    return {"median": _median(values), "p90": _percentile(values, 0.9), "count": len(values)}


def _rate(part: int, whole: int) -> Optional[float]:
    return round(part / whole, 4) if whole else None


def analysis_summary(doc: Optional[dict]) -> Optional[dict]:
    """Verdict summary of an AnalysisResult document for the session list."""
    if not isinstance(doc, dict):
        return None
    criteria = [c for c in (doc.get("criteria") or []) if isinstance(c, dict)]
    passed = sum(1 for c in criteria if c.get("passed") is True)
    failed = sum(1 for c in criteria if c.get("passed") is False)
    safety_failed = any(c.get("safety") and c.get("passed") is False for c in criteria)
    return {
        "passed": passed,
        "failed": failed,
        "unjudged": len(criteria) - passed - failed,
        "total": len(criteria),
        "safety_failed": safety_failed,
        "all_passed": bool(criteria) and passed == len(criteria),
        "failed_ids": [c.get("id") for c in criteria if c.get("passed") is False],
        "created_at": doc.get("created_at"),
    }


class _AsrAcc:
    """Recognition quality over the voice turns (turns that have an stt span) of one group."""

    def __init__(self) -> None:
        self.voice_turns = 0
        self.suspect = 0
        self.misheard = 0
        self.signals: dict[str, int] = {}

    def add(self, signals: list[str], misheard: bool, suspect_set: frozenset) -> None:
        self.voice_turns += 1
        self.suspect += int(bool(suspect_set & set(signals)))
        self.misheard += int(bool(misheard))
        for name in signals:
            self.signals[name] = self.signals.get(name, 0) + 1

    def result(self) -> dict:
        n = self.voice_turns
        return {
            "voice_turns": n,
            "suspect_turns": self.suspect,
            "suspect_rate": _rate(self.suspect, n),
            "misheard_turns": self.misheard,
            "misheard_rate": _rate(self.misheard, n),
            "reask_rate": _rate(self.signals.get("reask", 0), n),
            "repeat_rate": _rate(self.signals.get("repeat", 0), n),
            "low_confidence_rate": _rate(self.signals.get("low_confidence", 0), n),
        }


class _Acc:
    """Accumulates the numbers of one group (everything, one agent version, one day)."""

    def __init__(self) -> None:
        self.sessions = 0
        self.status: dict[str, int] = {}
        self.turns = 0
        self.delays: list[float] = []
        self.span_ms: dict[str, list[float]] = {}
        self.tool_calls = 0
        self.tool_failures = 0
        self.tool_confirm = 0
        self.tool_executed = 0
        self.tool_errors: dict[str, int] = {}
        self.tool_names: dict[str, dict[str, int]] = {}
        self.handoff_sessions: set[str] = set()
        self.interruptions = 0
        self.analyzed = 0
        self.all_passed = 0
        self.judged = 0
        self.passed = 0
        self.safety_failures = 0
        self.criteria: dict[str, dict[str, int]] = {}
        self.asr = _AsrAcc()
        self.asr_engines: dict[str, _AsrAcc] = {}
        self.empty_wait = 0

    def result(self) -> dict:
        return {
            "sessions": self.sessions,
            "turns": self.turns,
            "status": dict(self.status),
            "response_ms": _dist(self.delays),
            "response_ms_median": _median(self.delays),
            "response_ms_p90": _percentile(self.delays, 0.9),
            "span_ms": {k: _dist(v) for k, v in sorted(self.span_ms.items())},
            "tools": {
                "calls": self.tool_calls,
                "failures": self.tool_failures,
                "failure_rate": _rate(self.tool_failures, self.tool_calls),
                "needs_confirmation": self.tool_confirm,
                "executed": self.tool_executed,
                "error_types": dict(self.tool_errors),
                "by_name": {
                    n: {**c, "failure_rate": _rate(c["failures"], c["calls"])}
                    for n, c in sorted(self.tool_names.items())
                },
            },
            "tool_failure_rate": _rate(self.tool_failures, self.tool_calls),
            "handoff": {"sessions": len(self.handoff_sessions),
                        "rate": _rate(len(self.handoff_sessions), self.sessions)},
            "handoff_rate": _rate(len(self.handoff_sessions), self.sessions),
            "interruptions": self.interruptions,
            "criteria": {
                "sessions_analyzed": self.analyzed,
                "sessions_all_passed": self.all_passed,
                "judged": self.judged,
                "passed": self.passed,
                "pass_rate": _rate(self.passed, self.judged),
                "safety_failures": self.safety_failures,
                "by_id": {
                    cid: {**c, "pass_rate": _rate(c["passed"], c["judged"])}
                    for cid, c in sorted(self.criteria.items())
                },
            },
            "criteria_pass_rate": _rate(self.passed, self.judged),
            "asr": self.asr.result(),
            "by_stt_engine": {name: acc.result() for name, acc in sorted(self.asr_engines.items())},
            "empty_wait_turns": self.empty_wait,
            "empty_wait_rate": _rate(self.empty_wait, self.turns),
        }


class TraceStore:
    def __init__(self, data_dir: Path | str, masker: Optional[RegexMasker] = None,
                 bus: Optional[Bus] = None) -> None:
        self.data_dir = Path(data_dir)
        self.db_path = self.data_dir / "trace.sqlite3"
        self.audio_dir = self.data_dir / "audio"
        self.masker = masker or RegexMasker()
        self.bus = bus or Bus()
        self.secrets = SessionSecrets()
        self._audio_lock = threading.Lock()

    # -- connections ------------------------------------------------------------------------

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """A connection for one unit of work. Commits on success, rolls back on error."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA secure_delete=ON")     # purged and rewritten content is zeroed
            conn.executescript(SCHEMA)
            yield conn
            conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()

    @contextmanager
    def _read(self) -> Iterator[Optional[sqlite3.Connection]]:
        """A connection for reading; None when there is no database yet."""
        if not self.db_path.exists():
            yield None
            return
        with self.connect() as conn:
            yield conn

    # -- writes used by the recorder (values are already masked) ---------------------------

    def write_session(self, conn: sqlite3.Connection, meta: dict) -> None:
        conn.execute(
            "INSERT INTO sessions(session_id, agent_id, version, channel, test, simulated, started_at,"
            " status, label, settings) VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(session_id) DO UPDATE SET agent_id=excluded.agent_id, version=excluded.version,"
            " channel=excluded.channel, test=excluded.test, simulated=excluded.simulated,"
            " started_at=excluded.started_at, label=excluded.label, settings=excluded.settings",
            (meta["session_id"], meta["agent_id"], int(meta["version"]), meta["channel"],
             int(bool(meta.get("test"))), int(bool(meta.get("simulated"))), float(meta["started_at"]),
             "active", meta.get("label") or "", _dumps(meta.get("settings") or {})),
        )

    def write_turn(self, conn: sqlite3.Connection, turn: dict) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO turns(session_id, turn, user_text, agent_text, t_user_end,"
            " t_agent_start, t_agent_end, interrupted, handoff) VALUES (?,?,?,?,?,?,?,?,?)",
            (turn["session_id"], int(turn["turn"]), turn.get("user_text") or "", turn.get("agent_text") or "",
             float(turn["t_user_end"]), turn.get("t_agent_start"), turn.get("t_agent_end"),
             int(bool(turn.get("interrupted"))), int(bool(turn.get("handoff")))),
        )

    def write_span(self, conn: sqlite3.Connection, span: dict) -> int:
        cur = conn.execute(
            "INSERT INTO spans(session_id, turn, kind, name, t_start, t_end, ok, input, output, attrs)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (span["session_id"], int(span["turn"]), span["kind"], span.get("name") or "",
             float(span["t_start"]), float(span["t_end"]), int(bool(span.get("ok", True))),
             None if span.get("input") is None else _dumps(span["input"]),
             None if span.get("output") is None else _dumps(span["output"]),
             _dumps(span.get("attrs") or {})),
        )
        return int(cur.lastrowid or 0)

    def write_session_end(self, conn: sqlite3.Connection, session_id: str, status: str, t: float) -> None:
        conn.execute("UPDATE sessions SET status=?, ended_at=? WHERE session_id=?", (status, t, session_id))

    def rewrite_secrets(self, conn: sqlite3.Connection, session_id: str, pattern: re.Pattern) -> int:
        """Replaces newly learned personal values in what this session already stored."""
        changed = 0
        for row in conn.execute("SELECT id, name, input, output, attrs FROM spans WHERE session_id=?",
                                (session_id,)).fetchall():
            new = {k: (None if row[k] is None else
                       (pattern.sub("[가려짐]", row[k]) if k == "name"
                        else _dumps(replace_secrets(_loads(row[k]), pattern))))
                   for k in ("name", "input", "output", "attrs")}
            if any(new[k] != row[k] for k in new):
                conn.execute("UPDATE spans SET name=?, input=?, output=?, attrs=? WHERE id=?",
                             (new["name"], new["input"], new["output"], new["attrs"], row["id"]))
                changed += 1
        for row in conn.execute("SELECT turn, user_text, agent_text FROM turns WHERE session_id=?",
                                (session_id,)).fetchall():
            user, agent = pattern.sub("[가려짐]", row["user_text"]), pattern.sub("[가려짐]", row["agent_text"])
            if user != row["user_text"] or agent != row["agent_text"]:
                conn.execute("UPDATE turns SET user_text=?, agent_text=? WHERE session_id=? AND turn=?",
                             (user, agent, session_id, row["turn"]))
                changed += 1
        return changed

    def session_flags(self, session_id: str) -> Optional[tuple[bool, bool]]:
        """(test, simulated) of a stored session, None when unknown."""
        try:
            with self._read() as conn:
                if conn is None:
                    return None
                row = conn.execute("SELECT test, simulated FROM sessions WHERE session_id=?",
                                   (session_id,)).fetchone()
                return (bool(row["test"]), bool(row["simulated"])) if row else None
        except Exception:
            return None

    # -- audio ------------------------------------------------------------------------------

    def audio_path(self, session_id: str, turn: int, role: str) -> Path:
        role = "agent" if role == "agent" else "user"
        return self.audio_dir / _safe_name(session_id) / f"{int(turn)}-{role}.wav"

    def save_audio(self, session_id: str, turn: int, role: str, pcm16: bytes) -> Path:
        """Writes (or extends) the WAV of one turn and role. The caller has checked that the
        session is a test or simulated one."""
        path = self.audio_path(session_id, turn, role)
        with self._audio_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            previous = b""
            if path.exists():
                try:
                    with wave.open(str(path), "rb") as r:
                        previous = r.readframes(r.getnframes())
                except Exception:
                    previous = b""
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(AUDIO_RATE)
                w.writeframes(previous + bytes(pcm16))
        return path

    def list_audio(self, session_id: str) -> list[dict]:
        folder = self.audio_dir / _safe_name(session_id)
        out = []
        if folder.is_dir():
            for f in folder.glob("*.wav"):
                m = re.fullmatch(r"(\d+)-(user|agent)", f.stem)
                if m:
                    out.append({"turn": int(m.group(1)), "role": m.group(2), "bytes": f.stat().st_size})
        return sorted(out, key=lambda a: (a["turn"], a["role"] != "user"))

    # -- sessions ---------------------------------------------------------------------------

    @staticmethod
    def _session_row(row: sqlite3.Row) -> dict:
        return {
            "session_id": row["session_id"],
            "agent_id": row["agent_id"],
            "agent": row["agent_id"],
            "version": row["version"],
            "channel": row["channel"],
            "label": row["label"],
            "test": bool(row["test"]),
            "simulated": bool(row["simulated"]),
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "status": row["status"],
        }

    def _analyses(self, conn: sqlite3.Connection, session_ids: list[str]) -> dict[str, dict]:
        """Latest analysis document per session."""
        out: dict[str, dict] = {}
        for i in range(0, len(session_ids), 500):
            chunk = session_ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT key, session_id, value FROM documents WHERE kind='analysis'"
                f" AND (session_id IN ({marks}) OR key IN ({marks})) ORDER BY updated_at",
                chunk + chunk).fetchall()
            wanted = set(chunk)
            for row in rows:
                sid = row["session_id"] if row["session_id"] in wanted else row["key"]
                value = _loads(row["value"])
                if isinstance(value, dict):
                    out[sid] = value
        return out

    @staticmethod
    def _flag_counts(conn: sqlite3.Connection, session_ids: list[str]) -> dict[str, dict]:
        """{session: {"suspect_turns", "misheard_turns"}} from the stored turn flags (written when a
        session ends or is opened; a session that is still running has none yet)."""
        from .flags import KIND, SUSPECT_SIGNALS
        out: dict[str, dict] = {}
        for i in range(0, len(session_ids), 500):
            chunk = session_ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for row in conn.execute(
                    f"SELECT session_id, value FROM documents WHERE kind=? AND session_id IN ({marks})",
                    [KIND] + chunk):
                value = _loads(row["value"])
                if not isinstance(value, dict):
                    continue
                entry = out.setdefault(row["session_id"], {"suspect_turns": 0, "misheard_turns": 0})
                entry["suspect_turns"] += int(bool(SUSPECT_SIGNALS & set(value.get("auto") or [])))
                entry["misheard_turns"] += int(value.get("misheard") is True)
        return out

    def list_sessions(self, agent: Optional[str] = None, status: Optional[str] = None, limit: int = 50,
                      version: Optional[int] = None, test: Optional[bool] = None) -> list[dict]:
        """Newest first. Each row: session_id, agent_id (= agent), version, channel, label, test,
        simulated, started_at, ended_at, status, turns (= turn_count), response_ms_median,
        response_ms_max, handoff, interrupted, analysis (None or the summary of analysis_summary)."""
        where, args = [], []
        if agent:
            where.append("agent_id=?"); args.append(agent)
        if status:
            where.append("status=?"); args.append(status)
        if version is not None:
            where.append("version=?"); args.append(int(version))
        if test is not None:
            where.append("(test=1 OR simulated=1)" if test else "(test=0 AND simulated=0)")
        sql = "SELECT * FROM sessions" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY started_at DESC, session_id LIMIT ?"
        args.append(max(1, min(int(limit or 50), 1000)))
        with self._read() as conn:
            if conn is None:
                return []
            rows = [self._session_row(r) for r in conn.execute(sql, args).fetchall()]
            ids = [r["session_id"] for r in rows]
            per: dict[str, dict] = {sid: {"n": 0, "delays": [], "handoff": False, "interrupted": 0} for sid in ids}
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                marks = ",".join("?" * len(chunk))
                for t in conn.execute(
                        f"SELECT session_id, t_user_end, t_agent_start, interrupted, handoff FROM turns"
                        f" WHERE session_id IN ({marks})", chunk):
                    p = per[t["session_id"]]
                    p["n"] += 1
                    if t["t_agent_start"] is not None:
                        p["delays"].append((t["t_agent_start"] - t["t_user_end"]) * 1000.0)
                    p["handoff"] = p["handoff"] or bool(t["handoff"])
                    p["interrupted"] += int(t["interrupted"])
            analyses = self._analyses(conn, ids)
            flagged = self._flag_counts(conn, ids)
        for r in rows:
            p = per[r["session_id"]]
            r["turns"] = r["turn_count"] = p["n"]
            r["response_ms_median"] = _median(p["delays"])
            r["response_ms_max"] = round(max(p["delays"]), 1) if p["delays"] else None
            r["handoff"] = p["handoff"] or r["status"] == "handoff"
            r["interrupted"] = p["interrupted"]
            r["analysis"] = analysis_summary(analyses.get(r["session_id"]))
            r["asr"] = flagged.get(r["session_id"]) or {"suspect_turns": 0, "misheard_turns": 0}
        return rows

    def get_session(self, session_id: str) -> Optional[dict]:
        """{"meta": SessionMeta fields, "status", "ended_at", "turns": [...], "spans": [...],
        "audio": [{turn, role}], "analysis": AnalysisResult dict or None}; None when unknown."""
        with self._read() as conn:
            if conn is None:
                return None
            row = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            if row is None:
                return None
            meta = {
                "session_id": row["session_id"], "agent_id": row["agent_id"], "version": row["version"],
                "channel": row["channel"], "test": bool(row["test"]), "simulated": bool(row["simulated"]),
                "started_at": row["started_at"], "settings": _loads(row["settings"]) or {},
                "label": row["label"],
            }
            turns = []
            for t in conn.execute("SELECT * FROM turns WHERE session_id=? ORDER BY turn", (session_id,)):
                d = {
                    "session_id": t["session_id"], "turn": t["turn"], "user_text": t["user_text"],
                    "agent_text": t["agent_text"], "t_user_end": t["t_user_end"],
                    "t_agent_start": t["t_agent_start"], "t_agent_end": t["t_agent_end"],
                    "interrupted": bool(t["interrupted"]), "handoff": bool(t["handoff"]),
                }
                d["response_ms"] = (None if t["t_agent_start"] is None
                                    else round((t["t_agent_start"] - t["t_user_end"]) * 1000.0, 1))
                turns.append(d)
            spans = []
            for s in conn.execute("SELECT * FROM spans WHERE session_id=? ORDER BY t_start, id", (session_id,)):
                spans.append({
                    "id": s["id"], "session_id": s["session_id"], "turn": s["turn"], "kind": s["kind"],
                    "name": s["name"], "t_start": s["t_start"], "t_end": s["t_end"],
                    "ms": round((s["t_end"] - s["t_start"]) * 1000.0, 1), "ok": bool(s["ok"]),
                    "input": _loads(s["input"]), "output": _loads(s["output"]),
                    "attrs": _loads(s["attrs"]) or {},
                })
            analysis = self._analyses(conn, [session_id]).get(session_id)
        return {
            "meta": meta, "status": row["status"], "ended_at": row["ended_at"],
            "turns": turns, "spans": spans, "audio": self.list_audio(session_id), "analysis": analysis,
        }

    # -- metrics ----------------------------------------------------------------------------

    def metrics(self, agent: Optional[str] = None, version: Optional[int] = None, days: float = 7,
                test: Optional[bool] = None) -> dict:
        """Aggregates over the sessions started in the last `days` days.

        Top level: the numbers of all matching sessions (see _Acc.result) plus
        "by_version": one entry per (agent, version) with the same numbers, and
        "by_day": one entry per calendar day (config.TIMEZONE).
        Recognition quality ("asr", "by_stt_engine", "empty_wait_turns", "empty_wait_rate") is
        computed here from the turns and spans (flags.auto_signals), not read from the stored
        turn flags, so it also covers sessions that are still running; only "misheard" comes
        from the documents.
        test: None = all sessions, True = only test/simulated, False = only real ones.
        """
        now = time.time()
        since = now - float(days) * 86400.0
        where, args = ["s.started_at >= ?"], [since]
        if agent:
            where.append("s.agent_id=?"); args.append(agent)
        if version is not None:
            where.append("s.version=?"); args.append(int(version))
        if test is not None:
            where.append("(s.test=1 OR s.simulated=1)" if test else "(s.test=0 AND s.simulated=0)")
        cond = " AND ".join(where)

        from .flags import SUSPECT_SIGNALS, auto_signals
        total = _Acc()
        quality: dict[str, dict] = {}     # session -> its turns and stt/tool spans, for the signals
        groups: dict[tuple[str, int], _Acc] = {}
        days_acc: dict[str, _Acc] = {}
        tz = _timezone()

        def accs(agent_id: str, ver: int, started_at: float) -> list[_Acc]:
            day = datetime.fromtimestamp(started_at, tz).strftime("%Y-%m-%d")
            return [total, groups.setdefault((agent_id, ver), _Acc()), days_acc.setdefault(day, _Acc())]

        with self._read() as conn:
            if conn is not None:
                for s in conn.execute(f"SELECT * FROM sessions s WHERE {cond}", args):
                    for a in accs(s["agent_id"], s["version"], s["started_at"]):
                        a.sessions += 1
                        a.status[s["status"]] = a.status.get(s["status"], 0) + 1
                        if s["status"] == "handoff":
                            a.handoff_sessions.add(s["session_id"])
                for t in conn.execute(
                        "SELECT s.agent_id, s.version, s.started_at, t.session_id, t.t_user_end, t.t_agent_start,"
                        " t.interrupted, t.handoff, t.turn, t.user_text, t.agent_text"
                        f" FROM turns t JOIN sessions s USING(session_id) WHERE {cond}", args):
                    quality.setdefault(t["session_id"], {
                        "group": (t["agent_id"], t["version"], t["started_at"]), "turns": [], "spans": [],
                        "engine": {}})["turns"].append(
                        {"turn": t["turn"], "user_text": t["user_text"], "agent_text": t["agent_text"]})
                    for a in accs(t["agent_id"], t["version"], t["started_at"]):
                        a.turns += 1
                        if t["t_agent_start"] is not None:
                            a.delays.append((t["t_agent_start"] - t["t_user_end"]) * 1000.0)
                        a.interruptions += int(t["interrupted"])
                        if t["handoff"]:
                            a.handoff_sessions.add(t["session_id"])
                for p in conn.execute(
                        "SELECT s.agent_id, s.version, s.started_at, p.session_id, p.kind, p.name, p.t_start,"
                        " p.t_end, p.ok, p.attrs, p.turn, CASE WHEN p.kind='tool' THEN p.output END AS output"
                        f" FROM spans p JOIN sessions s USING(session_id) WHERE {cond}", args):
                    ms = (p["t_end"] - p["t_start"]) * 1000.0
                    tool = None
                    if p["kind"] in ("stt", "tool") and p["session_id"] in quality:
                        entry = quality[p["session_id"]]
                        attrs = _loads(p["attrs"]) if p["kind"] == "stt" else {}
                        attrs = attrs if isinstance(attrs, dict) else {}
                        entry["spans"].append({"turn": p["turn"], "kind": p["kind"], "attrs": attrs})
                        if p["kind"] == "stt":
                            entry["engine"][p["turn"]] = str(attrs.get("engine") or "unknown")
                    if p["kind"] == "tool":
                        output, attrs = _loads(p["output"]), _loads(p["attrs"])
                        output = output if isinstance(output, dict) else {}
                        attrs = attrs if isinstance(attrs, dict) else {}
                        confirm = bool(output.get("needs_confirmation") or attrs.get("needs_confirmation"))
                        tool = {
                            "confirm": confirm,
                            "failed": (not p["ok"]) and not confirm,
                            "executed": bool(attrs.get("executed") or output.get("executed")),
                            "error": attrs.get("error_type") or output.get("error_type"),
                        }
                    for a in accs(p["agent_id"], p["version"], p["started_at"]):
                        a.span_ms.setdefault(p["kind"], []).append(ms)
                        if p["kind"] == "handoff":
                            a.handoff_sessions.add(p["session_id"])
                        if tool:
                            name = a.tool_names.setdefault(p["name"] or "?", {"calls": 0, "failures": 0})
                            a.tool_calls += 1
                            name["calls"] += 1
                            a.tool_confirm += int(tool["confirm"])
                            a.tool_executed += int(tool["executed"])
                            if tool["failed"]:
                                a.tool_failures += 1
                                name["failures"] += 1
                                err = str(tool["error"] or "unknown")
                                a.tool_errors[err] = a.tool_errors.get(err, 0) + 1
                misheard = set()
                for d in conn.execute(
                        "SELECT d.value FROM documents d JOIN sessions s ON s.session_id = d.session_id"
                        f" WHERE d.kind='turn_flag' AND {cond}", args):
                    value = _loads(d["value"])
                    if isinstance(value, dict) and value.get("misheard") is True:
                        misheard.add((value.get("session_id"), value.get("turn")))
                for sid, entry in quality.items():
                    targets = accs(*entry["group"])
                    for number, signals in auto_signals(entry["turns"], entry["spans"]).items():
                        if "empty_wait" in signals:
                            for a in targets:
                                a.empty_wait += 1
                        engine = entry["engine"].get(number)
                        if engine is None:
                            continue        # not a spoken turn
                        for a in targets:
                            a.asr.add(signals, (sid, number) in misheard, SUSPECT_SIGNALS)
                            a.asr_engines.setdefault(engine, _AsrAcc()).add(
                                signals, (sid, number) in misheard, SUSPECT_SIGNALS)
                latest: dict[str, tuple] = {}
                for d in conn.execute(
                        "SELECT s.agent_id, s.version, s.started_at, s.session_id, d.value FROM documents d"
                        " JOIN sessions s ON s.session_id = COALESCE(d.session_id, d.key)"
                        f" WHERE d.kind='analysis' AND {cond} ORDER BY d.updated_at", args):
                    latest[d["session_id"]] = (d["agent_id"], d["version"], d["started_at"], _loads(d["value"]))
                for agent_id, ver, started_at, doc in latest.values():
                    summary = analysis_summary(doc)
                    if summary is None:
                        continue
                    for a in accs(agent_id, ver, started_at):
                        a.analyzed += 1
                        a.all_passed += int(summary["all_passed"])
                        a.safety_failures += int(summary["safety_failed"])
                        for c in doc.get("criteria") or []:
                            if not isinstance(c, dict) or c.get("passed") is None:
                                continue
                            entry = a.criteria.setdefault(str(c.get("id")), {"judged": 0, "passed": 0})
                            a.judged += 1
                            entry["judged"] += 1
                            if c.get("passed") is True:
                                a.passed += 1
                                entry["passed"] += 1

        out = total.result()
        out.update({"agent": agent, "version": version, "days": days, "since": since, "until": now})
        out["by_version"] = [
            {"agent_id": a, "agent": a, "version": v, **acc.result()} for (a, v), acc in sorted(groups.items())
        ]
        out["by_day"] = [{"day": day, **acc.result()} for day, acc in sorted(days_acc.items())]
        return out

    # -- documents --------------------------------------------------------------------------

    def put_document(self, kind: str, key: str, value: dict, mask: bool = True) -> None:
        """Stores (replaces) a document. Strings in it are masked unless mask=False; the values
        of identifier keys (DOCUMENT_ID_KEYS) are kept as they are. Raises if the write fails:
        this is not on the conversation path."""
        session_id = value.get("session_id") if isinstance(value, dict) else None
        stored: Any = value
        if mask:
            stored = self.masker.mask(value, protect_keys=DOCUMENT_ID_KEYS)
            if session_id:
                stored = replace_secrets(stored, self.secrets.pattern(str(session_id)))
        else:
            stored = self.masker._plain(value)
        now = time.time()
        with self.connect() as conn:
            if not session_id and conn.execute("SELECT 1 FROM sessions WHERE session_id=?", (key,)).fetchone():
                session_id = key
            conn.execute(
                "INSERT INTO documents(kind, key, session_id, value, created_at, updated_at) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(kind, key) DO UPDATE SET session_id=excluded.session_id, value=excluded.value,"
                " updated_at=excluded.updated_at",
                (kind, key, None if session_id is None else str(session_id), _dumps(stored), now, now))
        event = {"type": "analysis" if kind == "analysis" else "document", "kind": kind, "key": key,
                 "session_id": session_id, "t": now}
        if kind == "analysis":
            event["summary"] = analysis_summary(stored if isinstance(stored, dict) else None)
        self.bus.publish(event)

    @staticmethod
    def _document(row: sqlite3.Row) -> Document:
        value = _loads(row["value"])
        doc = Document(value if isinstance(value, dict) else {"value": value})
        doc.kind, doc.key = row["kind"], row["key"]
        doc.created_at, doc.updated_at = row["created_at"], row["updated_at"]
        return doc

    def get_document(self, kind: str, key: str) -> Optional[Document]:
        with self._read() as conn:
            if conn is None:
                return None
            row = conn.execute("SELECT * FROM documents WHERE kind=? AND key=?", (kind, key)).fetchone()
            return self._document(row) if row else None

    def list_documents(self, kind: str, prefix: str = "") -> list[Document]:
        """The documents of a kind whose key starts with `prefix`, ordered by key. Each entry is
        the stored dict; `.key`, `.created_at`, `.updated_at` are attributes on it."""
        with self._read() as conn:
            if conn is None:
                return []
            rows = conn.execute(
                "SELECT * FROM documents WHERE kind=? AND substr(key, 1, ?) = ? ORDER BY key",
                (kind, len(prefix), prefix)).fetchall()
            return [self._document(r) for r in rows]

    def delete_document(self, kind: str, key: str) -> bool:
        with self.connect() as conn:
            return conn.execute("DELETE FROM documents WHERE kind=? AND key=?", (kind, key)).rowcount > 0

    # -- retention --------------------------------------------------------------------------

    def purge(self, older_than_days: Optional[float] = None) -> int:
        """Deletes sessions that ended (or, if never ended, started) more than `older_than_days`
        ago, with their turns, spans, session documents and audio. Returns the number of sessions."""
        if older_than_days is None:
            from agentkit import config
            older_than_days = config.RETENTION_DAYS
        cutoff = time.time() - float(older_than_days) * 86400.0
        removed: list[str] = []
        with self._read() as conn:
            if conn is not None:
                removed = [r["session_id"] for r in conn.execute(
                    "SELECT session_id FROM sessions WHERE COALESCE(ended_at, started_at) < ?", (cutoff,))]
                for i in range(0, len(removed), 500):
                    chunk = removed[i:i + 500]
                    marks = ",".join("?" * len(chunk))
                    conn.execute(f"DELETE FROM turns WHERE session_id IN ({marks})", chunk)
                    conn.execute(f"DELETE FROM spans WHERE session_id IN ({marks})", chunk)
                    conn.execute(f"DELETE FROM documents WHERE session_id IN ({marks})", chunk)
                    conn.execute(f"DELETE FROM documents WHERE kind='analysis' AND key IN ({marks})", chunk)
                    conn.execute(f"DELETE FROM sessions WHERE session_id IN ({marks})", chunk)
                # records whose session row never arrived
                conn.execute("DELETE FROM spans WHERE t_end < ? AND session_id NOT IN (SELECT session_id FROM sessions)",
                             (cutoff,))
                conn.execute("DELETE FROM turns WHERE t_user_end < ? AND session_id NOT IN (SELECT session_id FROM sessions)",
                             (cutoff,))
                known = {_safe_name(r["session_id"]) for r in conn.execute("SELECT session_id FROM sessions")}
            else:
                known = set()
        for sid in removed:
            self.secrets.forget(sid)
            shutil.rmtree(self.audio_dir / _safe_name(sid), ignore_errors=True)
        if self.audio_dir.is_dir():
            for folder in self.audio_dir.iterdir():
                try:
                    if folder.is_dir() and folder.name not in known and folder.stat().st_mtime < cutoff:
                        shutil.rmtree(folder, ignore_errors=True)
                except OSError:
                    continue
        return len(removed)


def _timezone():
    try:
        from zoneinfo import ZoneInfo
        from agentkit import config
        return ZoneInfo(config.TIMEZONE)
    except Exception:
        return None
