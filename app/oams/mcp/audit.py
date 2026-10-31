"""The audit trail every MCP call lands in.

Concept doc §7.2: "每次工具调用进审计，标注 actor=agent + 会话 id". It is not an
afterthought -- it is the compensating control for a lightweight approval flow,
so it is written before the tool body runs: a call that crashes mid-way is still
visible as an attempt.

Kept as a tiny store interface so the stdio server can back it with Postgres
later while the tests use memory.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

ACTOR_AGENT = "agent"
ACTOR_HUMAN = "human"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class AuditEntry:
    """One tool invocation, as an auditor would want to read it."""

    seq: int
    at: str
    tool: str
    actor: str
    session_id: str | None
    subject: str
    plugin_id: str | None = None
    version_no: int | None = None
    ok: bool = True
    error_code: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "at": self.at,
            "tool": self.tool,
            "actor": self.actor,
            "session_id": self.session_id,
            "subject": self.subject,
            "plugin_id": self.plugin_id,
            "version_no": self.version_no,
            "ok": self.ok,
            "error_code": self.error_code,
            "detail": self.detail,
        }


class AuditStore(Protocol):
    def record(self, entry: AuditEntry) -> AuditEntry: ...

    def list_entries(self, *, plugin_id: str | None = None) -> list[AuditEntry]: ...


@dataclass
class MemoryAuditStore:
    """In-process audit log. Same shape the SQL table will have."""

    entries: list[AuditEntry] = field(default_factory=list)

    def record(self, entry: AuditEntry) -> AuditEntry:
        if entry.seq <= 0:
            entry.seq = len(self.entries) + 1
        self.entries.append(entry)
        return entry

    def append(self, *, tool: str, actor: str, session_id: str | None,
               subject: str, plugin_id: str | None = None,
               version_no: int | None = None, ok: bool = True,
               error_code: str | None = None,
               detail: str | None = None) -> AuditEntry:
        entry = AuditEntry(
            seq=len(self.entries) + 1, at=utc_now(), tool=tool, actor=actor,
            session_id=session_id, subject=subject, plugin_id=plugin_id,
            version_no=version_no, ok=ok, error_code=error_code, detail=detail,
        )
        return self.record(entry)

    def list_entries(self, *, plugin_id: str | None = None) -> list[AuditEntry]:
        if plugin_id is None:
            return list(self.entries)
        return [entry for entry in self.entries if entry.plugin_id == plugin_id]
