"""
The four ways a tool call reaches the outside (contracts.AdapterSpec.kind).

Every adapter has the same two methods:

    await adapter.call(arguments, ctx)    -> Reply
    await adapter.preview(arguments, ctx) -> Reply | None    (write tools: what would happen)

and signals the two kinds of failure differently:

    Reply(ok=False, error_type=...)   the system answered and refused: final, never retried
    raise AdapterUnavailable(...)     the system was not reached: the gateway may try again (read tools)

Wire format shared by "http" and "replay", and accepted from "python" callables:
    {"ok": bool, "text": str, "data": any, "error_type": str | null, "executed": bool (optional),
     "repeatable": bool (optional, write tools: the same request may run again)}
A failure may carry data["display_notice"] (a sentence or {"level", "text"}): the gateway shows it
on the customer's screen as a notice (docs/DISPLAY.md).
"""
from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx

from .. import config
from ..contracts import ToolContract, ToolResult

ERROR_TYPES = ("not_found", "permission_denied", "invalid_input", "precondition_failed",
               "not_confirmed", "system_error", "timeout")


class AdapterError(Exception):
    """The adapter cannot be built (bad target). Raised when the gateway is built."""


class AdapterUnavailable(Exception):
    """The outside system was not reached (connection refused, dropped, 502/503/504).

    uncertain: the request may have arrived before the connection broke. For a read tool this
    makes no difference; a write tool with an uncertain outcome is not offered again."""

    def __init__(self, message="", uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


class ToolFailure(Exception):
    """For python tool bodies: raise this to refuse with a type and a sentence for the model."""

    def __init__(self, error_type, text, data=None):
        super().__init__(text)
        self.error_type, self.text, self.data = error_type, text, data


@dataclass
class Reply:
    ok: bool
    text: str = ""
    data: Any = None
    error_type: Optional[str] = None
    executed: Optional[bool] = None     # write tools: False when the system says it changed nothing
    items: Optional[int] = None
    # write tools: the system says the same request may be made again and changes something each
    # time ("one more americano"). The gateway then does not treat a repeat as a duplicate.
    repeatable: bool = False


def to_reply(value):
    """Whatever an adapter body produced -> Reply."""
    if isinstance(value, Reply):
        return value
    if isinstance(value, ToolResult):
        return Reply(value.ok, value.text, value.data, value.error_type, value.executed if value.ok else None,
                     value.items or None)
    if isinstance(value, str):
        return Reply(True, value)
    if isinstance(value, dict) and isinstance(value.get("ok"), bool):
        error_type = value.get("error_type")
        if not value["ok"] and error_type not in ERROR_TYPES:
            error_type = "system_error"
        return Reply(value["ok"], str(value.get("text") or ""), value.get("data"),
                     None if value["ok"] else error_type,
                     value.get("executed") if isinstance(value.get("executed"), bool) else None,
                     value.get("items") if isinstance(value.get("items"), int) else None,
                     value.get("repeatable") is True)
    if value is None:
        return Reply(True, "")
    return Reply(True, "", value)       # bare data


class Adapter:
    kind = ""

    def __init__(self, contract: ToolContract):
        self.contract = contract
        self.target = contract.adapter.target
        self.options = dict(contract.adapter.options or {})

    async def call(self, arguments, ctx) -> Reply:
        raise NotImplementedError

    async def preview(self, arguments, ctx) -> Optional[Reply]:
        return None

    def end_session(self, session_id):
        pass


# ---- python ---------------------------------------------------------------------------------


def _load_callable(target, what):
    module_name, _, attr = str(target).partition(":")
    if not module_name or not attr:
        raise AdapterError(f"{what}: \"패키지.모듈:함수\" 형식이어야 합니다 (지금: {target!r})")
    try:
        module = importlib.import_module(module_name)
    except Exception as e:
        raise AdapterError(f"{what}: 모듈 {module_name}을 불러오지 못했습니다 ({type(e).__name__}: {e})")
    fn = module
    for part in attr.split("."):
        fn = getattr(fn, part, None)
    if not callable(fn):
        raise AdapterError(f"{what}: {module_name}에 호출할 수 있는 {attr}이 없습니다")
    return fn


class PythonAdapter(Adapter):
    """target "package.module:callable", called as `await fn(arguments, ctx)`.
    options.preview: another "module:callable" with the same signature, for write tools."""
    kind = "python"

    def __init__(self, contract):
        super().__init__(contract)
        self.fn = _load_callable(self.target, "adapter.target")
        self.preview_fn = (_load_callable(self.options["preview"], "adapter.options.preview")
                           if self.options.get("preview") else None)

    async def _run(self, fn, arguments, ctx):
        try:
            value = fn(arguments, ctx)
            if hasattr(value, "__await__"):
                value = await value
        except ToolFailure as e:
            return Reply(False, e.text, e.data, e.error_type if e.error_type in ERROR_TYPES else "system_error")
        return to_reply(value)

    async def call(self, arguments, ctx):
        return await self._run(self.fn, arguments, ctx)

    async def preview(self, arguments, ctx):
        return await self._run(self.preview_fn, arguments, ctx) if self.preview_fn else None


# ---- dummy ----------------------------------------------------------------------------------


def _dummy_systems():
    from ..dummy.cafe import get_cafe
    from ..dummy.shop import get_shop
    return {"shop": get_shop, "cafe": get_cafe}


class DummyAdapter(Adapter):
    """target "system:method", e.g. "shop:get_order", "cafe:add_to_cart". options are handed to the
    dummy system (delay_s, fail, fail_times, ... see agentkit/dummy/shop.py, cafe.py)."""
    kind = "dummy"

    def __init__(self, contract, systems=None):
        super().__init__(contract)
        system_name, _, self.method = self.target.partition(":")
        factories = dict(_dummy_systems())
        factories.update({name: (lambda s=s: s) for name, s in (systems or {}).items()})
        if system_name not in factories:
            raise AdapterError(f"adapter.target: 더미 시스템 '{system_name}'이 없습니다 (있는 것: {', '.join(factories)})")
        self.system = factories[system_name]()
        if self.method not in getattr(self.system, "methods", ()):
            raise AdapterError(f"adapter.target: 더미 시스템 '{system_name}'에 '{self.method}' 기능이 없습니다")

    async def call(self, arguments, ctx):
        from ..dummy.shop import ShopUnavailable
        try:
            return to_reply(await self.system.call(self.method, arguments, ctx, self.options))
        except ShopUnavailable as e:
            raise AdapterUnavailable(str(e))

    async def preview(self, arguments, ctx):
        from ..dummy.shop import ShopUnavailable
        try:
            out = await self.system.preview(self.method, arguments, ctx, self.options)
        except ShopUnavailable as e:
            raise AdapterUnavailable(str(e))
        return to_reply(out) if out is not None else None

    def end_session(self, session_id):
        self.system.end_session(session_id)


# ---- http -----------------------------------------------------------------------------------


class HttpAdapter(Adapter):
    """target is a URL. POST {"arguments": ..., "context": {...}} -> the wire format above.

    options.preview_url: where to POST the same body for a write tool's preview.
    options.headers:     extra request headers.
    options.allow_in_test: a write tool is not sent to this URL from a test or simulated session
                         (ToolContext.test) unless this is true: test sessions must not change
                         real systems.
    """
    kind = "http"

    def __init__(self, contract):
        super().__init__(contract)
        if not str(self.target).startswith(("http://", "https://")):
            raise AdapterError(f"adapter.target: http:// 또는 https:// 주소여야 합니다 (지금: {self.target!r})")

    async def _post(self, url, arguments, ctx):
        body = {"arguments": arguments, "context": ctx.model_dump() if hasattr(ctx, "model_dump") else dict(ctx or {})}
        timeout = httpx.Timeout(max(self.contract.timeout_s, 0.1), connect=min(2.0, max(self.contract.timeout_s, 0.1)))
        try:
            async with httpx.AsyncClient(timeout=timeout, headers=self.options.get("headers")) as client:
                response = await client.post(url, json=body)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise AdapterUnavailable(f"{type(e).__name__}: {e}")
        except httpx.TimeoutException:
            raise                           # the gateway reports this as "timeout"
        except httpx.HTTPError as e:        # broke after connecting: the request may have arrived
            raise AdapterUnavailable(f"{type(e).__name__}: {e}", uncertain=True)
        if response.status_code in (502, 503, 504):
            raise AdapterUnavailable(f"HTTP {response.status_code}", uncertain=response.status_code != 503)
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("ok"), bool):
            return to_reply(payload)
        if response.status_code >= 400:
            kind = {404: "not_found", 401: "permission_denied", 403: "permission_denied",
                    400: "invalid_input", 422: "invalid_input"}.get(response.status_code, "system_error")
            return Reply(False, "", None, kind)
        return Reply(True, "", payload)

    async def call(self, arguments, ctx):
        if self.contract.effect == "write" and getattr(ctx, "test", False) and not self.options.get("allow_in_test"):
            return Reply(False, "시험 대화에서는 실제 시스템을 바꾸지 않습니다. 이 요청은 실행되지 않았습니다.",
                         None, "system_error")
        return await self._post(self.target, arguments, ctx)

    async def preview(self, arguments, ctx):
        url = self.options.get("preview_url")
        return await self._post(url, arguments, ctx) if url else None


