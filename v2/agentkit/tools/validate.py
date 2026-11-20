"""
Input validation against a contract's input_schema.

jsonschema is not installed, and the contracts only use a small part of JSON Schema, so this is a
small validator for that part: type object with properties / required / additionalProperties, and
per property type (string, integer, number, boolean), enum, pattern, minimum, maximum, minLength,
maxLength. A property may also be an "object" (e.g. add_to_cart's options): it is handed on as it
is, its inner properties are for the model to read and are not checked here.

Models are sloppy about scalar types ("1" for 1, 1234 for "1234"), so values that convert without
loss are converted instead of refused. Unknown properties are dropped unless the schema says
additionalProperties: false, in which case they are an error.
"""
import json
import re

SUPPORTED_TYPES = ("string", "integer", "number", "boolean", "object")
OBJECT_KEYS = {"properties", "required", "additionalProperties"}
SUPPORTED_KEYS = {"type", "description", "enum", "pattern", "minimum", "maximum", "minLength", "maxLength",
                  "default", "title", "examples"}


class InputError(Exception):
    """problems: [(property name or "", sentence in Korean)]"""

    def __init__(self, problems):
        self.problems = problems
        super().__init__("; ".join(text for _, text in problems))


def check_schema(schema, where="input_schema"):
    """Problems of the schema itself (used when a contract is loaded): list of sentences."""
    problems = []
    if not isinstance(schema, dict):
        return [f"{where}: 객체(mapping)여야 합니다"]
    if schema.get("type") != "object":
        problems.append(f"{where}.type: \"object\"여야 합니다")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return problems + [f"{where}.properties: 객체(mapping)여야 합니다"]
    for name, spec in properties.items():
        here = f"{where}.properties.{name}"
        if not isinstance(spec, dict):
            problems.append(f"{here}: 객체(mapping)여야 합니다")
            continue
        if spec.get("type") not in SUPPORTED_TYPES:
            problems.append(f"{here}.type: {', '.join(SUPPORTED_TYPES)} 중 하나여야 합니다 (지금: {spec.get('type')!r})")
        for key in spec:
            if key not in SUPPORTED_KEYS and not (spec.get("type") == "object" and key in OBJECT_KEYS):
                problems.append(f"{here}.{key}: 지원하지 않는 항목입니다")
        if "enum" in spec and (not isinstance(spec["enum"], list) or not spec["enum"]):
            problems.append(f"{here}.enum: 값이 하나 이상 든 목록이어야 합니다")
        if "pattern" in spec:
            try:
                re.compile(spec["pattern"])
            except (re.error, TypeError):
                problems.append(f"{here}.pattern: 정규식으로 읽을 수 없습니다")
    required = schema.get("required", [])
    if not isinstance(required, list):
        problems.append(f"{where}.required: 목록이어야 합니다")
    else:
        for name in required:
            if name not in properties:
                problems.append(f"{where}.required: '{name}'이 properties에 없습니다")
    return problems


def _label(name, spec):
    description = str(spec.get("description") or "").strip()
    short = re.split(r"[.。\n]", description)[0].strip()
    return f"{name}({short})" if short and len(short) <= 30 else name


def _convert(value, kind):
    """(converted value, True) or (None, False)."""
    if kind == "string":
        if isinstance(value, str):
            return value, True
        if isinstance(value, bool):
            return None, False
        if isinstance(value, int):
            return str(value), True
        if isinstance(value, float) and value.is_integer():
            return str(int(value)), True
        return None, False
    if kind == "object":
        if isinstance(value, dict):
            return value, True
        if isinstance(value, str) and value.strip().startswith("{"):    # models send it as a JSON string
            try:
                loaded = json.loads(value)
            except ValueError:
                return None, False
            return (loaded, True) if isinstance(loaded, dict) else (None, False)
        return None, False
    if kind == "boolean":
        if isinstance(value, bool):
            return value, True
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true", True
        return None, False
    if isinstance(value, bool):
        return None, False
    if kind == "integer":
        if isinstance(value, int):
            return value, True
        if isinstance(value, float) and value.is_integer():
            return int(value), True
        if isinstance(value, str) and re.fullmatch(r"\s*[+-]?\d+\s*", value):
            return int(value), True
        return None, False
    if kind == "number":
        if isinstance(value, (int, float)):
            return value, True
        if isinstance(value, str):
            try:
                return float(value), True
            except ValueError:
                return None, False
    return None, False


_TYPE_WORDS = {"string": "글자", "integer": "정수", "number": "숫자", "boolean": "참/거짓",
               "object": "이름과 값의 묶음(객체)"}


def validate(schema, arguments):
    """Returns the cleaned arguments (converted scalars, unknown properties dropped).
    Raises InputError listing every problem."""
    if not isinstance(arguments, dict):
        raise InputError([("", "인자는 이름과 값의 묶음(객체)이어야 합니다.")])
    properties = schema.get("properties") or {}
    problems, cleaned = [], {}
    for name in schema.get("required") or []:
        if arguments.get(name) is None:
            problems.append((name, f"{_label(name, properties.get(name, {}))} 값이 없습니다."))
    for name, value in arguments.items():
        spec = properties.get(name)
        if spec is None:
            if schema.get("additionalProperties") is False:
                problems.append((name, f"'{name}'은 이 도구에 없는 인자입니다."))
            continue
        if value is None:
            continue    # reported above if required; an optional null is the same as absent
        kind = spec.get("type", "string")
        value, ok = _convert(value, kind)
        label = _label(name, spec)
        if not ok:
            problems.append((name, f"{label} 값은 {_TYPE_WORDS.get(kind, kind)}여야 합니다."))
            continue
        if "enum" in spec and value not in spec["enum"]:
            problems.append((name, f"{label} 값은 {', '.join(map(str, spec['enum']))} 중 하나여야 합니다."))
            continue
        if kind == "object":
            cleaned[name] = value
            continue
        if kind == "string":
            if "pattern" in spec and not re.search(spec["pattern"], value):
                problems.append((name, f"{label} 값 '{value}'의 형식이 맞지 않습니다."))
                continue
            if len(value) < spec.get("minLength", 0):
                problems.append((name, f"{label} 값이 비어 있거나 너무 짧습니다."))
                continue
            if "maxLength" in spec and len(value) > spec["maxLength"]:
                problems.append((name, f"{label} 값이 너무 깁니다 (최대 {spec['maxLength']}자)."))
                continue
        else:
            if "minimum" in spec and value < spec["minimum"]:
                problems.append((name, f"{label} 값은 {spec['minimum']} 이상이어야 합니다."))
                continue
            if "maximum" in spec and value > spec["maximum"]:
                problems.append((name, f"{label} 값은 {spec['maximum']} 이하여야 합니다."))
                continue
        cleaned[name] = value
    if problems:
        raise InputError(problems)
    return cleaned
