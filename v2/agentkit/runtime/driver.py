"""Talk to an agent in text, in process (contracts.ConversationDriver). Tests and simulated customers use it."""
import time

from ..contracts import AgentTurn


class TextConversation:
    def __init__(self, engine):
        self.engine = engine
        self.session_id = engine.session_id
        self.events = []          # the ReplyEvents of the last turn, for tests that look closer

    @classmethod
    async def open(cls, agent_id, version=None, test=True, simulated=True, label="", **options):
        from . import create_engine
        return cls(await create_engine(agent_id, version=version, channel="text", test=test,
                                       simulated=simulated, label=label, **options))

    @property
    def handed_off(self):
        return self.engine.handed_off

    async def say(self, text):
        return await self._turn(self.engine.reply(text, t_user_end=time.time()))

    async def opening(self):
        """Runs the agent's opening once: its words and the screens it put up. An agent without an
        opening (or a second call) gives an empty turn."""
        turn = await self._turn(self.engine.open())
        if getattr(getattr(self.engine.definition, "opening", None), "brief", "") and hasattr(self.engine, "brief"):
            # the briefing belongs to the agent's first words
            brief = await self._turn(self.engine.brief())
            turn = AgentTurn(text=" ".join(t for t in (turn.text, brief.text) if t), displays=turn.displays + brief.displays,
                             tool_calls=turn.tool_calls + brief.tool_calls, handoff=turn.handoff or brief.handoff,
                             ms=turn.ms + brief.ms)
        return turn

    async def act(self, name, arguments=None):
        """A touch on the screen (definition.actions). A refused touch gives an empty turn; the reason
        is in the "done" event's data["error"] (self.events[-1])."""
        return await self._turn(self.engine.act(name, arguments or {}))

    async def _turn(self, events):
        started = time.perf_counter()
        self.events = []
        reply, tool_calls, handoff, displays = "", [], False, []
        async for event in events:
            self.events.append(event)
            if event.type == "filler":
                self.engine.mark_output("filler", time.time())
            elif event.type in ("delta", "piece"):
                self.engine.mark_output("reply", time.time())
            elif event.type == "tool" and event.data.get("state") == "done":
                tool_calls.append({key: event.data.get(key) for key in (
                    "name", "arguments", "ok", "executed", "error_type", "needs_confirmation", "effect")})
            elif event.type == "display":
                displays.append(dict(event.data))
            elif event.type == "handoff":
                handoff = True
            elif event.type == "done":
                reply = event.text
        return AgentTurn(text=reply, displays=displays, tool_calls=tool_calls, handoff=handoff or self.engine.handed_off,
                         ms=(time.perf_counter() - started) * 1000)

    async def close(self, status="completed"):
        await self.engine.close(status)
