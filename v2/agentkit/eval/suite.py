"""A whole test suite, the comparison of two runs, and saving a conversation as a case (R-EVAL-6~8)."""
import asyncio
import inspect
import statistics
import time
import uuid

from agentkit.contracts import Comparison, TestCase, TestRun

from . import deps
from .analysis import load_session
from .simulate import run_case, run_persona, unresolved_markers

CONCURRENCY = 2     # the LLM server is shared
ACTIVE = set()      # run ids being worked on in this process


def new_run_id(agent_id, version):
    return f"{agent_id}.v{version}.{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"


def case_key(agent_id, case_id):
    return f"{agent_id}/{case_id}"


async def saved_cases(agent_id, store):
    """Cases stored with save_case for this agent -> list[TestCase]."""
    cases = []
    try:
        documents = await deps.list_documents(store, "case", agent_id + "/")
    except Exception as e:
        deps.log.warning("could not list saved cases: %s", e)
        return cases
    for document in documents:
        if document.get("agent_id") != agent_id or not isinstance(document.get("case"), dict):
            continue
        try:
            cases.append(TestCase.model_validate(document["case"]))
        except Exception as e:
            deps.log.warning("saved case ignored: %s", e)
    return cases


def aggregate(run: TestRun):
    """Fills pass_rate, safety_failures and the metrics of the run from its items."""
    items = run.items
    run.pass_rate = round(sum(1 for item in items if item.passed) / len(items), 4) if items else 0.0
    run.safety_failures = sum(1 for item in items for result in item.criteria
                              if result.safety and result.passed is False)
    metrics = {}

    def values(name):
        return [item.metrics[name] for item in items if name in item.metrics]

    if values("response_ms_median"):
        metrics["response_ms_median"] = round(statistics.median(values("response_ms_median")), 1)
    if values("response_ms_max"):
        metrics["response_ms_max"] = round(max(values("response_ms_max")), 1)
    for name in ("tool_failures", "interruptions"):
        if values(name):
            metrics[name] = float(sum(values(name)))
    if values("turns"):
        metrics["turns"] = round(statistics.mean(values("turns")), 2)       # per conversation
    if values("handoff"):
        metrics["handoff"] = round(statistics.mean(values("handoff")), 4)   # share of conversations
    if values("cart_changes"):
        metrics["cart_changes"] = round(statistics.mean(values("cart_changes")), 2)         # per conversation
    if values("payment_executed"):
        metrics["payment_executed"] = round(statistics.mean(values("payment_executed")), 4)  # share paid
    verdicts = {}
    for item in items:
        for result in item.criteria:
            if result.passed is not None and not result.id.startswith("_"):
                verdicts.setdefault(result.id, []).append(result.passed)
    for criterion_id, passed in verdicts.items():
        metrics[f"pass.{criterion_id}"] = round(sum(passed) / len(passed), 4)
    run.metrics = metrics
    return run


async def run_suite(agent_id, version=None, run_id=None, *, store=None, definition_store=None, llm=None,
                    driver_factory=None, concurrency=CONCURRENCY, include_saved_cases=True) -> TestRun:
    """Every persona and case of the definition (plus cases saved with save_case), two at a time.

    The TestRun is stored as documents(kind="test_run", key=run_id) at the start and after every
    item, so a reader sees it grow. The stored document carries three extra keys next to the
    TestRun fields: "status" ("running" | "finished" | "error"), "total" and "skipped".
    """
    store = deps.get_store(store)
    definition_store = deps.get_definition_store(definition_store)
    llm = deps.get_llm(llm)
    definition = definition_store.load(agent_id, version)
    version = definition.version if version is None else version
    run = TestRun(run_id=run_id or new_run_id(agent_id, version), agent_id=agent_id, version=version,
                  started_at=time.time())

    jobs = [(run_persona, persona) for persona in definition.tests.personas]
    jobs += [(run_case, case) for case in definition.tests.cases]
    skipped = []
    if include_saved_cases:
        known = {item.id for _, item in jobs}
        for case in await saved_cases(agent_id, store):
            if case.id in known:
                continue
            markers = unresolved_markers(case)
            if markers:
                skipped.append({"item_id": case.id, "reason": "facts로 채울 수 없는 표시: " + ", ".join(markers)})
                continue
            known.add(case.id)
            jobs.append((run_case, case))
    order = {item.id: index for index, (_, item) in enumerate(jobs)}

    async def save(status):
        document = run.model_dump()
        document.update(status=status, total=len(jobs), skipped=skipped)
        try:
            await deps.maybe_await(store.put_document("test_run", run.run_id, document))
        except Exception as e:
            deps.log.warning("could not store test run %s: %s", run.run_id, e)

    ACTIVE.add(run.run_id)
    await save("running")
    gate = asyncio.Semaphore(max(1, concurrency))

    async def one(function, item):
        async with gate:
            result = await function(agent_id, version, item, store=store, llm=llm,
                                    driver_factory=driver_factory, definition=definition)
        run.items.append(result)
        run.items.sort(key=lambda entry: order.get(entry.item_id, 0))
        aggregate(run)
        await save("running")

    try:
        await asyncio.gather(*(one(function, item) for function, item in jobs))
        run.finished_at = time.time()
        aggregate(run)
        await save("finished")
    except BaseException:
        await save("error")
        raise
    finally:
        ACTIVE.discard(run.run_id)
    try:
        definition_store.attach_test_run(agent_id, version, run.run_id)     # draft -> tested
    except Exception as e:      # not a draft (ReleaseError) or a store without this method
        deps.log.info("test run %s not attached to %s v%s: %s", run.run_id, agent_id, version, e)
    return run


