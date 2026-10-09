-- Company Workhub: work items, progress reports, and the policy gaps left open by 0001.
-- Apply only to the dedicated workhub database, never to the product-center database.
--
-- Requires 0001 to be applied first.

CREATE SCHEMA IF NOT EXISTS workhub;
SET LOCAL search_path = workhub, public;

-- ---------------------------------------------------------------------------
-- 1. Work items
--
-- A work item is attached to a position, never to a member. This is what makes
-- handover automatic when someone leaves: the item stays with the position and
-- follows whoever holds it next. Attaching it to a member re-creates the
-- "they left, nobody owns it" problem this system exists to prevent.
-- ---------------------------------------------------------------------------

CREATE TABLE work_items (
    work_item_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_unit_id    uuid NOT NULL REFERENCES org_units(org_unit_id) ON DELETE RESTRICT,
    position_id    uuid NOT NULL REFERENCES positions(position_id) ON DELETE RESTRICT,
    title          text NOT NULL CHECK (btrim(title) <> ''),
    kind           text NOT NULL DEFAULT 'routine'
                   CHECK (kind IN ('project', 'routine', 'adhoc')),
    status         text NOT NULL DEFAULT 'planned'
                   CHECK (status IN ('planned', 'in_progress', 'blocked', 'done', 'cancelled')),
    blocked_reason text,
    needs_decision boolean NOT NULL DEFAULT false,
    created_by     uuid REFERENCES workforce_members(member_id) ON DELETE RESTRICT,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    -- A blocked item with no stated reason is useless on a board: management sees
    -- "blocked" and cannot act on it. The reason is what makes the board actionable.
    CHECK (status <> 'blocked' OR (blocked_reason IS NOT NULL AND btrim(blocked_reason) <> ''))
);

CREATE INDEX work_items_org_unit_idx ON work_items(org_unit_id, status);
CREATE INDEX work_items_position_idx ON work_items(position_id) WHERE status NOT IN ('done', 'cancelled');

-- Keep the denormalised org_unit_id honest. The board aggregates by department, so
-- a work item whose org_unit disagrees with its position's org_unit would show up
-- in the wrong department's board with no visible cause.
CREATE FUNCTION work_items_position_org_unit_matches() RETURNS trigger AS $$
DECLARE
    position_org_unit uuid;
BEGIN
    SELECT p.org_unit_id INTO position_org_unit
      FROM workhub.positions p
     WHERE p.position_id = NEW.position_id;

    IF position_org_unit IS NULL THEN
        RAISE EXCEPTION 'work_items.position_id % does not reference an existing position', NEW.position_id;
    END IF;

    IF position_org_unit IS NOT NULL AND position_org_unit <> NEW.org_unit_id THEN
        RAISE EXCEPTION 'work_items.org_unit_id % disagrees with position org_unit %', NEW.org_unit_id, position_org_unit;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER work_items_org_unit_consistency
    BEFORE INSERT OR UPDATE OF org_unit_id, position_id ON work_items
    FOR EACH ROW EXECUTE FUNCTION work_items_position_org_unit_matches();

-- ---------------------------------------------------------------------------
-- 2. Progress reports
--
-- One row per submission, append-only. A retraction marks the row instead of
-- deleting it: "the employee can retract" must not become "the history can be
-- rewritten".
-- ---------------------------------------------------------------------------

CREATE TABLE work_item_reports (
    report_id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    work_item_id           uuid NOT NULL REFERENCES work_items(work_item_id) ON DELETE RESTRICT,
    reported_by_member_id  uuid NOT NULL REFERENCES workforce_members(member_id) ON DELETE RESTRICT,
    -- human: typed by the member. agent: submitted by the member's own AI.
    -- The board shows this. It is not a trust signal, it is a provenance signal:
    -- when a number turns out to be wrong, we need to know how it was produced.
    submitted_via          text NOT NULL CHECK (submitted_via IN ('human', 'agent')),
    -- Nullable: a report may carry only a note. Boards must render "no value"
    -- honestly rather than defaulting to 0.
    progress_value         smallint CHECK (progress_value IS NULL OR progress_value BETWEEN 0 AND 100),
    note                   text,
    evidence               text NOT NULL CHECK (btrim(evidence) <> ''),
    status                 text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retracted')),
    retracted_by_member_id uuid REFERENCES workforce_members(member_id) ON DELETE RESTRICT,
    retracted_at           timestamptz,
    reported_at            timestamptz NOT NULL DEFAULT now(),
    CHECK ((status = 'active' AND retracted_at IS NULL AND retracted_by_member_id IS NULL)
        OR (status = 'retracted' AND retracted_at IS NOT NULL AND retracted_by_member_id IS NOT NULL))
);

