"""
Shared contracts of the v2 framework: the types and interfaces every module codes against.

This file is the fixed point of the parallel work. Modules may not change it; if something here
turns out to be wrong or missing, write it down in v2/docs/notes/<module>.md and work around it.

Sections: agent definition, tools, trace records, conversation engine, evaluation, releases.
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Literal, Optional, Protocol

from pydantic import BaseModel, Field

# ================================================================================================
# 1. Agent definition (loaded from v2/agents/<agent_id>/versions/<version>/*.yaml)
# ================================================================================================

Language = Literal["ko", "en", "auto"]
ReplyLength = Literal["short", "normal", "long", "explain"]
Channel = Literal["text", "voice", "avatar"]


class LLMSettings(BaseModel):
    model: str = "gemma4:26b"
    num_ctx: int = 8192
    num_predict: int = 1500


class VoiceSettings(BaseModel):
    """Used by the voice and avatar channels."""
    tts_voice: str = "F1"
    stt: Literal["whisper", "qwen"] = "whisper"
    endpoint: Literal["silence", "smart"] = "silence"
    interrupt_mode: Literal["basic", "unmute", "unmute_gate"] = "unmute_gate"
    barge_in: bool = True           # listen while the agent talks (needs headphones, or echo cancels poorly)
    speculative: bool = True        # prepare the reply before the end of turn is certain (read tools only)


class AvatarSettings(BaseModel):
    enabled: bool = False
    image: Optional[str] = None     # path relative to the repository root


class KnowledgeSettings(BaseModel):
    """Retrieval that runs before every reply. kind "none" switches it off."""
    kind: Literal["none", "weknora"] = "none"
    name: str = ""                  # shown to people and named in the prompt
    kb_id: str = ""
    threshold: float = 0.4


class FlowRule(BaseModel):
    id: str
    rule: str                       # one sentence the agent must follow, in the agent's language


class HandoffRule(BaseModel):
    id: str
    when: str                       # condition in words, e.g. "환불 금액이 100000원을 넘는다"
    say: str = ""                   # what the customer must hear when handed over for this reason (as it is),
                                    # e.g. the emergency number; empty: one sentence of the agent's own


class Guard(BaseModel):
    """A check on a sentence before it is spoken (see ARCHITECTURE.md, "말하기 전 검사").

    kind "regex":  the sentence matches `pattern`  -> violation
    kind "claims_lookup": the sentence matches `pattern` although none of `unless_tools`
                   was called for this reply -> violation
    On a violation the runtime withdraws the unspoken text and regenerates once with `nudge`
    appended as a system note; a second violation drops the sentence.
    """
    id: str
    kind: Literal["regex", "claims_lookup"] = "regex"
    pattern: str
    unless_tools: list[str] = Field(default_factory=list)
    nudge: str


class SuccessCriterion(BaseModel):
    """judge "llm": an LLM reads the masked transcript and decides whether `rule` held.
    judge "metric": `metric` `op` `threshold` over the session, e.g. response_ms_median <= 2500.
    Metrics: response_ms_median, response_ms_max, tool_failures, interruptions, turns, handoff (0/1).
    """
    id: str
    rule: str
    judge: Literal["llm", "metric"] = "llm"
    metric: Optional[str] = None
    op: Optional[Literal["<=", ">=", "==", "<", ">"]] = None
    threshold: Optional[float] = None
    safety: bool = False            # a failed safety criterion blocks approval regardless of pass rate


class ExtractField(BaseModel):
    name: str
    type: Literal["string", "enum", "boolean", "number"] = "string"
    values: list[str] = Field(default_factory=list)   # for enum
    description: str = ""
    pii: bool = False               # stored as "[채워짐]" / "[비어 있음]" only


class Evaluation(BaseModel):
    success: list[SuccessCriterion] = Field(default_factory=list)
    extract: list[ExtractField] = Field(default_factory=list)
    latency_budget_ms: int = 2500   # a turn slower than this is highlighted in the monitor


class TestPersona(BaseModel):
    """A simulated customer. `expect` maps criterion ids to the verdict a correct agent earns."""
    id: str
    description: str                # who they are and how they talk
    goal: str                       # what they want out of the conversation
    facts: dict[str, Any] = Field(default_factory=dict)   # e.g. order_id, phone_last4 (dummy data)
    opening: Optional[str] = None   # first utterance; generated when missing
    max_turns: int = 8
    expect: dict[str, bool] = Field(default_factory=dict)


class TestCase(BaseModel):
    """A stored conversation to replay: the user's turns are replayed in order."""
    id: str
    description: str = ""
    user_turns: list[str]
    facts: dict[str, Any] = Field(default_factory=dict)
    expect: dict[str, bool] = Field(default_factory=dict)


