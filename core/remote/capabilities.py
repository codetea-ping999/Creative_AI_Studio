"""Capability identifiers advertised by Agent Protocol v1."""

from __future__ import annotations

from enum import Enum


class AgentCapability(str, Enum):
    """Remote features a client may discover during the v1 handshake."""

    JOBS = "jobs"


SUPPORTED_CAPABILITIES: tuple[str, ...] = tuple(
    sorted(capability.value for capability in AgentCapability)
)


__all__ = ["AgentCapability", "SUPPORTED_CAPABILITIES"]
