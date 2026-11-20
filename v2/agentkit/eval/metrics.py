"""Numbers computed from a stored session, and the metric judge (SuccessCriterion.judge == "metric").

Metric names (contracts.SuccessCriterion): response_ms_median, response_ms_max, tool_failures,
interruptions, turns, handoff (0/1). Added for agents with a screen (docs/DISPLAY.md; the contract's
docstring does not list them): cart_changes (executed add_to_cart / remove_from_cart / apply_coupon
calls; a touch reaches the same tools through the gateway, so spoken and touched changes are both
counted, once each) and payment_executed (0/1: request_payment was executed, or a receipt was shown).
Calls the gateway makes by itself to redraw the screen (attrs.refresh_of) are not the agent's calls
and count nowhere.

Turn 0 is the agent's opening (before the customer said anything): it is not a customer turn, so it
counts neither in `turns` nor in the response delays.
"""
import statistics

from agentkit.contracts import CriterionResult

METRICS = ("response_ms_median", "response_ms_max", "tool_failures", "interruptions", "turns", "handoff",
           "cart_changes", "payment_executed")
CART_TOOLS = ("add_to_cart", "remove_from_cart", "apply_coupon")
PAYMENT_TOOLS = ("request_payment",)

_OPS = {
    "<=": lambda a, b: a <= b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
    "<": lambda a, b: a < b,
    ">": lambda a, b: a > b,
}


def tool_calls(spans):
    """Tool spans as compact dicts: {turn, name, effect, ok, executed, needs_confirmation, error_type}."""
    calls = []
    for span in spans:
        if span.get("kind") != "tool":
            continue
        attrs = span.get("attrs") or {}
        if attrs.get("refresh_of"):
            # the gateway redrawing the screen after another tool (get_cart after add_to_cart): not
            # a call the agent made, so it is neither counted nor shown to the judge as one
            continue
        output = span.get("output") if isinstance(span.get("output"), dict) else {}

        def field(name, default=None):
            if attrs.get(name) is not None:
                return attrs[name]
            if output.get(name) is not None:
                return output[name]
            return default

        needs_confirmation = bool(field("needs_confirmation", False))
        error_type = field("error_type")
        ok = span.get("ok", True)
        if output.get("ok") is False:
            ok = False
        calls.append({
            "turn": span.get("turn", 0), "name": span.get("name", ""), "effect": field("effect", ""),
            "ok": bool(ok), "executed": bool(field("executed", False)),
            "needs_confirmation": needs_confirmation, "error_type": error_type,
            "t_start": span.get("t_start", 0.0),
        })
    calls.sort(key=lambda call: (call["turn"], call["t_start"]))
    return calls


def is_tool_failure(call):
    """A call that did not do what was asked. A write tool's first phase (proposal) is not a failure."""
    return not call["ok"] and not call["needs_confirmation"]


def customer_turns(turns):
    """The turns a customer started (said or touched something); turn 0 is the agent's opening."""
    return [turn for turn in turns or [] if (turn.get("turn") or 0) >= 1 or "turn" not in turn]


def is_cart_change(call):
    """The order was really changed: a line added or removed, a coupon applied (a refused or failed
    call changed nothing)."""
    return call["name"] in CART_TOOLS and call["ok"] and call["executed"]


def is_payment(call):
    return call["name"] in PAYMENT_TOOLS and call["ok"] and call["executed"]


def response_ms(turn):
    start, end = turn.get("t_agent_start"), turn.get("t_user_end")
    if start is None or end is None:
        return None
    return max(0.0, (start - end) * 1000.0)


