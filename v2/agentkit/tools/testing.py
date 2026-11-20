"""Helpers for tests that need a gateway without the other modules: a recorder that keeps what it
is given, a context factory, and an adapter that follows a script."""
from __future__ import annotations

import asyncio

from ..contracts import AdapterSpec, ToolContext, ToolContract
from .adapters import Adapter, Reply, to_reply


class FakeRecorder:
    """Satisfies contracts.Recorder; keeps spans as (span, pii_paths)."""

    def __init__(self, fail=False):
        self.spans, self.fail = [], fail

    def start_session(self, meta):
        pass

    def span(self, span, pii_paths=None):
        if self.fail:
            raise RuntimeError("recorder is broken")
        self.spans.append((span, list(pii_paths or [])))

    def turn(self, turn):
        pass

    def audio(self, session_id, turn, role, pcm16):
        pass

    def end_session(self, session_id, status):
        pass


def make_ctx(session_id="s1", turn=1, text="", speculative=False, test=True, language="ko",
             agent_id="shop-support", version=1):
    return ToolContext(session_id=session_id, agent_id=agent_id, version=version, turn=turn, language=language,
                       test=test, last_user_text=text, speculative=speculative)


def make_contract(name, effect="read", **fields):
    fields.setdefault("description", f"{name} (시험용)")
    fields.setdefault("input_schema", {"type": "object", "properties": {"key": {"type": "string"}}})
    fields.setdefault("adapter", AdapterSpec(kind="python", target="unused:unused"))
    return ToolContract(name=name, effect=effect, **fields)


class ScriptedAdapter(Adapter):
    """Answers from a list, one entry per call: a Reply / wire dict / str, an Exception to raise,
    or a number of seconds to hang before answering "ok". The last entry repeats.
    `calls` and `previews` keep the arguments it was given."""
    kind = "scripted"

    def __init__(self, contract, script=("ok",), preview=None):
        super().__init__(contract)
        self.script, self.preview_script = list(script), preview
        self.calls, self.previews = [], []

    async def call(self, arguments, ctx):
        self.calls.append(dict(arguments))
        step = self.script[min(len(self.calls), len(self.script)) - 1]
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, (int, float)) and not isinstance(step, bool):
            await asyncio.sleep(step)
            return Reply(True, "ok")
        return to_reply(step)

    async def preview(self, arguments, ctx):
        self.previews.append(dict(arguments))
        if self.preview_script is None:
            return None
        if isinstance(self.preview_script, BaseException):
            raise self.preview_script
        return to_reply(self.preview_script)


class Packed:
    """A gateway as an agent with these tool packs uses it: bare names mean the tools of the packs."""

    def __init__(self, gateway, packs):
        self.gateway, self.packs = gateway, list(packs)

    def __getattr__(self, name):
        return getattr(self.gateway, name)

    def contract(self, name):
        return self.gateway.contract(self.gateway.resolve(name, self.packs))

    def specs(self, tool_names):
        return self.gateway.specs([self.gateway.resolve(name, self.packs) for name in tool_names])

    async def call(self, ctx, name, arguments):
        return await self.gateway.call(ctx, self.gateway.resolve(name, self.packs), arguments)
