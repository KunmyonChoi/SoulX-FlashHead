"""The builder's way into the evaluation module: start a suite in the background, read a run,
compare two runs. Tests replace this object with a fake that has the same four methods."""
from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger("agentkit.builder")


class EvalBridge:
    def __init__(self, definition_store, trace_store=None):
        self.definition_store = definition_store
        self.trace_store = trace_store          # None: agentkit.eval's own default
        self._tasks: dict[str, asyncio.Task] = {}

    def _store(self):
        from agentkit.eval import deps
        return deps.get_store(self.trace_store)

    async def start(self, agent_id: str, version: int) -> dict:
        """Starts run_suite as a task of this process and returns {"run_id", "total"} at once."""
        from agentkit.contracts import TestRun
        from agentkit.eval import deps, run_suite
        from agentkit.eval.suite import ACTIVE, new_run_id

        store = self._store()
        definition = self.definition_store.load(agent_id, version)
        total = len(definition.tests.personas) + len(definition.tests.cases)
        run_id = new_run_id(agent_id, version)
        first = TestRun(run_id=run_id, agent_id=agent_id, version=version, started_at=time.time()).model_dump()
        first.update(status="running", total=total, skipped=[])
        await deps.maybe_await(store.put_document("test_run", run_id, first))

        async def work():
            try:
                await run_suite(agent_id, version, run_id, store=store, definition_store=self.definition_store)
            except Exception as error:
                log.exception("test run %s failed", run_id)
                document = await deps.get_document(store, "test_run", run_id) or first
                document.update(status="error", error=repr(error)[:400], finished_at=time.time())
                await deps.maybe_await(store.put_document("test_run", run_id, document))
            finally:
                self._tasks.pop(run_id, None)
                ACTIVE.discard(run_id)

        ACTIVE.add(run_id)      # so GET /api/tests/runs/{run_id} shows it as running, not interrupted
        self._tasks[run_id] = asyncio.create_task(work())
        return {"run_id": run_id, "total": total}

    async def load(self, run_id: str):
        from agentkit.eval import load_run
        from agentkit.eval.suite import ACTIVE
        document = await load_run(run_id, self._store())
        if document is not None and document.get("status") == "running" and run_id not in ACTIVE:
            document["status"] = "interrupted"
        return document

    async def compare(self, base_run_id: str, new_run_id: str):
        from agentkit.eval import compare
        return await compare(base_run_id, new_run_id, store=self._store())

    async def list_runs(self, agent_id: str) -> list[dict]:
        """Newest first, without items."""
        from agentkit.eval import list_runs
        return await list_runs(agent_id, self._store())

    async def wait(self, run_id: str, timeout: float | None = None):
        """For tests and scripts: until the run started here is over."""
        task = self._tasks.get(run_id)
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), timeout)
