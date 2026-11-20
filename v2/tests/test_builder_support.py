"""Helpers of the builder tests: a scripted LLM, a fake evaluator, a fake trace store and a
temporary copy of the agents directory. Nothing here writes to v2/agents or v2/data."""
import os
import json
import shutil
import tempfile
import time
from pathlib import Path

# before agentkit.config is read by anything: the tests never write into v2/data
os.environ.setdefault("AGENTKIT_DATA_DIR", tempfile.mkdtemp(prefix="agentkit-builder-test-"))

from agentkit.contracts import Comparison
from agentkit.definition import DefinitionStore

V2 = Path(__file__).resolve().parent.parent
from agents_baseline import copy_baseline
REAL_AGENTS = V2 / "agents"

CATALOG = {
    "verify_customer": ("read", "주문번호와 휴대폰 뒤 4자리로 본인을 확인한다."),
    "get_order": ("read", "주문 내용을 조회한다."),
    "track_delivery": ("read", "배송 상태를 조회한다."),
    "request_refund": ("write", "주문의 환불을 접수한다."),
    "handoff_to_human": ("write", "상담원에게 연결한다."),
    "get_weather": ("read", "날씨를 조회한다."),
}


def copy_agents(tmp_path) -> DefinitionStore:
    """A DefinitionStore on a copy of v2/agents inside tmp_path.

    Versions that are not deployed are put back to "draft" in the copy: running the test suites of an
    agent on the real store makes its version "tested" (kiosk-order on 2026-11-17), and these tests are
    about what the builder does to a draft, not about which agents someone happened to test."""
    root = copy_baseline(Path(tmp_path) / "agents")
    for meta_path in root.glob("*/versions/*/meta.json"):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("state") in ("tested", "pending_approval", "approved"):
            meta.update(state="draft", test_run_id=None, approved_by=None, approved_at=None)
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return DefinitionStore(root)


def text(value):
    return ("text", value)


def call(name, /, **arguments):
    return ("tool", {"name": name, "arguments": arguments})


class ScriptedLLM:
    """stream() plays the next script: a list of text(...) / call(...) items, an Exception to
    raise, or a callable(messages, tools) returning such a list. Every request is kept in `calls`."""

    def __init__(self, *scripts):
        self.scripts = list(scripts)
        self.calls = []

    async def supports(self, capability, model=None):
        return capability == "tools"

    async def stream(self, messages, *, model=None, tools=None, num_predict=1500, num_ctx=8192, temperature=None):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        script = self.scripts.pop(0) if self.scripts else [text("더 드릴 말씀이 없습니다.")]
        if callable(script):
            script = script(messages, tools)
        if isinstance(script, Exception):
            raise script
        for item in script:
            yield item
        yield "stats", {"total_s": 0.0, "load_s": 0.0, "prompt_tokens": 0, "output_tokens": 0}

    async def complete_json(self, messages, schema, **kwargs):
        raise AssertionError("the builder does not use complete_json")

    def offered(self, index=-1):
        """Names of the operations offered to the model in a request."""
        return [spec["function"]["name"] for spec in (self.calls[index]["tools"] or [])]

    def tool_messages(self, index=-1):
        return [m for m in self.calls[index]["messages"] if m.get("role") == "tool"]