class Tests(BaseModel):
    personas: list[TestPersona] = Field(default_factory=list)
    cases: list[TestCase] = Field(default_factory=list)


class OpeningCall(BaseModel):
    name: str                       # a read tool, called when the session starts
    arguments: dict[str, Any] = Field(default_factory=dict)


class Opening(BaseModel):
    """What the agent does before the customer has said anything (kiosk, drive-through).

    `say` is spoken or shown as the agent's first words. `tools` are called first, in order; what
    they return goes on the screen (their contract's `display`) and into the conversation history,
    so the model knows, for example, the menu it is about to take orders from."""
    say: str = ""
    tools: list[OpeningCall] = Field(default_factory=list)


class ScreenSettings(BaseModel):
    """Persistent areas the client keeps on screen next to the conversation. Empty: chat only."""
    panels: list[Literal["menu", "cart"]] = Field(default_factory=list)


class ClientAction(BaseModel):
    """Something the customer can do by touching the screen instead of saying it.

    The client sends {"type": "action", "name", "arguments"}; the engine calls `tool` through the
    gateway with those arguments and then lets the model react in one short reply. Only tools the
    gateway would execute at once are allowed here (read tools, write tools without a
    confirm_template); a tool that needs spoken consent cannot be triggered by a touch."""
    name: str                       # e.g. "add_item"
    tool: str                       # e.g. "add_to_cart"
    description: str = ""           # for people: what touching does
    say: str = ""                   # how the touch reads in the transcript, "{field}" from the arguments


class AgentDefinition(BaseModel):
    """Everything that makes one agent that agent. Files: agent.yaml (id..avatar, knowledge),
    tools.yaml (tools), flow.yaml (flow, handoff, prohibitions), guards.yaml (guards),
    evaluation.yaml (evaluation), tests.yaml (tests)."""
    id: str
    version: int = 1
    name: str
    description: str = ""
    persona: str                    # who the agent is and how it talks; becomes the system prompt's head
    language: Language = "ko"
    reply_length: ReplyLength = "short"
    live_clock: bool = False        # tell the model today's date and the time of day
    llm: LLMSettings = Field(default_factory=LLMSettings)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    avatar: AvatarSettings = Field(default_factory=AvatarSettings)
    knowledge: KnowledgeSettings = Field(default_factory=KnowledgeSettings)
    tools: list[str] = Field(default_factory=list)       # names of tool contracts
    # the packs those names are looked up in, e.g. ["burger", "common"]: two systems may both have a
    # "search_menu", and an agent means the one of its own packs. Empty: every pack, and a name that
    # more than one pack has is then an error.
    tool_packs: list[str] = Field(default_factory=list)
    flow: list[FlowRule] = Field(default_factory=list)
    handoff: list[HandoffRule] = Field(default_factory=list)
    prohibitions: list[str] = Field(default_factory=list)  # things the agent must never do
    guards: list[Guard] = Field(default_factory=list)
    evaluation: Evaluation = Field(default_factory=Evaluation)
    tests: Tests = Field(default_factory=Tests)
    # screen and first words (agent.yaml: opening, screen, actions); all optional, see docs/DISPLAY.md
    opening: Opening = Field(default_factory=Opening)
    screen: ScreenSettings = Field(default_factory=ScreenSettings)
    actions: list[ClientAction] = Field(default_factory=list)


# Parts of a definition whose change the builder must flag as a safety change (R-BLD-5).
SAFETY_FIELDS = ("tools", "tool_packs", "handoff", "prohibitions", "guards")
# ...plus any SuccessCriterion with safety=True, and ExtractField.pii.

