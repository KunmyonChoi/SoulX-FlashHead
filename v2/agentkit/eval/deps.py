"""Collaborators of the evaluation module.

Every public function takes `store`, `definition_store`, `llm` and `driver_factory` as optional
arguments. What is not given is looked up here: first in the values set by `configure()` (tests,
the server), then in the real modules (agentkit.trace, agentkit.definition, agentkit.runtime).
"""
import inspect
import logging
import re

log = logging.getLogger("agentkit.eval")

_configured = {}


def configure(**kwargs):
    """Set process-wide defaults: store, definition_store, llm, driver_factory, and
    tool_descriptions ({tool name: (effect, description)}). None removes one."""
    for name, value in kwargs.items():
        if name not in ("store", "definition_store", "llm", "driver_factory", "tool_descriptions"):
            raise TypeError(f"unknown collaborator: {name}")
        if value is None:
            _configured.pop(name, None)
        else:
            _configured[name] = value


def reset():
    _configured.clear()


def get_store(store=None):
    if store is not None:
        return store
    if "store" in _configured:
        return _configured["store"]
    from agentkit.trace import get_store as real
    return real()


def get_definition_store(definition_store=None):
    if definition_store is not None:
        return definition_store
    if "definition_store" in _configured:
        return _configured["definition_store"]
    from agentkit.definition import DefinitionStore
    return DefinitionStore()


def get_llm(llm=None):
    if llm is not None:
        return llm
    if "llm" in _configured:
        return _configured["llm"]
    from agentkit.llm import LLM
    return LLM()


async def _real_driver(agent_id, version, *, label="", simulated=True):
    from agentkit.runtime import TextConversation
    return await TextConversation.open(agent_id, version=version, test=True, simulated=simulated,
                                       label=label)


def get_driver_factory(driver_factory=None):
    """A driver factory is `async (agent_id, version, *, label, simulated) -> ConversationDriver`."""
    return driver_factory or _configured.get("driver_factory") or _real_driver


def tool_descriptions(names, packs=None):
    """{name: (effect, first sentence of the description)} for the judge's glossary. Read from the
    tool contracts (agentkit.tools) when they are there; tools without a contract are left out."""
    known = _configured.get("tool_descriptions")
    if known is None:
        try:
            from agentkit.tools import load_contracts, resolve
            contracts, known = load_contracts(), {}
            for name in names:               # bare names of the agent's packs, or ids from recorded calls
                try:
                    contract = contracts[resolve(contracts, name, packs)]
                except KeyError:
                    continue
                known[name] = (contract.effect, contract.description)
        except Exception as e:
            log.info("no tool descriptions for the judge: %s", e)
            known = {}
    glossary = {}
    for name in names:
        if name in known:
            effect, description = known[name]
            glossary[name] = (effect, re.split(r"(?<=[.!?。])\s", str(description).strip(), maxsplit=1)[0])
    return glossary


def flush_recorder(store=None, timeout=3.0):
    """Wait until the real recorder has written what the conversation produced. Does nothing when
    `store` is not the trace module's own store (a fake has no recorder behind it)."""
    if store is not None and not type(store).__module__.startswith("agentkit.trace"):
        return
    try:
        from agentkit.trace import get_recorder
        get_recorder().flush(timeout=timeout)
    except Exception:
        pass


async def maybe_await(value):
    return await value if inspect.isawaitable(value) else value


def plain(value):
    """Pydantic models (at any depth) -> plain dicts and lists."""
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


# ---- documents (the trace store's general documents table) --------------------------------------

async def get_document(store, kind, key):
    """The document, or None when it does not exist (whatever the store does for a missing key)."""
    try:
        value = await maybe_await(store.get_document(kind, key))
    except (KeyError, LookupError):
        return None
    if isinstance(value, dict) and set(value) >= {"key", "value"} and isinstance(value["value"], dict):
        value = value["value"]
    return plain(value) if value is not None else None


async def list_documents(store, kind, prefix=""):
    """All documents of a kind as dicts. Accepts a store that lists values, {key, value} rows or keys."""
    rows = await maybe_await(store.list_documents(kind, prefix)) or []
    if isinstance(rows, dict):
        rows = list(rows.values())
    documents = []
    for row in rows:
        row = plain(row)
        if isinstance(row, str):
            row = await get_document(store, kind, row)
        elif isinstance(row, (list, tuple)) and len(row) == 2:
            row = row[1]
        elif isinstance(row, dict) and "value" in row and isinstance(row["value"], dict) and "key" in row:
            row = row["value"]
        if isinstance(row, dict):
            documents.append(row)
    return documents


# ---- masking for the fallback transcript (used only when the store has no record of a session) --

_FALLBACK_PATTERNS = [
    (re.compile(r"01[016789][- ]?\d{3,4}[- ]?\d{4}"), "[전화번호]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[이메일]"),
    (re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{1,3}\d{3,}(?!\d)"), "[주문번호]"),
    (re.compile(r"(뒤\s*(?:네|4)\s*자리\D{0,6})\d{4}"), r"\1[전화번호]"),
]


def mask_text(text):
    try:
        from agentkit.trace import get_masker
        return get_masker().mask_text(text)
    except Exception:
        for pattern, marker in _FALLBACK_PATTERNS:
            text = pattern.sub(marker, text)
        return text
