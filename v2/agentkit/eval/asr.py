"""Speech recognition quality (docs/ASR_QUALITY.md): recognition cases and running them again.

A case is one user utterance somebody marked as misheard: where its audio is (the session's own
recording, so only test and simulated sessions can have cases), what was really said, and what the
recogniser made of it at the time. Document kind "asr_case", stored unmasked.

The bench runs the audio of the cases through each recogniser this process has
(agentkit.runtime.audio.shared(): Whisper in this process, Qwen3-ASR over HTTP) and scores the
character error rate. Document kind "asr_bench", one per run.

The signals and the turn flags themselves live in agentkit.trace.flags (the trace store's metrics
need them); they are re-exported here.
"""
import logging
import re
import statistics
import threading
import time
import uuid
import wave

import numpy as np

from agentkit.trace import flags
from agentkit.trace.flags import AUTO_SIGNALS, SUSPECT_SIGNALS, auto_signals, similar  # noqa: F401

log = logging.getLogger("agentkit.eval")

CASE_KIND = "asr_case"
BENCH_KIND = "asr_bench"
ENGINES = ("whisper", "qwen")
ACTIVE = set()          # bench runs this process is working on
_NOT_TEXT = re.compile(r"[\W_]+", re.UNICODE)


# ---- character error rate ---------------------------------------------------------------------------

def normalize(text):
    """The characters that count: no spaces, no punctuation, lower case."""
    return _NOT_TEXT.sub("", str(text or "")).lower()


def edit_distance(a, b):
    """Levenshtein distance between two strings."""
    if len(a) < len(b):
        a, b = b, a
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        new = [i]
        for j, cb in enumerate(b, 1):
            new.append(min(row[j] + 1, new[j - 1] + 1, row[j - 1] + (ca != cb)))
        row = new
    return row[-1]


def cer_counts(expected, heard):
    """(edits, expected characters) after normalize()."""
    expected, heard = normalize(expected), normalize(heard)
    return edit_distance(expected, heard), len(expected)


def cer(expected, heard):
    """Edit distance / number of expected characters; None when nothing was expected. Can exceed 1."""
    edits, n = cer_counts(expected, heard)
    return round(edits / n, 4) if n else None


# ---- cases ------------------------------------------------------------------------------------------

def case_id(session_id, turn):
    return f"{re.sub(r'[^A-Za-z0-9._-]', '_', str(session_id))[:120]}-{int(turn)}"


def audio_url(session_id, turn):
    return f"/api/sessions/{session_id}/audio/{int(turn)}/user"


def read_wav(path):
    """(float32 samples in [-1, 1], sample rate) of a 16-bit mono WAV."""
    with wave.open(str(path), "rb") as r:
        rate = r.getframerate()
        pcm = np.frombuffer(r.readframes(r.getnframes()), dtype="<i2")
    return pcm.astype(np.float32) / 32768.0, rate


def upsert_case(store, session, flag):
    """Creates or updates the case of a turn flagged misheard. Returns the case, or None when the
    turn has no stored user audio (a session that was not recorded)."""
    meta = session["meta"]
    session_id, turn = meta["session_id"], int(flag["turn"])
    path = store.audio_path(session_id, turn, "user")
    if not path.is_file():
        return None
    record = next((t for t in session.get("turns") or [] if int(t["turn"]) == turn), {})
    stt = next((s for s in reversed(session.get("spans") or [])
                if s.get("kind") == "stt" and int(s.get("turn") or 0) == turn), {})
    attrs = stt.get("attrs") or {}
    seconds = attrs.get("audio_s")
    if seconds is None:
        try:
            samples, rate = read_wav(path)
            seconds = round(len(samples) / rate, 3)
        except Exception:
            seconds = None
    key = case_id(session_id, turn)
    previous = store.get_document(CASE_KIND, key) or {}
    now = time.time()
    case = {
        "id": key, "session_id": session_id, "turn": turn, "agent_id": meta.get("agent_id"),
        "expected": str(flag.get("actual_text") or "").strip(),
        # what the recogniser returned then, as the trace store has it (masked)
        "heard": (stt.get("output") or {}).get("text") or record.get("user_text") or "",
        "engine": attrs.get("engine"), "language": attrs.get("language") or "ko",
        "audio_url": audio_url(session_id, turn), "audio_s": seconds,
        "created_at": previous.get("created_at") or now, "updated_at": now,
    }
    store.put_document(CASE_KIND, key, case, mask=False)
    return case