# ================================================================================================
# 2. Tools (contracts in v2/tools/<name>.yaml; all calls go through the gateway)
# ================================================================================================

ToolErrorType = Literal[
    "not_found", "permission_denied", "invalid_input", "precondition_failed",
    "not_confirmed", "system_error", "timeout",
]


class AdapterSpec(BaseModel):
    """How the call reaches the outside system.

    kind "python": target "package.module:callable"; called as `await fn(arguments, ctx)`.
    kind "dummy":  target names a dummy system method, e.g. "shop:get_order".
    kind "http":   target is a URL; POST {"arguments": ..., "context": {...}} -> {"ok", "data", ...}.
    kind "replay": target is a JSONL file of recorded (arguments -> result) pairs.
    """
    kind: Literal["python", "dummy", "http", "replay"] = "dummy"
    target: str
    options: dict[str, Any] = Field(default_factory=dict)


DisplayKind = Literal["menu", "items", "cart", "confirm", "receipt", "notice"]


class DisplaySpec(BaseModel):
    """How a tool's result is put on the customer's screen (payload shapes: docs/DISPLAY.md).

    The screen shows what the system returned, never what the model said about it: the gateway
    builds the payload from ToolResult.data and the engine forwards it without the model."""
    kind: DisplayKind
    path: str = ""                  # dotted path into ToolResult.data holding the payload; "" = all of data
    # after this tool executed: read tools (called without arguments) whose display is shown too,
    # e.g. add_to_cart -> ["get_cart"], so the order on screen is always the system's own state
    refresh: list[str] = Field(default_factory=list)


class ToolReply(BaseModel):
    """A fixed spoken reply for a successful call, used instead of a second model pass.

    After the model calls a tool, the engine normally runs the model once more to word the answer.
    For tools whose answer is always the same shape ("아이스 아메리카노 두 잔 담았어요."), that second
    pass costs about a second and adds nothing. With `reply`, the engine fills `say` from the call's
    arguments and result and speaks it at once; `closing` is said once after all such sentences of the
    reply. Placeholders: {path} into {"args": arguments, **result.data}, dotted; filters `|count`
    (1-10 as native Korean numerals: 두) and `|paren` (a list as "(a, b)" or nothing).
    The engine falls back to a model pass when the call failed, needs confirmation, a placeholder is
    missing, the model also said something, or the reply's language is not the template's."""
    say: str
    closing: str = ""


class ToolContract(BaseModel):
    name: str                                 # what the model calls it; unique inside its pack
    # the pack (one outside system's tools, the folder v2/tools/<pack>/) the contract was read from;
    # "" for a contract that is in no pack. Set by the loader, not written in the file.
    pack: str = ""
    description: str                          # for the model: what it does and when to use it
    effect: Literal["read", "write"] = "read"
    input_schema: dict[str, Any]              # JSON Schema (type object, properties, required)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    # write tools: the sentence read to the customer before execution; "{field}" placeholders
    # are filled from the arguments and from the adapter's preview (see ToolGateway.call).
    confirm_template: Optional[str] = None
    idempotency_fields: list[str] = Field(default_factory=list)   # arguments that identify "the same request"
    requires: list[str] = Field(default_factory=list)   # session facts needed first, e.g. ["verified"]
    grants: list[str] = Field(default_factory=list)     # session facts set when the call succeeds
    timeout_s: float = 4.0
    retries: int = 1                          # read tools only; write tools are never retried blindly
    pii_input: list[str] = Field(default_factory=list)    # argument names holding personal data
    pii_output: list[str] = Field(default_factory=list)   # dotted paths in `data` holding personal data
    filler: Optional[str] = None              # said at once while the call runs ("주문을 확인해 볼게요.")
    display: Optional[DisplaySpec] = None     # what the customer sees when the call succeeds
    reply: Optional[ToolReply] = None         # spoken at once on success, skipping the model's second pass
    adapter: AdapterSpec

    @property
    def id(self) -> str:
        """The name that is unique over all packs: "burger.search_menu" (or the bare name outside a pack).
        The gateway keys contracts by it; a recorded call keeps the bare name and has it in attrs.tool_id."""
        return f"{self.pack}.{self.name}" if self.pack else self.name


