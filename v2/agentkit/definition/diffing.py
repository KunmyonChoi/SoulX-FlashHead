"""What changed between two definitions, and whether the change touches a safety rule (R-BLD-5)."""
from __future__ import annotations

from agentkit.contracts import SAFETY_FIELDS, AgentDefinition

# evaluation and tests are reported one level down ("evaluation.success", "tests.personas")
SPLIT_FIELDS = ("evaluation", "tests")
IGNORED_FIELDS = ("id", "version")
# opening, screen and actions are reported like any other top-level field. They are not safety
# fields: an action can only reach a tool that is already in `tools` (a safety field, checked by the
# loader), and the gateway refuses a touch on a tool that needs spoken consent.


def _flat(definition: AgentDefinition) -> dict:
    data = definition.model_dump(mode="json")
    out = {}
    for key, value in data.items():
        if key in IGNORED_FIELDS:
            continue
        if key in SPLIT_FIELDS:
            for sub, sub_value in value.items():
                out[f"{key}.{sub}"] = sub_value
        else:
            out[key] = value
    return out


def diff_definitions(before: AgentDefinition, after: AgentDefinition) -> dict[str, dict]:
    """{field: {"before": ..., "after": ...}} for every field that differs."""
    a, b = _flat(before), _flat(after)
    return {key: {"before": a.get(key), "after": b.get(key)}
            for key in list(dict.fromkeys([*a, *b])) if a.get(key) != b.get(key)}


def _safety_view(definition: AgentDefinition) -> dict:
    data = definition.model_dump(mode="json")
    view = {key: data[key] for key in SAFETY_FIELDS}
    view["safety_criteria"] = sorted((c for c in data["evaluation"]["success"] if c["safety"]),
                                     key=lambda c: c["id"])
    view["pii_fields"] = sorted((f for f in data["evaluation"]["extract"] if f["pii"]),
                                key=lambda f: f["name"])
    return view


def safety_changes(before: AgentDefinition, after: AgentDefinition) -> list[str]:
    """Which safety parts differ: names from SAFETY_FIELDS, "safety_criteria", "pii_fields"."""
    a, b = _safety_view(before), _safety_view(after)
    return [key for key in a if a[key] != b[key]]


def is_safety_change(before: AgentDefinition, after: AgentDefinition) -> bool:
    return bool(safety_changes(before, after))
