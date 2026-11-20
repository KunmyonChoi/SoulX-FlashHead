"""
Tools: contracts (v2/tools/*.yaml), the gateway every call goes through, and the adapters.

    from agentkit.tools import build_gateway
    gateway = build_gateway(recorder=None)
    result = await gateway.call(ctx, "get_order", {"order_id": "A1001"})
"""
from .adapters import AdapterUnavailable, Reply, ToolFailure, record_call
from .consent import is_consent, read_consent
from .gateway import Gateway, build_gateway
from .loader import ContractError, UnknownTool, catalog, load_contract, load_contracts, resolve
from .validate import InputError, validate

__all__ = ["build_gateway", "Gateway", "ContractError", "load_contract", "load_contracts", "resolve", "catalog", "UnknownTool",
           "read_consent", "is_consent", "validate", "InputError",
           "ToolFailure", "AdapterUnavailable", "Reply", "record_call"]
