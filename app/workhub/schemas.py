from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class MemberCreate(BaseModel):
    employee_code: str | None = None
    display_name: str = Field(min_length=1, max_length=200)
    joined_at: date | None = None


class PositionCreate(BaseModel):
    position_code: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=200)
    org_unit_id: UUID


class AssignmentCreate(BaseModel):
    member_id: UUID
    position_id: UUID
    assignment_type: Literal["primary", "concurrent", "acting"]
    starts_at: datetime
    ends_at: datetime | None = None
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_times(self):
        if self.starts_at.tzinfo is None or self.starts_at.utcoffset() is None:
            raise ValueError("starts_at must include a timezone")
        if self.ends_at is not None and (self.ends_at.tzinfo is None or self.ends_at.utcoffset() is None):
            raise ValueError("ends_at must include a timezone")
        if self.assignment_type == "acting" and self.ends_at is None:
            raise ValueError("acting assignments require ends_at")
        return self


class GrantCreate(BaseModel):
    subject_type: Literal["position", "member"]
    position_id: UUID | None = None
    member_id: UUID | None = None
    resource: str = Field(min_length=1, max_length=120)
    action: str = Field(min_length=1, max_length=80)
    scope_type: Literal["self_position", "org_unit", "selected_positions", "company_aggregate", "all"]
    scope_org_unit_id: UUID | None = None
    scope_definition: dict[str, Any] = Field(default_factory=dict)
    effect: Literal["allow", "deny"] = "allow"
    valid_from: datetime
    valid_until: datetime | None = None
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def enforce_subject_and_scope(self):
        if self.valid_from.tzinfo is None or self.valid_from.utcoffset() is None:
            raise ValueError("valid_from must include a timezone")
        if self.valid_until is not None and (self.valid_until.tzinfo is None or self.valid_until.utcoffset() is None):
            raise ValueError("valid_until must include a timezone")
        if self.subject_type == "position" and (self.position_id is None or self.member_id is not None):
            raise ValueError("position grants require only position_id")
        if self.subject_type == "member" and (self.member_id is None or self.position_id is not None):
            raise ValueError("member grants require only member_id")
        if self.subject_type == "member" and self.valid_until is None:
            raise ValueError("member exception grants require valid_until")
        if self.scope_type == "org_unit" and self.scope_org_unit_id is None:
            raise ValueError("org_unit scope requires scope_org_unit_id")
        return self


class OrgUnitCreate(BaseModel):
    org_code: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=200)
    parent_org_unit_id: UUID | None = None
    unit_type: Literal["company", "department", "team", "group"] = "department"


class PositionConfigCreate(BaseModel):
    effective_from: datetime
    config: dict[str, Any] = Field(default_factory=dict)
    based_on_version_id: UUID | None = None
    change_reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def timezone_required(self):
        if self.effective_from.tzinfo is None or self.effective_from.utcoffset() is None:
            raise ValueError("effective_from must include a timezone")
        return self


class MemberStatusUpdate(BaseModel):
    employment_status: Literal["active", "suspended", "departed"]
    reason: str = Field(min_length=1, max_length=1000)


WORK_ITEM_STATUS = Literal["planned", "in_progress", "blocked", "done", "cancelled"]


class WorkItemCreate(BaseModel):
    # Only the position is supplied. The department is derived from the position,
    # because a work item whose department disagrees with its position's department
    # would appear on the wrong board for no visible reason (see migration 0002).
    position_id: UUID
    title: str = Field(min_length=1, max_length=300)
    kind: Literal["project", "routine", "adhoc"] = "routine"
    status: WORK_ITEM_STATUS = "planned"
    blocked_reason: str | None = None
    needs_decision: bool = False

    @model_validator(mode="after")
    def blocked_reason_required(self):
        if self.status == "blocked" and not (self.blocked_reason and self.blocked_reason.strip()):
            raise ValueError("blocked work items require a blocked_reason")
        return self


class WorkItemUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    status: WORK_ITEM_STATUS | None = None
    blocked_reason: str | None = None
    needs_decision: bool | None = None


class ReportCreate(BaseModel):
    # 'human' or 'agent' is provenance, not trust: when a number turns out wrong we
    # need to know how it was produced. The reporter is always the authenticated
    # member; an AI is the member's pen, so the member stays accountable.
    submitted_via: Literal["human", "agent"]
    progress_value: int | None = Field(default=None, ge=0, le=100)
    note: str | None = None
    evidence: str = Field(min_length=1, max_length=4000)


class ReportRetract(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class SpaceUpsert(BaseModel):
    """A room or a desk on the floor plan.

    Geometry is entered by hand against the actual floor, so it carries a
    `calibrated` flag: false means nobody has checked these numbers yet, and the
    plan draws it dashed rather than pretending it reflects the office.
    """
    kind: Literal["zone", "desk"]
    org_unit_id: UUID
    position_id: UUID | None = None
    label: str | None = None
    x: int = Field(ge=0, le=999)
    y: int = Field(ge=0, le=999)
    w: int = Field(gt=0, le=1000)
    h: int = Field(gt=0, le=1000)
    calibrated: bool = False
    sort_order: int = 0

    @model_validator(mode="after")
    def geometry_and_reference(self):
        if self.x + self.w > 1000 or self.y + self.h > 1000:
            raise ValueError("space does not fit on the 1000x1000 floor canvas")
        if self.kind == "desk" and self.position_id is None:
            raise ValueError("a desk needs a position")
        if self.kind == "zone" and self.position_id is not None:
            raise ValueError("a zone belongs to a department, not a position")
        return self
