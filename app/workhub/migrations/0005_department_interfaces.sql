-- Company Workhub: department-authored interfaces (the evolving base layer).
-- Apply only to the dedicated workhub database, never to the product-center database.
-- Requires 0002 (work items) and 0004 (positions/org units).
--
-- Why this exists: departments draw their own screens through the Skill, but the
-- base has to remember what they drew. Two tables, deliberately split:
--   interfaces   -- the drawing itself (a validated descriptor), versioned per department
--   interface_entries -- what somebody actually typed into it
--
-- The split matters. A descriptor is company-shaped knowledge and is audited and
-- reviewed; an entry is one person's report and is owned by the person who filled
-- it in. Mixing them would let a department redraw the screen and silently
-- rewrite history under reports people already signed.
CREATE SCHEMA IF NOT EXISTS workhub;
SET LOCAL search_path = workhub, public;

CREATE TABLE interfaces (
    interface_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_unit_id      uuid NOT NULL REFERENCES org_units(org_unit_id) ON DELETE CASCADE,
    -- Stable slug from the descriptor, unique per department: a department reruns
    -- its own screen, it does not fork a copy every time the AI regenerates it.
    slug             text NOT NULL,
    name             text NOT NULL,
    -- A department may have many drafts; at most one is live. Enforced below.
    status           text NOT NULL DEFAULT 'draft'
                     CHECK (status IN ('draft', 'active', 'archived')),
    visibility       text NOT NULL DEFAULT 'department'
                     CHECK (visibility IN ('department', 'public')),
    schema_version   text NOT NULL,
    -- The descriptor itself. It is data, never code; workhub.descriptor rejects
    -- anything carrying logic or credentials before it reaches this column.
    descriptor       jsonb NOT NULL,
    -- Where this screen came from, so a good department idea can be traced and
    -- promoted instead of being copied into a corner.
    based_on         uuid REFERENCES interfaces(interface_id),
    version_no       integer NOT NULL DEFAULT 1,
    -- Who may fill this in: positions, not people, so a handover needs no edit.
    position_id      uuid REFERENCES positions(position_id),
    created_by       uuid REFERENCES workforce_members(member_id),
    updated_by       uuid REFERENCES workforce_members(member_id),
    -- Public templates are the only thing that needs a human gate.
    reviewed_by      uuid REFERENCES workforce_members(member_id),
    reviewed_at      timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_unit_id, slug, version_no),
    CHECK ((visibility <> 'public') OR (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)),
    CHECK ((status <> 'active') OR (visibility = 'department' OR reviewed_by IS NOT NULL))
);

-- One live screen per department slug. Drafts are free; going live is the act
-- that has to be unambiguous, otherwise the aggregate cannot tell which drawing
-- produced the numbers it is showing.
CREATE UNIQUE INDEX interfaces_one_active_idx
    ON interfaces(org_unit_id, slug) WHERE status = 'active';

CREATE INDEX interfaces_org_idx ON interfaces(org_unit_id, status);
CREATE INDEX interfaces_public_idx ON interfaces(visibility) WHERE visibility = 'public';

CREATE TABLE interface_entries (
    entry_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    interface_id     uuid NOT NULL REFERENCES interfaces(interface_id) ON DELETE CASCADE,
    -- The report is tied to the work item when the screen produces one. Null means
    -- the department is tracking something that has no work item yet; the entry is
    -- still real, but it cannot reach the company aggregate until it is linked.
    work_item_id     uuid REFERENCES work_items(work_item_id) ON DELETE SET NULL,
    -- Standard fields are columns, not JSON, because the aggregate reads them for
    -- every department and must not parse arbitrary shapes to answer "what is stuck".
    title            text,
    status           text CHECK (status IS NULL OR status IN ('planned', 'in_progress', 'blocked', 'done')),
    blocked_reason   text,
    needs_decision   boolean NOT NULL DEFAULT false,
    progress_value   integer CHECK (progress_value IS NULL OR progress_value BETWEEN 0 AND 100),
    note             text,
    evidence         text,
    -- Everything the department invented that did not map to a standard field.
    -- Kept, because it is their work; never aggregated, because it means nothing
    -- outside this department. The nightly scan reads this column to decide what
    -- deserves to become standard.
    extra            jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- Who stood behind this, and whether their AI typed it.
    reported_by      uuid NOT NULL REFERENCES workforce_members(member_id),
    submitted_via    text NOT NULL DEFAULT 'human' CHECK (submitted_via IN ('human', 'agent')),
    reported_at      timestamptz NOT NULL DEFAULT now(),
    -- Retraction marks, never deletes: a wrong report has to stay visible as a
    -- wrong report, or the audit trail becomes a story about a perfect company.
    status_entry     text NOT NULL DEFAULT 'active' CHECK (status_entry IN ('active', 'retracted')),
    retracted_by     uuid REFERENCES workforce_members(member_id),
    retracted_at     timestamptz,
    CHECK (evidence IS NULL OR btrim(evidence) <> ''),
    CHECK ((status_entry <> 'retracted') OR (retracted_by IS NOT NULL AND retracted_at IS NOT NULL)),
    CHECK ((status IS NULL) OR (status <> 'blocked') OR (blocked_reason IS NOT NULL AND btrim(blocked_reason) <> ''))
);

CREATE INDEX interface_entries_interface_idx ON interface_entries(interface_id, reported_at DESC);
CREATE INDEX interface_entries_work_item_idx ON interface_entries(work_item_id);
CREATE INDEX interface_entries_reporter_idx ON interface_entries(reported_by);
-- The nightly evolution scan: which unmapped keys are actually being used.
CREATE INDEX interface_entries_extra_idx ON interface_entries USING gin (extra);