CREATE INDEX work_item_reports_item_idx ON work_item_reports(work_item_id, reported_at DESC) WHERE status = 'active';
CREATE INDEX work_item_reports_reporter_idx ON work_item_reports(reported_by_member_id, reported_at DESC);

CREATE VIEW work_item_current_state AS
SELECT DISTINCT ON (r.work_item_id)
       r.work_item_id,
       r.report_id             AS latest_report_id,
       r.reported_by_member_id AS latest_reported_by_member_id,
       r.submitted_via         AS latest_submitted_via,
       r.progress_value        AS latest_progress_value,
       r.note                  AS latest_note,
       r.evidence              AS latest_evidence,
       r.reported_at           AS latest_reported_at
  FROM work_item_reports r
 WHERE r.status = 'active'
 ORDER BY r.work_item_id, r.reported_at DESC, r.report_id DESC;

-- ---------------------------------------------------------------------------
-- 3. Gap 1: at most one primary holder per position at any time
--
-- 0001 only prevented the same member from holding the same position twice in an
-- overlapping window. It did not stop two different members from both being
-- 'primary' on one position. Decision 1 requires at most one.
-- ---------------------------------------------------------------------------

ALTER TABLE position_assignments
    ADD CONSTRAINT position_assignments_single_primary_per_position
    EXCLUDE USING gist (
        position_id WITH =,
        tstzrange(starts_at, ends_at, '[)') WITH &&
    ) WHERE (assignment_type = 'primary' AND status IN ('scheduled', 'active'));

-- ---------------------------------------------------------------------------
-- 4. Gap 2: acting assignments are capped at 90 days
--
-- Decision 2 caps acting assignments at 90 days and forbids extending one by
-- editing ends_at; a renewal is a new assignment row. The cap is enforced here;
-- the "do not edit ends_at" half is an application rule enforced by audit.
-- ---------------------------------------------------------------------------

ALTER TABLE position_assignments
    ADD CONSTRAINT position_assignments_acting_max_90_days
    CHECK (assignment_type <> 'acting' OR ends_at <= starts_at + interval '90 days');

-- ---------------------------------------------------------------------------
-- 5. Gap 3: approval records, so dual sign-off is representable
--
-- 0001 stored a single approved_by column on assignments and grants, which cannot
-- express two signatures. This table records one row per approver per decision.
--
-- What the database enforces: one vote per person per subject.
-- What it deliberately does not enforce: that two specific roles both approved.
-- The database does not know who is a department head or who is in HR, so the
-- "both signatures present" rule belongs to the application layer, which reads
-- these rows. Do not move that knowledge into a constraint.
-- ---------------------------------------------------------------------------

CREATE TABLE approval_records (
    approval_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    subject_type       text NOT NULL CHECK (subject_type IN ('position_assignment', 'permission_grant', 'position_config_version')),
    subject_id         uuid NOT NULL,
    approver_member_id uuid NOT NULL REFERENCES workforce_members(member_id) ON DELETE RESTRICT,
    approver_role      text NOT NULL CHECK (approver_role IN ('department_head', 'hr', 'admin')),
    decision           text NOT NULL CHECK (decision IN ('approved', 'rejected')),
    comment            text,
    decided_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (subject_type, subject_id, approver_member_id)
);

CREATE INDEX approval_records_subject_idx ON approval_records(subject_type, subject_id, decided_at DESC);

-- ---------------------------------------------------------------------------
-- 6. Gap 4: sensitive field registry
--
-- Field-level masking was left as an application convention in 0001 with no
-- definition of which fields are sensitive. This registry makes it data-driven so
-- the rule can be reviewed without a code change.
--
-- Convention: access is authorised as resource 'employee.sensitive_field' with
-- action 'read', and the requested field_key is carried in the grant's
-- scope_definition. Every such read is written to workforce_audit_events.
-- ---------------------------------------------------------------------------

CREATE TABLE sensitive_field_registry (
    field_key               text PRIMARY KEY,
    label                   text NOT NULL,
    description             text,
    requires_explicit_grant boolean NOT NULL DEFAULT true,
    created_at              timestamptz NOT NULL DEFAULT now()
);

INSERT INTO sensitive_field_registry (field_key, label, description) VALUES
    ('identity_card',   '身份证',   '身份证号及证件影像'),
    ('bank_account',    '银行卡',   '工资卡号与开户信息'),
    ('salary',          '薪资',     '薪资结构、调薪记录与发放明细'),
    ('contract',        '合同',     '劳动合同与附件'),
    ('performance',     '绩效',     '考核结果与绩效面谈记录'),
    ('onboarding_docs', '入职材料', '入职登记表、学历与背调材料');
