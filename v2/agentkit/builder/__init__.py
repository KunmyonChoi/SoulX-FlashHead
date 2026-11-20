"""The builder agent (WP8): makes and edits agent definitions in conversation, runs the tests,
compares runs and asks a person for approval. It cannot approve, deploy or roll back.

    from agentkit.builder import BuilderAgent
    result = await BuilderAgent().chat("환불 금액이 5만 원을 넘으면 사람에게 넘겨", agent_id="shop-support")
    from agentkit.builder.api import router
"""
from .agent import BuilderAgent, BuilderError
from .operations import MODIFYING, OPERATION_NAMES, SPECS, OperationError, Operations, Turn

__all__ = ["BuilderAgent", "BuilderError", "Operations", "Turn", "OperationError", "OPERATION_NAMES", "SPECS",
           "MODIFYING"]
