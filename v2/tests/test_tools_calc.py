"""The calculator tool: right numbers, no eval, bounded work."""
import pytest

import runtime_fakes  # noqa: F401

from agentkit.tools.adapters import ToolFailure
from agentkit.tools.calc import calculate


async def run(**arguments):
    return await calculate(arguments, None)


async def test_factorizes_128_correctly_with_its_steps():
    out = await run(expression="128", task="factorize")
    assert out["text"].startswith("128 = 2^7") and "128 ÷ 2 = 64" in out["text"] and out["data"]["factors"] == [[2, 7]]
    assert "소수" in (await run(expression="97", task="factorize"))["text"]
    assert (await run(expression="360", task="factorize"))["data"]["factors"] == [[2, 3], [3, 2], [5, 1]]


async def test_evaluates_ordinary_arithmetic():
    assert (await run(expression="(4500+5000)×2"))["data"]["value"] == 19000
    assert (await run(expression="2^10"))["data"]["value"] == 1024
    assert (await run(expression="35,000*0.15"))["text"].startswith("35,000*0.15 = 5250")
    assert abs((await run(expression="sqrt(2)"))["data"]["value"] - 1.41421356) < 1e-6


@pytest.mark.parametrize("expression", ["__import__('os').system('ls')", "9**9**9", "1/0", "open('x')", "a+1", ""])
async def test_refuses_what_is_not_safe_arithmetic(expression):
    with pytest.raises(ToolFailure):
        await run(expression=expression)