class ToolContext(BaseModel):
    """What the gateway knows about the conversation a call comes from."""
    session_id: str
    agent_id: str
    version: int
    turn: int                                 # index of the current user turn, starting at 1
    language: Literal["ko", "en"] = "ko"
    test: bool = False                        # test or simulated session: adapters must not touch real systems
    last_user_text: str = ""                  # the customer's latest utterance (consent is read from it)
    speculative: bool = False                 # the turn is not final yet: write tools must refuse


class ToolResult(BaseModel):
    ok: bool
    text: str                                 # what the model is told (also on failure)
    data: Any = None                          # structured result, before masking
    executed: bool = False                    # a write tool really changed something
    needs_confirmation: bool = False          # write tool, first phase: nothing was changed yet
    confirmation_text: Optional[str] = None   # the sentence to read to the customer
    error_type: Optional[ToolErrorType] = None
    ms: float = 0.0
    items: int = 0                            # numbered entries in a list result, for display
    call_id: str = ""
    # Screens to show, in order, each {"kind": DisplayKind, "data": {...}, "source": tool name}.
    # Set by the gateway: the contract's display on success (plus its `refresh` tools); for a write
    # tool's proposal a {"kind": "confirm"} built from the confirmation text and the preview.
    display: list[dict[str, Any]] = Field(default_factory=list)


class ToolGateway(Protocol):
    """The only way a tool is called.

    Write tools run in two phases (R-TOOL-4), enforced here and not left to the model:
      1. A call with no matching proposal creates one and returns needs_confirmation=True with
         confirmation_text. Nothing is executed.
      2. The same call (same idempotency fields) in a LATER turn executes only if
         ctx.last_user_text reads as consent; otherwise error_type "not_confirmed".
      A call with ctx.speculative=True never executes a write tool.
      A request that already executed returns the first result again and executed=False (R-TOOL-5).
    `requires` facts missing -> error_type "precondition_failed". Facts live per session.
    Every call is recorded as a Span(kind="tool") with pii fields masked.
    """

    def names(self) -> list[str]: ...

    def contract(self, name: str) -> ToolContract: ...

    def specs(self, tool_names: list[str]) -> list[dict]:
        """Ollama/OpenAI style function specs for these tools."""
        ...

    async def call(self, ctx: ToolContext, name: str, arguments: dict) -> ToolResult: ...

    def facts(self, session_id: str) -> set[str]: ...

    def end_session(self, session_id: str) -> None: ...


# ================================================================================================
# 3. Trace records (written through the Recorder; personal data is masked before storage)
# ================================================================================================

SpanKind = Literal[
    "endpoint",    # end-of-turn decision: attrs {mode, silence_s, probability}
    "stt",         # speech -> text: output {text}, attrs {engine, language}
    "retrieval",   # knowledge search: input {query}, output {sources, top_score}, attrs {used}
    "llm",         # one generation pass: input {messages}, output {text, tool_calls}, attrs {model, round, first_token_ms}
    "tool",        # one gateway call: input {arguments}, output ToolResult fields, attrs {effect, executed, error_type}
    "guard",       # a sentence withheld: input {sentence}, attrs {guard_id, action: "regenerated"|"dropped"}
    "tts",         # synthesis of one piece: input {chars}, attrs {seconds}
    "output",      # first sound or text of the reply reaching the client: attrs {phase: "filler"|"reply"}
    "interrupt",   # the user cut the agent off: attrs {heard_chars}
    "handoff",     # handed to a human: attrs {rule_id, reason}
    "display",     # something was put on the customer's screen: name = kind, output {data}, attrs {source}
    "action",      # the customer touched the screen: name = action name, input {arguments}, attrs {tool}
    "template",    # a reply built from tool reply templates without a model pass: output {text}, attrs {tools, saved_pass}
]
SessionStatus = Literal["active", "completed", "handoff", "abandoned", "error"]


class SessionMeta(BaseModel):
    session_id: str
    agent_id: str
    version: int
    channel: Channel
    test: bool = False            # an internal tester agreed to recording (R-OBS-5)
    simulated: bool = False       # the customer is a simulated persona
    started_at: float             # epoch seconds
    settings: dict[str, Any] = Field(default_factory=dict)
    label: str = ""               # e.g. the persona or test case id


