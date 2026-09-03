"""CreativeStudio Agent Protocol v1 handshake state."""

from __future__ import annotations

from uuid import uuid4

from core.remote.capabilities import SUPPORTED_CAPABILITIES
from core.remote.schemas import AgentInfo

PROTOCOL_VERSION = "1"
AGENT_VERSION = "0.1.0"


class AgentProtocol:
    """Own the immutable handshake identity for one running API application."""

    def __init__(self, *, instance_id: str | None = None) -> None:
        resolved_instance_id = instance_id or f"studio-{uuid4().hex}"
        self._info = AgentInfo(
            protocol_version=PROTOCOL_VERSION,
            agent_version=AGENT_VERSION,
            instance_id=resolved_instance_id,
            capabilities=SUPPORTED_CAPABILITIES,
        )

    def info(self) -> AgentInfo:
        """Return a copy so callers cannot mutate the stored handshake state."""

        return self._info.model_copy(deep=True)


__all__ = ["AGENT_VERSION", "PROTOCOL_VERSION", "AgentProtocol"]
