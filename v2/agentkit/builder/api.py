"""REST of the builder agent: POST /api/builder/chat {agent?, message, conversation_id?}.

The response always carries `reply` and `conversation_id`. After a change it also carries
`agent_id`, `version`, `safety_change` and `changed_fields`, read back from the definition store.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .agent import BuilderAgent, BuilderError

router = APIRouter()
_builder: Optional[BuilderAgent] = None


class ChatRequest(BaseModel):
    message: str
    agent: Optional[str] = None
    agent_id: Optional[str] = None          # accepted as another spelling of `agent`
    conversation_id: Optional[str] = None


def get_builder() -> BuilderAgent:
    global _builder
    if _builder is None:
        _builder = BuilderAgent()
    return _builder


def configure(builder: Optional[BuilderAgent]) -> None:
    """Tests and scripts: use this BuilderAgent (None: build the default one at the next request)."""
    global _builder
    _builder = builder


@router.post("/api/builder/chat")
async def post_chat(body: ChatRequest):
    if not body.message.strip():
        raise HTTPException(400, "message가 비어 있습니다.")
    try:
        return await get_builder().chat(body.message, agent_id=body.agent or body.agent_id,
                                        conversation_id=body.conversation_id)
    except BuilderError as error:
        # 502, not 503: the studio page reads 503 as "the builder module is not installed"
        raise HTTPException(502, str(error))


@router.get("/api/builder/operations")
async def get_operations():
    """What the builder can do. There is no approve, deploy or rollback in this list."""
    from .operations import SPECS
    return {"operations": [{"name": spec["function"]["name"], "description": spec["function"]["description"]}
                           for spec in SPECS]}