class Span(BaseModel):
    session_id: str
    turn: int                     # user turn index, starting at 1; 0 for things before the first turn
    kind: SpanKind
    name: str = ""                # tool name, model name, ...
    t_start: float                # epoch seconds
    t_end: float
    ok: bool = True
    input: Any = None
    output: Any = None
    attrs: dict[str, Any] = Field(default_factory=dict)


class TurnRecord(BaseModel):
    session_id: str
    turn: int
    user_text: str
    agent_text: str               # what was actually said (cut to what was heard after an interruption)
    t_user_end: float             # when the user's turn was complete (text sent / end of speech)
    t_agent_start: Optional[float] = None   # first text or sound of the real reply
    t_agent_end: Optional[float] = None
    interrupted: bool = False
    handoff: bool = False


class Recorder(Protocol):
    """Never blocks and never raises into the conversation (R-OBS-7). Masks before it stores."""

    def start_session(self, meta: SessionMeta) -> None: ...

    def span(self, span: Span, pii_paths: list[str] | None = None) -> None:
        """pii_paths: dotted paths inside span.input / span.output to replace by a marker,
        e.g. ["input.arguments.phone_last4", "output.data.address"]."""
        ...

    def turn(self, turn: TurnRecord) -> None: ...

    def audio(self, session_id: str, turn: int, role: Literal["user", "agent"], pcm16: bytes) -> None:
        """16 kHz mono PCM. Stored only for test or simulated sessions; dropped otherwise."""
        ...

    def end_session(self, session_id: str, status: SessionStatus) -> None: ...


class Masker(Protocol):
    def mask_text(self, text: str) -> str: ...

    def mask(self, value: Any, pii_paths: list[str] | None = None) -> Any:
        """Deep copy with every string run through mask_text and every pii path replaced."""
        ...


# ================================================================================================
# 4. Conversation engine (the text core every channel sits on)
# ================================================================================================


class ReplyEvent(BaseModel):
    """What the engine yields while it works on one reply.

    type "delta":    text streamed for display (may later be withdrawn by "replace")
    type "replace":  the reply text so far, after a guard withdrew unspoken text
    type "piece":    a sentence cleared for speech / final display, in order
    type "filler":   a short line to say while a tool runs (text in `text`)
    type "tool":     a tool call started or finished (data: {state, name, arguments, result...})
    type "knowledge":retrieval started or finished (data as in v1's "knowledge" message)
    type "llm":      generation pass started / first token / done (data as in v1's "llm" message)
    type "handoff":  the conversation is handed to a human (data: {rule_id, reason})
    type "display":  put this on the customer's screen (data: {kind, data, source}); never spoken
    type "done":     the reply is complete (text: the whole reply)
    """
    type: Literal["delta", "replace", "piece", "filler", "tool", "knowledge", "llm", "handoff", "display", "done"]
    text: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class ReplyEngine(Protocol):
    """One conversation's brain: history, prompt, tools, guards. No audio in here.

    The text channel shows the events; the voice channel turns "filler" and "piece" into speech.
    """

    session_id: str
    definition: AgentDefinition
    turn: int
    handed_off: bool

    def reply(self, user_text: str, *, language: Optional[Literal["ko", "en"]] = None,
              speculative: bool = False, t_user_end: Optional[float] = None) -> AsyncIterator[ReplyEvent]:
        """Starts the next turn. Cancelling the iteration cancels the reply and its tool calls;
        a cancelled speculative reply leaves no trace in the history."""
        ...

    def open(self) -> AsyncIterator[ReplyEvent]:
        """The agent's opening (definition.opening), to be run once before the first customer turn:
        "tool" and "display" events for the opening calls, then "piece" and "done" with the opening
        words (nothing is yielded but "done" when the definition has no opening). Recorded as turn 0;
        the opening calls and their results stay in the history."""
        ...

    def act(self, name: str, arguments: dict[str, Any]) -> AsyncIterator[ReplyEvent]:
        """A touch on the screen (definition.actions). Counts as a customer turn: the mapped tool is
        called through the gateway, its "tool" and "display" events are yielded, and the model then
        reacts with one short reply (the usual events up to "done"). An unknown action, or one whose
        tool would need spoken consent, yields a "done" with data {"error": ...} and changes nothing."""
        ...

    def truncate_last_reply(self, heard_text: str) -> None:
        """After an interruption: keep only what the user actually heard ("" = nothing)."""
        ...

    def mark_output(self, phase: Literal["filler", "reply"], t: float) -> None:
        """The channel reports when the first sound or text reached the client."""
        ...

    async def close(self, status: SessionStatus = "completed") -> None: ...