def flag_turn(store, session_id, turn, misheard, actual_text=None, note=None):
    """POST /api/sessions/{sid}/turns/{turn}/flag. Returns (flags, case or None).
    Raises LookupError for an unknown session or turn."""
    session = store.get_session(session_id)
    if session is None:
        raise LookupError(f"대화를 찾을 수 없습니다: {session_id}")
    # `actual_text` goes into the case as it was typed; the flag document is masked like any other
    doc = flags.set_human(store, session, turn, misheard, actual_text, note)
    case = None
    if misheard:
        typed = dict(doc)
        if actual_text is not None:
            typed["actual_text"] = actual_text
        else:
            typed["actual_text"] = (store.get_document(CASE_KIND, case_id(session_id, turn)) or {}).get(
                "expected") or doc.get("actual_text")
        case = upsert_case(store, session, typed)
    else:
        store.delete_document(CASE_KIND, case_id(session_id, turn))     # no longer a case
    return doc, case


def list_cases(store, agent=None):
    """Newest first. Cases whose audio is gone (the session was purged) are left out."""
    cases = []
    for doc in store.list_documents(CASE_KIND):
        case = dict(doc)
        if agent and case.get("agent_id") != agent:
            continue
        if not store.audio_path(case.get("session_id"), case.get("turn") or 0, "user").is_file():
            continue
        cases.append(case)
    return sorted(cases, key=lambda c: c.get("created_at") or 0, reverse=True)


def delete_case(store, key):
    return store.delete_document(CASE_KIND, key)


# ---- bench ------------------------------------------------------------------------------------------

def recognizers(names):
    """{engine: callable(audio, language) -> text, or None when the engine cannot be used now}.
    Uses the recognisers this process already holds, so no model is loaded twice."""
    from agentkit.runtime import audio as audio_parts
    parts = audio_parts.shared()
    out = {}
    for name in names:
        out[name] = None
        try:
            if name == "whisper":
                # loading and every call go through the one-worker pool the voice sessions use
                model = parts.stt_pool.submit(parts.whisper).result()
                out[name] = (lambda audio, language, model=model:
                             parts.stt_pool.submit(model.transcribe, audio, language).result()[0])
            elif name == "qwen":
                remote = parts.qwen()
                if remote.model_name() is not None:
                    out[name] = lambda audio, language, remote=remote: remote.transcribe(audio, language)[0]
        except Exception as e:
            log.warning("ASR bench: %s unavailable: %r", name, e)
    return out


def new_run_id():
    return f"asr-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def summarize(items, engines, available):
    """{engine: {n, cer, exact, exact_rate, ms_median, available}} over the items done so far.
    cer: all edits / all expected characters; exact: how many cases came out right (CER 0)."""
    out = {}
    for name in engines:
        edits = chars = exact = n = 0
        times = []
        for item in items:
            result = (item.get("results") or {}).get(name)
            if not result or result.get("cer") is None:
                continue
            e, c = cer_counts(item["expected"], result.get("text"))
            edits, chars, n = edits + e, chars + c, n + 1
            exact += int(e == 0)
            if result.get("ms") is not None:
                times.append(result["ms"])
        out[name] = {
            "n": n, "cer": round(edits / chars, 4) if chars else None, "exact": exact,
            "exact_rate": round(exact / n, 4) if n else None,
            "ms_median": round(statistics.median(times), 1) if times else None,
            "available": bool(available.get(name)),
        }
    return out


def first_document(run_id, engines, agent=None):
    return {"run_id": run_id, "status": "running", "agent": agent or None, "started_at": time.time(),
            "finished_at": None, "total": 0, "done": 0, "skipped": [],
            "engines": {name: {"n": 0, "cer": None, "exact": 0, "exact_rate": None, "ms_median": None,
                               "available": None} for name in engines},
            "items": []}


