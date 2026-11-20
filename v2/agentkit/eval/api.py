"""REST of the evaluation module (ARCHITECTURE.md section 6, rows owned by WP6).

Collaborators come from agentkit.eval.configure(...) or the real modules.
"""
import asyncio
import time
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from agentkit.contracts import TestRun

from . import asr, deps
from .analysis import analyze_session
from .suite import ACTIVE, compare, list_runs, load_run, new_run_id, run_suite, save_case

router = APIRouter()
_tasks = {}     # run_id -> asyncio.Task of a suite started by this process


class RunRequest(BaseModel):
    agent: str
    version: Optional[int] = None       # None: the deployed stable version


class SaveCaseRequest(BaseModel):
    case_id: str
    expect: dict[str, bool] = Field(default_factory=dict)
    facts: dict[str, Any] = Field(default_factory=dict)
    description: str = ""


class FlagRequest(BaseModel):
    misheard: bool
    actual_text: Optional[str] = None
    note: Optional[str] = None


class BenchRequest(BaseModel):
    engines: Optional[list[str]] = None     # default: every engine (whisper, qwen)
    agent: Optional[str] = None             # default: the cases of every agent


def _unavailable(e):
    return HTTPException(503, f"필요한 모듈을 쓸 수 없습니다: {e}")


@router.post("/api/sessions/{session_id}/analyze")
async def post_analyze(session_id: str):
    """Runs the analysis now (several LLM calls) and returns the AnalysisResult."""
    try:
        result = await analyze_session(session_id)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ImportError as e:
        raise _unavailable(e)
    return result.model_dump()


@router.get("/api/sessions/{session_id}/analysis")
async def get_analysis(session_id: str):
    try:
        document = await deps.get_document(deps.get_store(), "analysis", session_id)
    except ImportError as e:
        raise _unavailable(e)
    if document is None:
        raise HTTPException(404, "이 대화의 분석 결과가 아직 없습니다.")
    return document


@router.post("/api/sessions/{session_id}/save_case")
async def post_save_case(session_id: str, body: SaveCaseRequest):
    try:
        return await save_case(session_id, body.case_id, body.expect, facts=body.facts,
                               description=body.description)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except ImportError as e:
        raise _unavailable(e)


@router.post("/api/tests/run")
async def post_run(body: RunRequest):
    """Starts the suite in the background and returns {"run_id", "status": "running"} at once."""
    try:
        store = deps.get_store()
        definition = deps.get_definition_store().load(body.agent, body.version)
    except ImportError as e:
        raise _unavailable(e)
    except Exception as e:
        raise HTTPException(404, f"정의서를 읽지 못했습니다: {e}")
    version = body.version if body.version is not None else definition.version
    run_id = new_run_id(body.agent, version)
    total = len(definition.tests.personas) + len(definition.tests.cases)
    # a first document, so GET /api/tests/runs/{run_id} answers before the task gets to run
    first = TestRun(run_id=run_id, agent_id=body.agent, version=version, started_at=time.time()).model_dump()
    first.update(status="running", total=total, skipped=[])
    await deps.maybe_await(store.put_document("test_run", run_id, first))

    async def work():
        try:
            await run_suite(body.agent, version, run_id)
        except Exception as e:
            deps.log.exception("test run %s failed", run_id)
            document = await deps.get_document(store, "test_run", run_id) or first
            document.update(status="error", error=repr(e)[:400], finished_at=time.time())
            await deps.maybe_await(store.put_document("test_run", run_id, document))
        finally:
            _tasks.pop(run_id, None)
            ACTIVE.discard(run_id)

    ACTIVE.add(run_id)
    _tasks[run_id] = asyncio.create_task(work())
    return {"run_id": run_id, "agent": body.agent, "version": version, "status": "running", "total": total}


@router.get("/api/tests/runs")
async def get_runs(agent: Optional[str] = None):
    try:
        return {"runs": await list_runs(agent)}
    except ImportError as e:
        raise _unavailable(e)


@router.get("/api/tests/runs/{run_id}")
async def get_run(run_id: str):
    """The TestRun so far, plus "status" (running | finished | error), "total" and "skipped"."""
    try:
        document = await load_run(run_id)
    except ImportError as e:
        raise _unavailable(e)
    if document is None:
        raise HTTPException(404, f"시험 실행을 찾을 수 없습니다: {run_id}")
    if document.get("status") == "running" and run_id not in ACTIVE:
        document["status"] = "interrupted"      # started by a process that is gone
    return document


@router.get("/api/tests/compare")
async def get_compare(base: str, new: str):
    try:
        return (await compare(base, new)).model_dump()
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ImportError as e:
        raise _unavailable(e)


# ---- speech recognition quality (docs/ASR_QUALITY.md) -----------------------------------------------
# Plain functions: FastAPI runs them in its thread pool, the store calls are blocking.

@router.post("/api/sessions/{session_id}/turns/{turn}/flag")
def post_flag(session_id: str, turn: int, body: FlagRequest):
    """A person marks a turn as misheard (or takes the mark back). With user audio stored for the
    turn this creates, updates or (misheard false) removes its recognition case."""
    try:
        flags, case = asr.flag_turn(deps.get_store(), session_id, turn, body.misheard, body.actual_text, body.note)
    except LookupError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "flags": flags, "case": case}


@router.get("/api/asr/cases")
def get_asr_cases(agent: Optional[str] = None):
    return {"cases": asr.list_cases(deps.get_store(), agent or None)}


@router.delete("/api/asr/cases/{case_id}")
def delete_asr_case(case_id: str):
    if not asr.delete_case(deps.get_store(), case_id):
        raise HTTPException(404, f"인식 사례를 찾을 수 없습니다: {case_id}")
    return {"ok": True}


@router.post("/api/asr/bench")
def post_asr_bench(body: Optional[BenchRequest] = None):
    """Starts a run in the background and returns {"run_id", "status": "running"} at once."""
    body = body or BenchRequest()
    try:
        run_id = asr.start_bench(deps.get_store(), body.engines, body.agent or None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"run_id": run_id, "status": "running"}


@router.get("/api/asr/bench")
def get_asr_benches():
    return {"runs": asr.list_benches(deps.get_store())}


@router.get("/api/asr/bench/{run_id}")
def get_asr_bench(run_id: str):
    document = asr.load_bench(deps.get_store(), run_id)
    if document is None:
        raise HTTPException(404, f"인식 다시 돌리기 실행을 찾을 수 없습니다: {run_id}")
    return document
