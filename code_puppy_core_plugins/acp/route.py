"""Session routes: which agent and model one ACP session actually runs on.

Each ACP session owns a ``SessionRoute`` -- an agent name, a model id and an
epoch. The route is *evidence, not intent*: it is read back from the agent
that was built, never taken from the request, so a failed switch keeps
reporting the old, still-effective route.

Every session response carries the route under ``_meta.codePuppyRoute``, and
``initialize`` advertises the contract under
``agentInfo._meta.codePuppySessionRoute`` so a client can tell this agent
apart from a generic ACP agent before relying on it.

Epochs let a client order route reports without trusting arrival order: a
session opens at 1, a real change advances it by one, and re-selecting the
current value reuses it. A lower epoch is stale; the same epoch with
different values is a conflict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

CONTRACT_VERSION = 1
META_KEY = "codePuppyRoute"
CAPABILITY_META_KEY = "codePuppySessionRoute"

# How ``session/load`` treats a session persisted before routes were recorded:
# it opens on the process default route, which is then reported and saved.
LEGACY_LOAD_POLICY = "default_route"


def capability_metadata() -> Dict[str, Any]:
    """Describe the route contract for ``agentInfo._meta`` at ``initialize``."""
    return {
        CAPABILITY_META_KEY: {
            "version": CONTRACT_VERSION,
            "configOptionIds": {"agent": "agent", "model": "model"},
            "routeMetaKey": META_KEY,
            "legacyLoadPolicy": LEGACY_LOAD_POLICY,
        }
    }


@dataclass(frozen=True)
class SessionRoute:
    """The agent and model one ACP session runs on, with its epoch."""

    agent_name: str
    model_id: str
    epoch: int = 1

    def changed(
        self, *, agent_name: Optional[str] = None, model_id: Optional[str] = None
    ) -> "SessionRoute":
        """Return the route after a change, one epoch later."""
        return SessionRoute(
            agent_name=agent_name or self.agent_name,
            model_id=model_id or self.model_id,
            epoch=self.epoch + 1,
        )

    def same_binding(self, other: "SessionRoute") -> bool:
        """True when ``other`` names the same agent and model."""
        return (self.agent_name, self.model_id) == (other.agent_name, other.model_id)

    def payload(self, session_id: str) -> Dict[str, Any]:
        """The wire form reported to the client."""
        return {
            "version": CONTRACT_VERSION,
            "sessionId": session_id,
            "agentId": self.agent_name,
            "modelId": self.model_id,
            "routeEpoch": self.epoch,
        }

    def metadata(self, session_id: str) -> Dict[str, Any]:
        """The ``_meta`` value for a session response or update."""
        return {META_KEY: self.payload(session_id)}

    def persisted_payload(self) -> Dict[str, Any]:
        """The form stored in the session's ACP metadata sidecar."""
        return {
            "version": CONTRACT_VERSION,
            "agent_id": self.agent_name,
            "model_id": self.model_id,
            "route_epoch": self.epoch,
        }

    @classmethod
    def from_persisted(cls, value: Any) -> Optional["SessionRoute"]:
        """Read a stored route, or ``None`` when it is missing or malformed."""
        if not isinstance(value, dict):
            return None
        if value.get("version") != CONTRACT_VERSION:
            return None
        agent_name = value.get("agent_id")
        model_id = value.get("model_id")
        epoch = value.get("route_epoch")
        if not isinstance(agent_name, str) or not agent_name.strip():
            return None
        if not isinstance(model_id, str) or not model_id.strip():
            return None
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
            return None
        return cls(agent_name=agent_name, model_id=model_id, epoch=epoch)


class RouteUnavailable(ValueError):
    """A persisted session's stored route is present but unreadable."""


def expected_epoch(kwargs: Dict[str, Any]) -> Optional[int]:
    """Read an optional ``expectedRouteEpoch`` from a request's ``_meta``.

    A client sends it with a switch so the switch is refused if the route
    changed since the client last saw it.
    """
    metadata = kwargs.get("field_meta") or kwargs.get("_meta")
    if not isinstance(metadata, dict):
        return None
    route_meta = metadata.get(META_KEY)
    if not isinstance(route_meta, dict):
        return None
    value = route_meta.get("expectedRouteEpoch")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value
