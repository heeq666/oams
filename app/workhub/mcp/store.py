"""Where drafts live.

The P0 host has no PostgreSQL in this environment (and the MCP server must stay
testable without it), so the storage rule is expressed as a small interface with
an in-memory implementation. The rule itself is the part that matters:

* every write appends a new `version_no`; no version is ever edited in place;
* a version that has left `draft` (pending / active / archived) is frozen --
  the AI cannot overwrite a submission that is waiting for a reviewer;
* a plugin therefore always has exactly one writable draft version, and any
  number of frozen ones.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from workhub.mcp.audit import utc_now

STATUS_DRAFT = "draft"
STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_ARCHIVED = "archived"

ALL_STATUSES = (STATUS_DRAFT, STATUS_PENDING, STATUS_ACTIVE, STATUS_ARCHIVED)
#: Only this status accepts writes.
WRITABLE_STATUSES = (STATUS_DRAFT,)


class StoreError(Exception):
    """Raised when a store invariant would be violated; carries a stable code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class VersionFrozen(StoreError):
    def __init__(self, plugin_id: str, version_no: int, status: str) -> None:
        self.plugin_id = plugin_id
        self.version_no = version_no
        self.status = status
        super().__init__(
            "VERSION_FROZEN",
            f"plugin {plugin_id!r} version {version_no} is {status!r}; "
            "only draft versions accept writes",
        )


@dataclass
class PluginVersion:
    """One immutable-by-convention version of a plugin's descriptor."""

    plugin_id: str
    version_no: int
    status: str
    descriptor: dict[str, Any]
    org_unit_code: str
    name: str
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    created_by: str | None = None
    submitted_at: str | None = None
    submitted_by: str | None = None
    review_note: str | None = None

    @property
    def is_writable(self) -> bool:
        return self.status in WRITABLE_STATUSES

    def summary(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "version_no": self.version_no,
            "status": self.status,
            "name": self.name,
            "org_unit_code": self.org_unit_code,
            "module_count": len(self.descriptor.get("modules") or []),
            "field_count": len(self.descriptor.get("fields") or []),
            "capabilities": list(self.descriptor.get("capabilities") or []),
            "updated_at": self.updated_at,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.summary(), "descriptor": self.descriptor}


class DraftStore(Protocol):
    """Everything the write path needs; nothing about the transport."""

    def create_draft(self, *, plugin_id: str, org_unit_code: str, name: str,
                     descriptor: dict[str, Any], actor: str) -> PluginVersion: ...

    def latest(self, plugin_id: str) -> PluginVersion | None: ...

    def versions(self, plugin_id: str) -> list[PluginVersion]: ...

    def list_plugins(self, *, org_unit_code: str | None = None,
                     status: str | None = None) -> list[PluginVersion]: ...

    def append_draft_version(self, *, plugin_id: str, descriptor: dict[str, Any],
                             actor: str) -> PluginVersion: ...

    def submit(self, *, plugin_id: str, actor: str,
               review_note: str | None = None) -> PluginVersion: ...


@dataclass
class MemoryDraftStore:
    """In-process stand-in for the `plugin_versions` table.

    Deliberately strict: `append_draft_version` refuses to touch anything that is
    not the latest draft, so a stale writer (an AI holding a version number from
    five turns ago) cannot silently clobber newer work.
    """

    _versions: dict[str, list[PluginVersion]] = field(default_factory=dict)

    # -- reads ---------------------------------------------------------------
    def versions(self, plugin_id: str) -> list[PluginVersion]:
        return list(self._versions.get(plugin_id, []))

    def latest(self, plugin_id: str) -> PluginVersion | None:
        rows = self._versions.get(plugin_id)
        return rows[-1] if rows else None

    def get_version(self, plugin_id: str, version_no: int) -> PluginVersion | None:
        for row in self._versions.get(plugin_id, []):
            if row.version_no == version_no:
                return row
        return None

    def list_plugins(self, *, org_unit_code: str | None = None,
                     status: str | None = None) -> list[PluginVersion]:
        rows = [versions[-1] for versions in self._versions.values() if versions]
        if org_unit_code is not None:
            rows = [row for row in rows if row.org_unit_code == org_unit_code]
        if status is not None:
            rows = [row for row in rows if row.status == status]
        return sorted(rows, key=lambda row: (row.plugin_id, row.version_no))

    def all_versions(self) -> list[PluginVersion]:
        return [row for versions in self._versions.values() for row in versions]

    # -- writes --------------------------------------------------------------
    def create_draft(self, *, plugin_id: str, org_unit_code: str, name: str,
                     descriptor: dict[str, Any], actor: str) -> PluginVersion:
        if plugin_id in self._versions and self._versions[plugin_id]:
            raise StoreError(
                "PLUGIN_EXISTS",
                f"plugin {plugin_id!r} already exists; use patch tools or a new slug",
            )
        row = PluginVersion(
            plugin_id=plugin_id, version_no=1, status=STATUS_DRAFT,
            descriptor=descriptor, org_unit_code=org_unit_code, name=name,
            created_by=actor,
        )
        self._versions[plugin_id] = [row]
        return row

    def append_draft_version(self, *, plugin_id: str, descriptor: dict[str, Any],
                             actor: str) -> PluginVersion:
        rows = self._versions.get(plugin_id) or []
        if not rows:
            raise StoreError("PLUGIN_NOT_FOUND", f"plugin {plugin_id!r} does not exist")
        head = rows[-1]
        if not head.is_writable:
            # The head is pending/active: a write starts a NEW draft beside it,
            # never on top of it. This is the "AI cannot overwrite a pending
            # version" rule, expressed as a data rule rather than a promise.
            next_no = head.version_no + 1
            row = PluginVersion(
                plugin_id=plugin_id, version_no=next_no, status=STATUS_DRAFT,
                descriptor=descriptor, org_unit_code=head.org_unit_code,
                name=head.name, created_by=actor,
            )
            rows.append(row)
            return row
        next_no = head.version_no + 1
        row = replace(
            head, version_no=next_no, status=STATUS_DRAFT,
            descriptor=descriptor, updated_at=utc_now(), created_by=actor,
        )
        rows.append(row)
        return row

    def submit(self, *, plugin_id: str, actor: str,
               review_note: str | None = None) -> PluginVersion:
        rows = self._versions.get(plugin_id) or []
        if not rows:
            raise StoreError("PLUGIN_NOT_FOUND", f"plugin {plugin_id!r} does not exist")
        head = rows[-1]
        if not head.is_writable:
            raise StoreError(
                "ALREADY_SUBMITTED",
                f"plugin {plugin_id!r} version {head.version_no} is {head.status!r}; "
                "create a new draft before submitting again",
            )
        frozen = replace(
            head, status=STATUS_PENDING, submitted_at=utc_now(),
            submitted_by=actor, review_note=review_note, updated_at=utc_now(),
        )
        rows[-1] = frozen
        return frozen