# ---- replay ---------------------------------------------------------------------------------


def _canonical(arguments):
    return json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False, default=str)


def resolve_path(target):
    """Absolute, or relative to the tools folder, or relative to v2/."""
    path = Path(str(target)).expanduser()
    if path.is_absolute():
        return path
    for base in (config.TOOLS_DIR, config.V2_ROOT):
        if (Path(base) / path).exists():
            return Path(base) / path
    return Path(config.TOOLS_DIR) / path


def record_call(path, tool, arguments, result, phase="call"):
    """Appends one recorded (arguments -> result) pair to a replay file.
    result: ToolResult, Reply or wire dict. phase: "call" or "preview"."""
    reply = to_reply(result)
    line = {"tool": tool, "phase": phase, "arguments": arguments,
            "result": {"ok": reply.ok, "text": reply.text, "data": reply.data, "error_type": reply.error_type}}
    if reply.executed is not None:
        line["result"]["executed"] = reply.executed
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")


class ReplayAdapter(Adapter):
    """target is a JSONL file; each line {"tool"?, "phase"?, "arguments": {...}, "result": {...}}.

    A call is answered by the recorded result whose arguments are equal (all of them, or only
    options.match_fields when given). Lines naming another tool are skipped, so one file can
    hold a whole conversation. When several lines match, they are given out in order and the
    last one repeats. No match -> system_error.
    """
    kind = "replay"

    def __init__(self, contract):
        super().__init__(contract)
        self.path = resolve_path(self.target)
        if not self.path.exists():
            raise AdapterError(f"adapter.target: 기록 파일 {self.path}이 없습니다")
        self.fields = self.options.get("match_fields")
        self.recorded = {"call": {}, "preview": {}}
        with open(self.path, encoding="utf-8") as f:
            for number, raw in enumerate(f, 1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    line = json.loads(raw)
                    result = line["result"]
                    arguments = line.get("arguments") or {}
                except (ValueError, KeyError, TypeError) as e:
                    raise AdapterError(f"adapter.target: {self.path} {number}번째 줄을 읽지 못했습니다 ({e})")
                if line.get("tool") not in (None, contract.name):
                    continue
                phase = "preview" if line.get("phase") == "preview" else "call"
                self.recorded[phase].setdefault(self._key(arguments), []).append(result)
        self._used = {}

    def _key(self, arguments):
        if self.fields:
            arguments = {name: (arguments or {}).get(name) for name in self.fields}
        return _canonical(arguments)

    def _next(self, phase, arguments, ctx):
        key = self._key(arguments)
        results = self.recorded[phase].get(key)
        if not results:
            return None
        slot = (getattr(ctx, "session_id", ""), phase, key)
        index = min(self._used.get(slot, 0), len(results) - 1)
        self._used[slot] = index + 1
        return to_reply(results[index])

    async def call(self, arguments, ctx):
        reply = self._next("call", arguments, ctx)
        if reply is None:
            return Reply(False, "이 요청에 대해 기록해 둔 응답이 없습니다. 지금은 확인할 수 없다고 안내하세요.",
                         None, "system_error")
        return reply

    async def preview(self, arguments, ctx):
        return self._next("preview", arguments, ctx)

    def end_session(self, session_id):
        for slot in [slot for slot in self._used if slot[0] == session_id]:
            del self._used[slot]


KINDS = {"python": PythonAdapter, "dummy": DummyAdapter, "http": HttpAdapter, "replay": ReplayAdapter}


def build_adapter(contract: ToolContract) -> Adapter:
    return KINDS[contract.adapter.kind](contract)