class AgentTurn(BaseModel):
    """Result of one turn through a ConversationDriver."""
    text: str
    displays: list[dict[str, Any]] = Field(default_factory=list)     # {kind, data, source} shown during the turn
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)   # {name, arguments, ok, executed, error_type}
    handoff: bool = False
    ms: float = 0.0


class ConversationDriver(Protocol):
    """In-process way to talk to an agent in text, used by tests and simulated customers."""

    session_id: str

    async def say(self, text: str) -> AgentTurn: ...

    async def opening(self) -> AgentTurn:
        """Runs the agent's opening once (ReplyEngine.open) and returns its words and screens."""
        ...

    async def act(self, name: str, arguments: dict[str, Any]) -> AgentTurn: ...

    async def close(self, status: SessionStatus = "completed") -> None: ...


# ================================================================================================
# 5. Evaluation
# ================================================================================================


class CriterionResult(BaseModel):
    id: str
    passed: Optional[bool]        # None: could not be judged
    reason: str = ""
    evidence_turns: list[int] = Field(default_factory=list)
    safety: bool = False


class AnalysisResult(BaseModel):
    session_id: str
    agent_id: str
    version: int
    summary: str
    extracted: dict[str, Any] = Field(default_factory=dict)
    criteria: list[CriterionResult] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)
    created_at: float = 0.0


class TestItemResult(BaseModel):
    item_id: str                  # persona or case id
    kind: Literal["persona", "case"]
    session_id: str
    passed: bool                  # every expectation met and no safety criterion failed
    criteria: list[CriterionResult] = Field(default_factory=list)
    mismatches: list[str] = Field(default_factory=list)   # criterion ids whose verdict differs from `expect`
    metrics: dict[str, float] = Field(default_factory=dict)


class TestRun(BaseModel):
    run_id: str
    agent_id: str
    version: int
    started_at: float
    finished_at: Optional[float] = None
    items: list[TestItemResult] = Field(default_factory=list)
    pass_rate: float = 0.0
    safety_failures: int = 0
    metrics: dict[str, float] = Field(default_factory=dict)


class Comparison(BaseModel):
    base_run: str
    new_run: str
    improved: list[str] = Field(default_factory=list)     # item ids failing before, passing now
    regressed: list[str] = Field(default_factory=list)
    unchanged_pass: list[str] = Field(default_factory=list)
    unchanged_fail: list[str] = Field(default_factory=list)
    metric_changes: dict[str, tuple[float, float]] = Field(default_factory=dict)   # name -> (base, new)


# ================================================================================================
# 6. Versions and releases
# ================================================================================================

VersionState = Literal["draft", "tested", "pending_approval", "approved", "deployed", "retired"]


class VersionInfo(BaseModel):
    agent_id: str
    version: int
    state: VersionState = "draft"
    created_at: float = 0.0
    created_by: str = ""              # "builder" or a person's name
    based_on: Optional[int] = None
    note: str = ""                    # what changed, in a sentence
    safety_change: bool = False       # the diff to `based_on` touches SAFETY_FIELDS (R-BLD-5)
    changed_fields: list[str] = Field(default_factory=list)
    test_run_id: Optional[str] = None
    approved_by: Optional[str] = None
    approved_at: Optional[float] = None


class Deployment(BaseModel):
    """Which versions take the conversations of one agent. `canary` gets `canary_percent` of them."""
    agent_id: str
    stable: Optional[int] = None
    canary: Optional[int] = None
    canary_percent: int = 0
    previous_stable: Optional[int] = None   # what rollback returns to
    updated_at: float = 0.0


class ReleaseError(Exception):
    """A release step was refused: untested version submitted, unapproved version deployed, ..."""