async def load_run(run_id, store=None):
    """The stored run document (TestRun fields + status, total, skipped), or None."""
    return await deps.get_document(deps.get_store(store), "test_run", run_id)


async def list_runs(agent_id=None, store=None):
    """Stored runs, newest first, without the per-item details."""
    runs = await deps.list_documents(deps.get_store(store), "test_run")
    rows = []
    for run in runs:
        if agent_id and run.get("agent_id") != agent_id:
            continue
        row = {key: value for key, value in run.items() if key != "items"}
        items = run.get("items") or []
        row["done"] = len(items)
        row["passed"] = sum(1 for item in items if item.get("passed"))
        rows.append(row)
    rows.sort(key=lambda row: row.get("started_at") or 0, reverse=True)
    return rows


async def compare(base_run_id, new_run_id, *, store=None) -> Comparison:
    """Items by id: improved (failed -> passed), regressed, unchanged; and metrics present in both.
    Items that exist in only one of the runs are in none of the lists. LookupError: unknown run."""
    store = deps.get_store(store)
    runs = []
    for run_id in (base_run_id, new_run_id):
        document = await deps.get_document(store, "test_run", run_id)
        if document is None:
            raise LookupError(f"test run not found: {run_id}")
        runs.append(TestRun.model_validate(document))
    base, new = runs
    before = {item.item_id: item.passed for item in base.items}
    comparison = Comparison(base_run=base.run_id, new_run=new.run_id)
    for item in new.items:
        if item.item_id not in before:
            continue
        was, now = before[item.item_id], item.passed
        target = (comparison.improved if now and not was else comparison.regressed if was and not now
                  else comparison.unchanged_pass if now else comparison.unchanged_fail)
        target.append(item.item_id)
    for name, summary in (("pass_rate", lambda run: run.pass_rate),
                          ("safety_failures", lambda run: float(run.safety_failures))):
        comparison.metric_changes[name] = (summary(base), summary(new))
    for name in sorted(set(base.metrics) & set(new.metrics)):
        comparison.metric_changes[name] = (base.metrics[name], new.metrics[name])
    return comparison


async def save_case(session_id, case_id, expect=None, *, facts=None, description="", store=None) -> dict:
    """Keeps a conversation's user turns as a replayable case: documents(kind="case",
    key="<agent_id>/<case_id>") = {agent_id, version, session_id, created_at, case: TestCase}.

    The stored text is masked, so values such as the order number are markers ("[주문번호]");
    `facts` (e.g. {"order_id": "A1001", "phone_last4": "1234"}) puts them back at replay time.
    """
    store = deps.get_store(store)
    session = await load_session(session_id, store)
    turns = sorted(session["turns"], key=lambda turn: turn.get("turn", 0))
    user_turns = [turn["user_text"] for turn in turns if (turn.get("user_text") or "").strip()]
    if not user_turns:
        raise ValueError(f"session {session_id} has no user turns")
    case = TestCase(id=case_id, description=description, user_turns=user_turns, facts=facts or {},
                    expect=expect or {})
    meta = session["meta"]
    document = {"agent_id": meta["agent_id"], "version": meta.get("version"), "session_id": session_id,
                "created_at": time.time(), "case": case.model_dump(),
                "unresolved_markers": unresolved_markers(case)}
    # The user turns are already masked. `facts` are test values the operator typed in for replay,
    # so the document must be stored as it is: a masking store would turn them into markers.
    key = case_key(meta["agent_id"], case_id)
    try:
        unmasked = "mask" in inspect.signature(store.put_document).parameters
    except (TypeError, ValueError):
        unmasked = False
    await deps.maybe_await(store.put_document("case", key, document, mask=False) if unmasked
                           else store.put_document("case", key, document))
    return document
