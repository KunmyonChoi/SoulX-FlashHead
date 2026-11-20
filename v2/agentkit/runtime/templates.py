"""Filling tool reply templates (contracts.ToolReply)."""
import re

_NATIVE = {1: "한", 2: "두", 3: "세", 4: "네", 5: "다섯", 6: "여섯", 7: "일곱", 8: "여덟", 9: "아홉", 10: "열"}
_FIELD = re.compile(r"\{([A-Za-z0-9_.]+)(?:\|([a-z]+))?\}")


class Missing(Exception):
    """A placeholder has no value: the template cannot be used for this call."""


def _lookup(values, path):
    value = values
    for part in path.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            raise Missing(path)
    if value is None or value == "":
        raise Missing(path)
    return value


def _apply(value, name):
    if name == "count":
        return _NATIVE.get(value, str(value)) if isinstance(value, int) else str(value)
    if name == "paren":
        items = value if isinstance(value, list) else [value]
        items = [str(item) for item in items if item not in (None, "")]
        return f"({', '.join(items)})" if items else ""
    raise Missing(f"filter {name}")


def fill(template, arguments, data):
    """The template with every placeholder filled. Raises Missing when a value is not there."""
    values = dict(data) if isinstance(data, dict) else {}
    values["args"] = arguments or {}

    def replace(match):
        path, name = match.group(1), match.group(2)
        try:
            value = _lookup(values, path)
        except Missing:
            if name == "paren":
                return ""          # an optional list that is absent reads as nothing
            raise
        return _apply(value, name) if name else str(value)

    return _FIELD.sub(replace, template).strip()
