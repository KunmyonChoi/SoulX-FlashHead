"""REST for definitions and releases (ARCHITECTURE.md section 6, rows owned by WP1).

    GET  /api/agents                          [{id, name, description, ..., deployment, versions}]
    GET  /api/agents/{id}                     {id, name, description, versions, deployment}
    GET  /api/agents/{id}/versions/{n}        the AgentDefinition as JSON
    GET  /api/agents/{id}/diff?a=&b=          {agent_id, a, b, changes, changed_fields, safety_change}
    POST /api/releases/{id}/{n}/submit        {ok, version: VersionInfo, deployment}
    POST /api/releases/{id}/{n}/approve       body {"approver": "..."}
    POST /api/releases/{id}/{n}/deploy        body {"percent": 100}
    POST /api/releases/{id}/rollback
A refused release step answers 409 {"detail": reason}; an unknown agent or version 404;
a definition that cannot be read 422 {"detail": message, "problems": [{file, field, message}]}.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Query
from fastapi.responses import JSONResponse

from agentkit.contracts import ReleaseError

from .diffing import safety_changes
from .errors import DefinitionError, DefinitionNotFound
from .store import DefinitionStore

router = APIRouter()


def get_store() -> DefinitionStore:
    """A store on the configured directory (cheap: the store holds no state)."""
    return DefinitionStore()


def _refusal(error: Exception) -> JSONResponse:
    if isinstance(error, ReleaseError):
        return JSONResponse({"detail": str(error)}, status_code=409)
    if isinstance(error, DefinitionNotFound):
        return JSONResponse({"detail": str(error)}, status_code=404)
    if isinstance(error, DefinitionError):
        problems = [{"file": f, "field": k, "message": m} for f, k, m in error.problems]
        return JSONResponse({"detail": str(error), "problems": problems}, status_code=422)
    raise error


def _guarded(fn):
    try:
        return fn()
    except (ReleaseError, DefinitionError) as error:
        return _refusal(error)


def _summary(store: DefinitionStore, agent_id: str) -> dict[str, Any]:
    versions = store.list_versions(agent_id)
    deployment = store.deployment(agent_id)
    shown = deployment.stable or (versions[-1].version if versions else None)
    out: dict[str, Any] = {
        "id": agent_id, "name": agent_id, "description": "", "language": None,
        "avatar": False, "tools": [], "shown_version": shown,
        "stable": deployment.stable, "canary": deployment.canary,
        "canary_percent": deployment.canary_percent,
        "latest_version": versions[-1].version if versions else None,
        "deployment": deployment.model_dump(mode="json"),
        "versions": [v.model_dump(mode="json") for v in versions],
    }
    if shown is not None:
        try:
            d = store.load(agent_id, shown)
            out.update(name=d.name, description=d.description, language=d.language,
                       avatar=d.avatar.enabled, tools=d.tools, tool_packs=d.tool_packs)
        except DefinitionError as error:
            out["error"] = str(error)
    return out


@router.get("/api/agents")
def list_agents():
    store = get_store()
    out = []
    for agent_id in store.list_agents():
        try:
            out.append(_summary(store, agent_id))
        except DefinitionError as error:
            out.append({"id": agent_id, "name": agent_id, "error": str(error), "versions": [],
                        "deployment": None, "stable": None})
    return out


@router.get("/api/agents/{agent_id}")
def get_agent(agent_id: str):
    return _guarded(lambda: _summary(get_store(), agent_id))


@router.get("/api/agents/{agent_id}/versions/{version}")
def get_version(agent_id: str, version: int):
    return _guarded(lambda: get_store().load(agent_id, version).model_dump(mode="json"))


@router.get("/api/agents/{agent_id}/diff")
def get_diff(agent_id: str, a: int = Query(...), b: int = Query(...)):
    def run():
        store = get_store()
        before, after = store.load(agent_id, a), store.load(agent_id, b)
        changes = store.diff(agent_id, a, b)
        touched = safety_changes(before, after)
        return {"agent_id": agent_id, "a": a, "b": b, "changes": changes,
                "changed_fields": list(changes), "safety_change": bool(touched),
                "safety_fields": touched}
    return _guarded(run)


def _release_result(store: DefinitionStore, agent_id: str, version: Optional[int]) -> dict[str, Any]:
    return {
        "ok": True,
        "version": store.version_info(agent_id, version).model_dump(mode="json") if version is not None else None,
        "deployment": store.deployment(agent_id).model_dump(mode="json"),
        "versions": [v.model_dump(mode="json") for v in store.list_versions(agent_id)],
    }


@router.post("/api/releases/{agent_id}/rollback")
def rollback(agent_id: str):
    def run():
        store = get_store()
        deployment = store.rollback(agent_id)
        return _release_result(store, agent_id, deployment.stable)
    return _guarded(run)


@router.post("/api/releases/{agent_id}/{version}/submit")
def submit(agent_id: str, version: int):
    def run():
        store = get_store()
        store.submit(agent_id, version)
        return _release_result(store, agent_id, version)
    return _guarded(run)


@router.post("/api/releases/{agent_id}/{version}/approve")
def approve(agent_id: str, version: int, body: Optional[dict] = Body(default=None)):
    def run():
        store = get_store()
        approver = (body or {}).get("approver") or ""
        store.approve(agent_id, version, approver if isinstance(approver, str) else "")
        return _release_result(store, agent_id, version)
    return _guarded(run)


@router.post("/api/releases/{agent_id}/{version}/deploy")
def deploy(agent_id: str, version: int, body: Optional[dict] = Body(default=None)):
    def run():
        store = get_store()
        percent = (body or {}).get("percent")
        store.deploy(agent_id, version, 100 if percent is None else percent)
        return _release_result(store, agent_id, version)
    return _guarded(run)
