"""The conversation runtime: ReplyEngine (engine.py) and the in-process text driver (driver.py).

    from agentkit.runtime import create_engine, TextConversation
    engine = await create_engine("shop-support")
    async for event in engine.reply("환불하고 싶어요"): ...

voice.py, audio.py and avatar.py in this package belong to the voice work package and are not
imported here, so the text runtime works without them.
"""
import logging
import uuid

from .engine import Engine, NullRecorder
from .knowledge import build_knowledge

logger = logging.getLogger("agentkit.runtime")

_shared = {}   # one definition store, tool gateway and recorder per process


def _recorder():
    if "recorder" not in _shared:
        try:
            from agentkit.trace import get_recorder
            _shared["recorder"] = get_recorder()
        except ImportError as e:
            logger.warning(f"No trace module ({e}); conversations are not recorded")
            _shared["recorder"] = NullRecorder()
    return _shared["recorder"]


def _store():
    if "store" not in _shared:
        try:
            from agentkit.definition import DefinitionStore
        except ImportError as e:
            raise RuntimeError(f"에이전트 정의 모듈이 없습니다 (agentkit.definition): {e}") from e
        _shared["store"] = DefinitionStore()
    return _shared["store"]


def _gateway(recorder):
    if "gateway" not in _shared:
        try:
            from agentkit.tools import build_gateway
            _shared["gateway"] = build_gateway(recorder=None if isinstance(recorder, NullRecorder) else recorder)
        except ImportError as e:
            logger.warning(f"No tools module ({e}); agents run without tools")
            _shared["gateway"] = None
    return _shared["gateway"]


def client_features(definition):
    """What a channel's "ready" message says about the screen (docs/DISPLAY.md): the panels to open,
    the touches the client may send, and whether the agent speaks first."""
    screen = getattr(definition, "screen", None)
    opening = getattr(definition, "opening", None)
    return {
        "screen": {"panels": list(getattr(screen, "panels", None) or [])},
        "actions": [{"name": a.name, "tool": a.tool, "description": a.description}
                    for a in getattr(definition, "actions", None) or []],
        "opening": bool(opening is not None and (opening.say.strip() or opening.tools)),
    }


def reset_shared():
    """Forget the shared store, gateway and recorder (tests that switch data directories)."""
    _shared.clear()


async def create_engine(agent_id, version=None, channel="text", test=False, simulated=False, label="",
                        session_key=None, *, llm=None, settings=None):
    """Assembles definition, tools and trace into an Engine for one conversation.

    version None: the version this conversation gets from the deployment (store.resolve, which
    honours the canary share; session_key keeps a returning caller on the same version).
    Raises whatever the definition store raises for an unknown agent or version.
    """
    session_id = uuid.uuid4().hex[:16]
    store = _store()
    if version is None:
        version = store.resolve(agent_id, session_key or session_id)
    definition = store.load(agent_id, version)
    recorder = _recorder()
    gateway = _gateway(recorder) if definition.tools else None
    return Engine(definition, gateway, recorder, llm, store=store, knowledge=build_knowledge(definition.knowledge),
                  channel=channel, test=test, simulated=simulated, label=label, session_id=session_id,
                  settings=settings)


from .driver import TextConversation  # noqa: E402  (driver imports create_engine lazily)

__all__ = ["Engine", "NullRecorder", "TextConversation", "client_features", "create_engine", "reset_shared"]
