"""REST: GET /api/tools (the contracts), GET /api/tools/{name}, and what the in-process dummy
systems were made to change: GET /api/dummy/shop/records, GET /api/dummy/cafe/records."""
from fastapi import APIRouter, HTTPException

from .loader import ContractError, load_contracts, resolve

router = APIRouter()


def _summary(contract):
    out = contract.model_dump()
    out["id"] = contract.id
    out["needs_confirmation"] = contract.effect == "write" and bool(contract.confirm_template)
    return out


@router.get("/api/tools")
async def list_tools(packs: str | None = None):
    """{"tools": [ToolContract fields + id + needs_confirmation], "errors": [...]} sorted by name.
    packs="cafe,common": only the tools of those packs, i.e. what an agent with these tool_packs
    means by its bare names (two packs may both have a "search_menu").
    `display` is {"kind", "path", "refresh"} or null (what the tool puts on the customer's screen).
    The files are read on every request, so an edited contract shows without a restart."""
    try:
        contracts = load_contracts()
    except ContractError as e:
        return {"tools": [], "errors": e.problems}
    wanted = [p.strip() for p in (packs or "").split(",") if p.strip()]
    keys = [key for key in contracts if not wanted or contracts[key].pack in wanted or not contracts[key].pack]
    return {"tools": [_summary(contracts[key]) for key in sorted(keys, key=lambda k: (contracts[k].name, k))], "errors": []}


@router.get("/api/tools/{name}")
async def get_tool(name: str):
    try:
        contracts = load_contracts()
    except ContractError as e:
        raise HTTPException(status_code=500, detail=e.problems)
    try:
        return _summary(contracts[resolve(contracts, name)])        # an id, or a bare name only one pack has
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/api/dummy/shop/records")
async def shop_records(session_id: str | None = None, kind: str | None = None):
    """Refunds and handoffs the in-process dummy shop has taken (kind: "refund" | "handoff").
    They outlive the session, so a finished conversation can be checked."""
    from ..dummy.shop import get_shop
    return {"records": get_shop().records(session_id, kind)}


@router.get("/api/dummy/cafe/records")
async def cafe_records(session_id: str | None = None):
    """Orders the in-process dummy cafe has taken payment requests for: [{"order_no", "session_id",
    "lines", "total", ...}]. They outlive the session."""
    from ..dummy.cafe import get_cafe
    return {"records": get_cafe().records(session_id)}
