"""Versioned CreativeStudio agent handshake endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request

from core.remote import AgentInfo, AgentProtocol

router = APIRouter(prefix="/v1/agent", tags=["agent"])


@router.get("/info", response_model=AgentInfo)
def get_agent_info(request: Request) -> AgentInfo:
    """Return the immutable handshake for the running Studio API instance."""

    protocol: AgentProtocol = request.app.state.agent_protocol
    return protocol.info()


__all__ = ["router"]
