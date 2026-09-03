"""Public surface for CreativeStudio remote agent protocol primitives."""

from core.remote.capabilities import AgentCapability, SUPPORTED_CAPABILITIES
from core.remote.protocol import AGENT_VERSION, PROTOCOL_VERSION, AgentProtocol
from core.remote.schemas import AgentInfo

__all__ = [
    "AGENT_VERSION",
    "PROTOCOL_VERSION",
    "AgentCapability",
    "AgentInfo",
    "AgentProtocol",
    "SUPPORTED_CAPABILITIES",
]