def run_bench(store, run_id, engines=None, agent=None, recognizers_fn=None, sample_rate=16000):
    """Runs every case (of one agent, or all) through the engines and stores the result document
    after each case. Returns the final document. Blocking: call it from a thread."""
    engines = [e for e in (engines or ENGINES)]
    doc = store.get_document(BENCH_KIND, run_id)
    doc = dict(doc) if doc else first_document(run_id, engines, agent)
    try:
        cases = list_cases(store, agent)
        usable = [c for c in cases if normalize(c.get("expected"))]
        doc["skipped"] = [c["id"] for c in cases if not normalize(c.get("expected"))]
        doc["total"] = len(usable)
        callers = (recognizers_fn or recognizers)(engines)
        available = {name: callers.get(name) is not None for name in engines}
        doc["engines"] = summarize([], engines, available)
        store.put_document(BENCH_KIND, run_id, doc, mask=False)
        for case in usable:
            item = {"case_id": case["id"], "session_id": case["session_id"], "turn": case["turn"],
                    "expected": case["expected"], "heard_then": case.get("heard") or "",
                    "audio_url": case.get("audio_url"), "results": {}}
            try:
                audio, rate = read_wav(store.audio_path(case["session_id"], case["turn"], "user"))
                if rate != sample_rate:
                    raise ValueError(f"{rate} Hz 음성은 다룰 수 없습니다")
            except Exception as e:
                audio = None
                item["error"] = f"음성을 읽지 못했습니다: {e}"
            for name in engines:
                call = callers.get(name)
                if call is None or audio is None:
                    continue
                started = time.perf_counter()
                try:
                    text = str(call(audio, case.get("language") or "ko") or "").strip()
                    item["results"][name] = {"text": text, "cer": cer(case["expected"], text),
                                             "ms": round((time.perf_counter() - started) * 1000, 1)}
                except Exception as e:
                    item["results"][name] = {"error": repr(e)[:300]}
            doc["items"].append(item)
            doc["done"] = len(doc["items"])
            doc["engines"] = summarize(doc["items"], engines, available)
            store.put_document(BENCH_KIND, run_id, doc, mask=False)
        doc.update(status="finished", finished_at=time.time())
    except Exception as e:
        log.exception("ASR bench %s failed", run_id)
        doc.update(status="error", error=repr(e)[:400], finished_at=time.time())
    store.put_document(BENCH_KIND, run_id, doc, mask=False)
    return doc


def start_bench(store, engines=None, agent=None, recognizers_fn=None):
    """Starts a run in a background thread and returns its id; the first document is already stored."""
    engines = list(engines or ENGINES)
    unknown = [e for e in engines if e not in ENGINES]
    if unknown or not engines:
        raise ValueError(f"모르는 인식 모델: {', '.join(unknown) or '(없음)'} (쓸 수 있는 값: {', '.join(ENGINES)})")
    run_id = new_run_id()
    store.put_document(BENCH_KIND, run_id, first_document(run_id, engines, agent), mask=False)
    ACTIVE.add(run_id)

    def work():
        try:
            run_bench(store, run_id, engines, agent, recognizers_fn)
        finally:
            ACTIVE.discard(run_id)

    threading.Thread(target=work, name=f"asr-bench-{run_id}", daemon=True).start()
    return run_id


def _current(doc):
    doc = dict(doc)
    if doc.get("status") == "running" and doc.get("run_id") not in ACTIVE:
        doc.update(status="error", error="실행하던 서버 프로세스가 끝나 중단되었습니다")
    return doc


def load_bench(store, run_id):
    doc = store.get_document(BENCH_KIND, run_id)
    return _current(doc) if doc else None


def list_benches(store, limit=20):
    """Summaries (no items), newest first."""
    runs = [_current(d) for d in store.list_documents(BENCH_KIND)]
    runs.sort(key=lambda d: d.get("started_at") or 0, reverse=True)
    return [{k: d.get(k) for k in ("run_id", "status", "agent", "started_at", "finished_at", "total", "done",
                                   "engines", "error") if k in d} for d in runs[:limit]]