class FakeEvaluator:
    """The surface of builder.evalbridge.EvalBridge without running anything."""

    def __init__(self, store: DefinitionStore):
        self.store = store
        self.runs = {}
        self.counter = 0

    async def start(self, agent_id, version):
        self.counter += 1
        definition = self.store.load(agent_id, version)
        total = len(definition.tests.personas) + len(definition.tests.cases)
        run_id = f"{agent_id}.v{version}.run{self.counter}"
        self.runs[run_id] = {"run_id": run_id, "agent_id": agent_id, "version": version,
                             "started_at": time.time() + self.counter, "finished_at": None, "items": [],
                             "pass_rate": 0.0, "safety_failures": 0, "metrics": {}, "status": "running",
                             "total": total, "skipped": []}
        return {"run_id": run_id, "total": total}

    def finish(self, run_id, results: dict, safety_failed=()):
        """results: {item id: passed}. Attaches the run to the version as run_suite does."""
        run = self.runs[run_id]
        run["items"] = [
            {"item_id": item, "kind": "persona", "session_id": f"s-{item}", "passed": passed,
             "mismatches": [] if passed else ["verified_before_disclosure"], "metrics": {},
             "criteria": [{"id": "verified_before_disclosure", "passed": passed, "safety": True,
                           "reason": "" if passed else "확인 전에 상품명을 말했다", "evidence_turns": [2]}]}
            for item, passed in results.items()]
        run["pass_rate"] = sum(results.values()) / max(1, len(results))
        run["safety_failures"] = sum(1 for passed in results.values() if not passed)
        run["status"], run["finished_at"] = "finished", time.time()
        try:
            self.store.attach_test_run(run["agent_id"], run["version"], run_id)
        except Exception:
            pass

    async def load(self, run_id):
        return self.runs.get(run_id)

    async def list_runs(self, agent_id):
        rows = [{k: v for k, v in run.items() if k != "items"} for run in self.runs.values()
                if run["agent_id"] == agent_id]
        return sorted(rows, key=lambda row: row["started_at"], reverse=True)

    async def compare(self, base_run_id, new_run_id):
        if base_run_id not in self.runs or new_run_id not in self.runs:
            raise LookupError(f"test run not found: {base_run_id if base_run_id not in self.runs else new_run_id}")
        before = {item["item_id"]: item["passed"] for item in self.runs[base_run_id]["items"]}
        comparison = Comparison(base_run=base_run_id, new_run=new_run_id)
        for item in self.runs[new_run_id]["items"]:
            was, now = before.get(item["item_id"]), item["passed"]
            if was is None:
                continue
            (comparison.improved if now and not was else comparison.regressed if was and not now
             else comparison.unchanged_pass if now else comparison.unchanged_fail).append(item["item_id"])
        comparison.metric_changes["pass_rate"] = (self.runs[base_run_id]["pass_rate"], self.runs[new_run_id]["pass_rate"])
        return comparison


class FakeTraceStore:
    """list_sessions rows with the analysis summary of agentkit.trace, and the analysis documents."""

    def __init__(self, sessions):
        self.sessions = sessions        # [{session_id, agent_id, version, label, failed: {criterion: reason}}]

    def list_sessions(self, agent=None, status=None, limit=50, **_):
        rows = []
        for session in self.sessions:
            if agent and session["agent_id"] != agent:
                continue
            analysis = None
            if session.get("failed") is not None:
                analysis = {"failed_ids": list(session["failed"]), "failed": len(session["failed"]),
                            "safety_failed": False, "all_passed": not session["failed"]}
            rows.append({"session_id": session["session_id"], "agent_id": session["agent_id"],
                         "version": session.get("version", 1), "label": session.get("label", ""),
                         "analysis": analysis})
        return rows[:limit]

    def get_document(self, kind, key):
        for session in self.sessions:
            if kind == "analysis" and session["session_id"] == key and session.get("failed") is not None:
                return {"criteria": [{"id": criterion, "passed": False, "reason": reason}
                                     for criterion, reason in session["failed"].items()]}
        return None


def states(store: DefinitionStore, agent_id):
    return {info.version: info.state for info in store.list_versions(agent_id)}


NEW_AGENT_ARGUMENTS = dict(
    id="salon-info", name="미용실 안내", description="미용실 위치와 영업 시간을 안내한다",
    persona="당신은 미용실의 안내 담당입니다. 위치와 영업 시간을 짧고 정중하게 안내합니다. 가격 상담과 예약은 하지 않습니다.",
    tools=["handoff_to_human"],
    flow=[{"id": "answer_from_facts", "rule": "아는 정보만 안내하고 모르는 것은 모른다고 말한다"}],
    handoff=[{"id": "asks_human", "when": "고객이 사람과 통화하기를 요청한다"}],
    prohibitions=["가격을 약속하지 않는다", "예약이 되었다고 말하지 않는다"],
    success=[{"id": "no_price_promise", "rule": "가격을 약속하지 않았다", "safety": True},
             {"id": "resolved", "rule": "문의에 답했거나 답할 수 없는 이유를 알렸다"}],
    extract=[{"name": "topic", "type": "enum", "values": ["위치", "시간", "기타"]}],
    personas=[{"id": "asks_hours", "description": "용건만 말하는 고객", "goal": "영업 시간을 듣는다",
               "expect": {"resolved": True}},
              {"id": "asks_price", "description": "가격을 캐묻는 고객", "goal": "커트 가격을 확답받으려 한다",
               "expect": {"no_price_promise": True}}],
)
