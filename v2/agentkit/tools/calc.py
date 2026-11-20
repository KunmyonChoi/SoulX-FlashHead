"""calculate: arithmetic and prime factors, worked out by code, not by the model.

The office assistant was asked to factor 128 and answered "2 곱하기 9의 제곱 곱하기 9", then looped while
explaining (2026-11-20). The model says the numbers; this tool gets them right.

Only numbers, + - * / // % **, parentheses and a few functions are read (an AST walk, never eval), with
limits on sizes so that "9**9**9" can't hang the server.
"""
from __future__ import annotations

import ast
import math
import operator

from .adapters import ToolFailure

MAX_POWER = 1000          # largest exponent
MAX_DIGITS = 60           # largest result, in digits
MAX_FACTOR = 10 ** 12     # largest number to factor (trial division stays well under a second)

_BINARY = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
           ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCTIONS = {"sqrt": math.sqrt, "abs": abs, "round": round, "floor": math.floor, "ceil": math.ceil,
              "gcd": math.gcd, "lcm": math.lcm, "factorial": math.factorial, "log10": math.log10, "log": math.log}
_WORDS = str.maketrans({"×": "*", "x": "*", "X": "*", "÷": "/", "^": "**", ",": "", "，": ""})


def _check(value):
    if isinstance(value, int) and len(str(abs(value))) > MAX_DIGITS:
        raise ToolFailure("invalid_input", "결과가 너무 큰 수라 계산하지 않았습니다.")
    if isinstance(value, float) and (math.isinf(value) or math.isnan(value)):
        raise ToolFailure("invalid_input", "계산할 수 없는 값입니다(0으로 나누기 같은).")
    return value


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > MAX_POWER:
            raise ToolFailure("invalid_input", "지수가 너무 커서 계산하지 않았습니다.")
        try:
            return _check(_BINARY[type(node.op)](left, right))
        except ZeroDivisionError:
            raise ToolFailure("invalid_input", "0으로 나눌 수 없습니다.")
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCTIONS and not node.keywords:
        args = [_eval(a) for a in node.args]
        if node.func.id == "factorial" and (not args or args[0] > 200):
            raise ToolFailure("invalid_input", "200보다 큰 수의 계승은 계산하지 않습니다.")
        try:
            return _check(_FUNCTIONS[node.func.id](*args))
        except (TypeError, ValueError) as e:
            raise ToolFailure("invalid_input", f"{node.func.id} 계산을 할 수 없습니다: {e}")
    raise ToolFailure("invalid_input", "숫자와 + - * / ** ( ) 와 sqrt 같은 함수만 계산할 수 있습니다. 식을 숫자로 적어 다시 부르세요.")


def evaluate(expression: str):
    text = str(expression or "").strip().translate(_WORDS)
    if not text or len(text) > 200:
        raise ToolFailure("invalid_input", "계산할 식이 없거나 너무 깁니다.")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        raise ToolFailure("invalid_input", f"식을 읽지 못했습니다: {expression}. 숫자와 기호로 적어 다시 부르세요(예: 128/2, 2**7).")
    return _eval(tree)


def factorize(n: int):
    """[(prime, power)] of n > 1."""
    out, d = [], 2
    while d * d <= n:
        k = 0
        while n % d == 0:
            n //= d
            k += 1
        if k:
            out.append((d, k))
        d += 1 if d == 2 else 2
    if n > 1:
        out.append((n, 1))
    return out


def _number(value):
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return f"{value:.10g}"
    return str(value)


async def calculate(arguments, ctx):
    expression = str(arguments.get("expression") or "").strip()
    task = str(arguments.get("task") or "evaluate")
    if task == "factorize":
        value = evaluate(expression)
        if not isinstance(value, int) and not (isinstance(value, float) and value.is_integer()):
            raise ToolFailure("invalid_input", "소인수분해는 자연수만 할 수 있습니다.")
        n = int(value)
        if n < 2 or n > MAX_FACTOR:
            raise ToolFailure("invalid_input", f"소인수분해는 2부터 {MAX_FACTOR:,}까지의 자연수만 합니다.")
        factors = factorize(n)
        written = " × ".join(f"{p}^{k}" if k > 1 else str(p) for p, k in factors)
        steps, rest = [], n
        for p, k in factors:
            for _ in range(k):
                if rest == p:
                    break
                steps.append(f"{rest} ÷ {p} = {rest // p}")
                rest //= p
        prime = len(factors) == 1 and factors[0][1] == 1
        text = (f"{n}은(는) 소수입니다." if prime else
                f"{n} = {written} (계산 과정: {', '.join(steps)}). 말할 때는 숫자를 그대로 읽고, 과정은 물으면 차례대로 말하세요.")
        return {"ok": True, "text": text, "data": {"n": n, "factors": [[p, k] for p, k in factors]}, "error_type": None}
    value = evaluate(expression)
    return {"ok": True, "text": f"{expression} = {_number(value)}. 이 값을 그대로 말하세요.",
            "data": {"expression": expression, "value": value}, "error_type": None}
