"""Schemas shared by CreativeStudio remote clients and the agent API."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class AgentInfo(BaseModel):
    """Versioned handshake returned when a client connects to a Studio agent."""

    model_config = ConfigDict(extra="forbid")

    protocol_version: str = Field(min_length=1)
    agent_version: str = Field(min_length=1)
    instance_id: str = Field(min_length=1)
    capabilities: tuple[str, ...] = ()


__all__ = ["AgentInfo"]
