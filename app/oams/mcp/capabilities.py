"""Capability checks for the Workhub MCP server.

The rule this module exists to enforce: an AI token is bound to
`plugin:write:draft` and can never hold `plugin:approve`. Approving is a human
act; typing "please submit it" into a chat window must not turn it into an agent
act. Nothing here trusts the model to behave -- every write path calls
`Capability.require` before it touches the store.
"""
from __future__ import annotations

from dataclasses import dataclass

#: The one write capability an AI session ever gets.
CAP_PLUGIN_WRITE_DRAFT = "plugin:write:draft"
#: Reading existing plugins (including other departments' published ones).
CAP_PLUGIN_READ = "plugin:read"
#: Reviewing a submission. Humans get this; agent tokens must never ask for it.
CAP_PLUGIN_APPROVE = "plugin:approve"

#: Capabilities minted into every agent token, and nothing else.
AGENT_CAPABILITIES: frozenset[str] = frozenset({CAP_PLUGIN_READ, CAP_PLUGIN_WRITE_DRAFT})

#: Capabilities an agent token may never hold, even if a caller asks.
FORBIDDEN_FOR_AGENT: tuple[str, ...] = (CAP_PLUGIN_APPROVE,)

#: Capabilities a human reviewer holds.
HUMAN_CAPABILITIES: frozenset[str] = frozenset(
    {CAP_PLUGIN_READ, CAP_PLUGIN_WRITE_DRAFT, CAP_PLUGIN_APPROVE}
)

KIND_AGENT = "agent"
KIND_HUMAN = "human"

#: Actor recorded for every agent-originated write, per the concept doc (§7.2).
ACTOR_AGENT = "agent"


class CapabilityDenied(Exception):
    """Raised when a caller asks for a capability its token does not hold."""

    def __init__(self, subject: str, capability: str) -> None:
        self.subject = subject
        self.capability = capability
        super().__init__(f"capability {capability!r} is not granted to {subject!r}")


@dataclass(frozen=True)
class Capability:
    """A token: who is acting, of what kind, and what it may do.

    `kind` is the load-bearing field. `submit_for_review` refuses to run for an
    agent token entirely, so the AI cannot promote its own draft even if a bug
    let it forge a `human_confirmed` flag -- and it can never approve.
    """

    subject: str
    caps: frozenset[str]
    kind: str = KIND_AGENT
    session_id: str | None = None

    def can(self, capability: str) -> bool:
        return capability in self.caps

    def require(self, capability: str) -> None:
        if not self.can(capability):
            raise CapabilityDenied(self.subject, capability)

    @property
    def is_human(self) -> bool:
        return self.kind == KIND_HUMAN


def issue_agent_capability(session_id: str, *,
                           extra: tuple[str, ...] = ()) -> Capability:
    """Mint the token an MCP session gets.

    `extra` exists only so tests (and, later, an explicitly configured read-only
    deployment) can add non-privileged scopes. Asking for a forbidden capability
    raises instead of being silently dropped: a request for `plugin:approve`
    is a bug or an attack, and both should surface loudly.
    """
    if not session_id:
        raise ValueError("an agent session id is required for the audit trail")
    for requested in extra:
        if requested in FORBIDDEN_FOR_AGENT:
            raise CapabilityDenied(session_id, requested)
    caps = AGENT_CAPABILITIES | frozenset(extra)
    # Belt and braces: no code path may widen an agent token to approve.
    caps = caps - frozenset(FORBIDDEN_FOR_AGENT)
    return Capability(subject=session_id, caps=caps, kind=KIND_AGENT,
                      session_id=session_id)


def issue_human_capability(subject: str, *,
                           session_id: str | None = None) -> Capability:
    """The token of the person who clicked "submit" in the UI."""
    if not subject:
        raise ValueError("a human actor is required: submission is a human act")
    return Capability(subject=subject, caps=HUMAN_CAPABILITIES,
                      kind=KIND_HUMAN, session_id=session_id)


def ensure_human(capability: Capability, *, what: str) -> None:
    """Refuse unless a person -- not an agent token -- is the one acting."""
    if not capability.is_human:
        raise CapabilityDenied(capability.subject, CAP_PLUGIN_APPROVE)
    if capability.session_id and capability.subject == capability.session_id:
        raise CapabilityDenied(capability.subject, CAP_PLUGIN_APPROVE)