def compute_metrics(session):
    """session: {"status", "turns", "spans"} as plain dicts. Latency metrics are left out when no
    turn has timing."""
    turns, spans = customer_turns(session.get("turns")), session.get("spans") or []
    metrics = {}
    delays = [ms for ms in (response_ms(turn) for turn in turns) if ms is not None]
    if delays:
        metrics["response_ms_median"] = round(statistics.median(delays), 1)
        metrics["response_ms_max"] = round(max(delays), 1)
    metrics["tool_failures"] = float(sum(1 for call in tool_calls(spans) if is_tool_failure(call)))
    interrupted_turns = {turn.get("turn") for turn in turns if turn.get("interrupted")}
    interrupted_turns |= {span.get("turn") for span in spans if span.get("kind") == "interrupt"}
    metrics["interruptions"] = float(len(interrupted_turns))
    metrics["turns"] = float(len(turns))
    handoff = (session.get("status") == "handoff" or any(turn.get("handoff") for turn in turns)
               or any(span.get("kind") == "handoff" for span in spans))
    metrics["handoff"] = 1.0 if handoff else 0.0
    calls = tool_calls(spans)
    metrics["cart_changes"] = float(sum(1 for call in calls if is_cart_change(call)))
    paid = any(is_payment(call) for call in calls) or any(
        span.get("kind") == "display" and span.get("name") == "receipt" for span in spans)
    metrics["payment_executed"] = 1.0 if paid else 0.0
    return metrics


def _evidence(criterion, session, passed):
    """The turns that explain a metric verdict (the ones breaking the limit when it failed)."""
    turns, spans = customer_turns(session.get("turns")), session.get("spans") or []
    metric = criterion.metric
    if metric in ("response_ms_median", "response_ms_max"):
        delays = [(turn.get("turn"), response_ms(turn)) for turn in turns]
        delays = [(number, ms) for number, ms in delays if ms is not None]
        if not delays:
            return []
        if not passed and criterion.op in ("<=", "<"):
            over = [number for number, ms in delays if not _OPS[criterion.op](ms, criterion.threshold)]
            if over:
                return over
        return [max(delays, key=lambda item: item[1])[0]]   # the slowest turn
    if metric == "tool_failures":
        return sorted({call["turn"] for call in tool_calls(spans) if is_tool_failure(call)})
    if metric == "interruptions":
        marked = {turn.get("turn") for turn in turns if turn.get("interrupted")}
        marked |= {span.get("turn") for span in spans if span.get("kind") == "interrupt"}
        return sorted(marked)
    if metric == "handoff":
        marked = {turn.get("turn") for turn in turns if turn.get("handoff")}
        marked |= {span.get("turn") for span in spans if span.get("kind") == "handoff"}
        return sorted(marked)
    if metric == "cart_changes":
        return sorted({call["turn"] for call in tool_calls(spans) if is_cart_change(call)})
    if metric == "payment_executed":
        marked = {call["turn"] for call in tool_calls(spans) if is_payment(call)}
        marked |= {span.get("turn") for span in spans
                   if span.get("kind") == "display" and span.get("name") == "receipt"}
        return sorted(marked)
    return []


def judge_metric(criterion, session, metrics=None):
    """`metric` `op` `threshold` over the session -> CriterionResult. passed=None when the criterion
    is incomplete, the metric is unknown, or the session has no data for it."""
    metrics = compute_metrics(session) if metrics is None else metrics

    def unjudged(reason):
        return CriterionResult(id=criterion.id, passed=None, reason=reason, safety=criterion.safety)

    if not criterion.metric or criterion.op is None or criterion.threshold is None:
        return unjudged("수치 기준에 metric, op, threshold가 모두 있어야 합니다.")
    if criterion.metric not in METRICS:
        return unjudged(f"알 수 없는 지표입니다: {criterion.metric}")
    if criterion.metric not in metrics:
        return unjudged(f"이 대화에는 {criterion.metric}을 계산할 기록이 없습니다.")
    value = metrics[criterion.metric]
    passed = bool(_OPS[criterion.op](value, criterion.threshold))
    reason = f"{criterion.metric} = {value:g} (기준: {criterion.op} {criterion.threshold:g})"
    return CriterionResult(id=criterion.id, passed=passed, reason=reason,
                           evidence_turns=[n for n in _evidence(criterion, session, passed) if n is not None],
                           safety=criterion.safety)
