-- Company Workhub workforce identity, position, assignment, permission and audit foundation.
-- Apply only to the dedicated workhub database, never to the product-center database.


CREATE SCHEMA IF NOT EXISTS workhub;
CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA workhub;
CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA workhub;
SET LOCAL search_path = workhub, public;

CREATE TABLE schema_migrations (
    version text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE org_units (
    org_unit_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    parent_org_unit_id uuid REFERENCES org_units(org_unit_id) ON DELETE RESTRICT,
    org_code          text NOT NULL UNIQUE,
    name              text NOT NULL,
    unit_type         text NOT NULL DEFAULT 'department'
                      CHECK (unit_type IN ('company', 'department', 'team', 'group')),
    status            text NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active', 'inactive')),
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CHECK (parent_org_unit_id IS NULL OR parent_org_unit_id <> org_unit_id)
);

CREATE TABLE org_unit_external_refs (
    external_ref_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_unit_id     uuid NOT NULL REFERENCES org_units(org_unit_id) ON DELETE RESTRICT,
    source_system   text NOT NULL,
    external_id     text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_system, external_id),
    UNIQUE (org_unit_id, source_system)
);

CREATE TABLE workforce_members (
    member_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    employee_code   text UNIQUE,
    display_name    text NOT NULL,
    employment_status text NOT NULL DEFAULT 'pending'
                      CHECK (employment_status IN ('pending', 'active', 'suspended', 'departed')),
    token_version   bigint NOT NULL DEFAULT 1 CHECK (token_version > 0),
    joined_at       date,
    departed_at     date,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CHECK (departed_at IS NULL OR joined_at IS NULL OR departed_at >= joined_at),
    CHECK (employment_status <> 'departed' OR departed_at IS NOT NULL)
);

CREATE TABLE member_external_identities (
    identity_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    member_id     uuid NOT NULL REFERENCES workforce_members(member_id) ON DELETE RESTRICT,
    provider      text NOT NULL,
    tenant_id     text NOT NULL,
    subject_id    text NOT NULL,
    status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
    linked_at     timestamptz NOT NULL DEFAULT now(),
    revoked_at    timestamptz,
    CHECK ((status = 'active' AND revoked_at IS NULL) OR (status = 'revoked' AND revoked_at IS NOT NULL))
);
CREATE UNIQUE INDEX member_external_identities_active_subject_idx
    ON member_external_identities(provider, tenant_id, subject_id) WHERE status = 'active';
CREATE INDEX member_external_identities_member_idx ON member_external_identities(member_id);

CREATE TABLE positions (
    position_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    position_code text NOT NULL UNIQUE,
    title         text NOT NULL,
    org_unit_id   uuid NOT NULL REFERENCES org_units(org_unit_id) ON DELETE RESTRICT,
    status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive', 'retired')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX positions_org_unit_idx ON positions(org_unit_id, status);

CREATE TABLE position_config_versions (
    config_version_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    position_id       uuid NOT NULL REFERENCES positions(position_id) ON DELETE RESTRICT,
    version_no        integer NOT NULL CHECK (version_no > 0),
    status            text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'scheduled', 'active', 'superseded', 'withdrawn')),
    effective_from    timestamptz,
    effective_to      timestamptz,
    based_on_version_id uuid REFERENCES position_config_versions(config_version_id) ON DELETE RESTRICT,
    config            jsonb NOT NULL DEFAULT '{}'::jsonb,
    change_reason     text NOT NULL,
    created_by        uuid REFERENCES workforce_members(member_id) ON DELETE RESTRICT,
    approved_by       uuid REFERENCES workforce_members(member_id) ON DELETE RESTRICT,
    approved_at       timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (position_id, version_no),
    CHECK (effective_to IS NULL OR effective_from IS NULL OR effective_to > effective_from),
    CHECK (status NOT IN ('active', 'scheduled') OR effective_from IS NOT NULL),
    CHECK (approved_at IS NULL OR approved_by IS NOT NULL)
);
CREATE INDEX position_config_versions_effective_idx ON position_config_versions(position_id, status, effective_from DESC);

CREATE TABLE position_assignments (
    assignment_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    member_id     uuid NOT NULL REFERENCES workforce_members(member_id) ON DELETE RESTRICT,
    position_id   uuid NOT NULL REFERENCES positions(position_id) ON DELETE RESTRICT,
    assignment_type text NOT NULL CHECK (assignment_type IN ('primary', 'concurrent', 'acting')),
    status        text NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled', 'active', 'ended', 'cancelled')),
    starts_at     timestamptz NOT NULL,
    ends_at       timestamptz,
    approved_by   uuid REFERENCES workforce_members(member_id) ON DELETE RESTRICT,
    approved_at   timestamptz,
    reason        text NOT NULL,
    created_by    uuid REFERENCES workforce_members(member_id) ON DELETE RESTRICT,
    created_at    timestamptz NOT NULL DEFAULT now(),
    ended_by      uuid REFERENCES workforce_members(member_id) ON DELETE RESTRICT,
    ended_at      timestamptz,
    CHECK (ends_at IS NULL OR ends_at > starts_at),
    CHECK (assignment_type <> 'acting' OR ends_at IS NOT NULL),
    CHECK ((status = 'ended' AND ended_at IS NOT NULL) OR (status <> 'ended' AND ended_at IS NULL)),
    CHECK (approved_at IS NULL OR approved_by IS NOT NULL)
);
CREATE INDEX position_assignments_member_active_idx ON position_assignments(member_id, starts_at, ends_at) WHERE status IN ('scheduled', 'active');
CREATE INDEX position_assignments_position_active_idx ON position_assignments(position_id, starts_at, ends_at) WHERE status IN ('scheduled', 'active');

CREATE TABLE permission_grants (
    grant_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    subject_type   text NOT NULL CHECK (subject_type IN ('position', 'member')),
    position_id    uuid REFERENCES positions(position_id) ON DELETE RESTRICT,
    member_id      uuid REFERENCES workforce_members(member_id) ON DELETE RESTRICT,
    resource       text NOT NULL,
    action         text NOT NULL,
    scope_type     text NOT NULL CHECK (scope_type IN ('self_position', 'org_unit', 'selected_positions', 'company_aggregate', 'all')),
    scope_org_unit_id uuid REFERENCES org_units(org_unit_id) ON DELETE RESTRICT,
    scope_definition jsonb NOT NULL DEFAULT '{}'::jsonb,
    effect         text NOT NULL DEFAULT 'allow' CHECK (effect IN ('allow', 'deny')),
    valid_from     timestamptz NOT NULL DEFAULT now(),
    valid_until    timestamptz,
    revoked_at     timestamptz,
    reason         text NOT NULL,
    source_assignment_id uuid REFERENCES position_assignments(assignment_id) ON DELETE RESTRICT,
    approved_by    uuid REFERENCES workforce_members(member_id) ON DELETE RESTRICT,
    approved_at    timestamptz,
    created_by     uuid REFERENCES workforce_members(member_id) ON DELETE RESTRICT,
    created_at     timestamptz NOT NULL DEFAULT now(),
    CHECK ((subject_type = 'position' AND position_id IS NOT NULL AND member_id IS NULL)
        OR (subject_type = 'member' AND member_id IS NOT NULL AND position_id IS NULL)),
    CHECK (valid_until IS NULL OR valid_until > valid_from),
    CHECK (subject_type <> 'member' OR valid_until IS NOT NULL),
    CHECK (scope_type <> 'org_unit' OR scope_org_unit_id IS NOT NULL),
    CHECK (approved_at IS NULL OR approved_by IS NOT NULL)
);
CREATE INDEX permission_grants_position_lookup_idx ON permission_grants(position_id, resource, action, valid_from, valid_until) WHERE revoked_at IS NULL AND subject_type = 'position';
CREATE INDEX permission_grants_member_lookup_idx ON permission_grants(member_id, resource, action, valid_from, valid_until) WHERE revoked_at IS NULL AND subject_type = 'member';

CREATE TABLE workforce_sessions (
    session_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    member_id        uuid NOT NULL REFERENCES workforce_members(member_id) ON DELETE RESTRICT,
    token_digest     bytea NOT NULL UNIQUE,
    token_version   bigint NOT NULL,
    issued_at        timestamptz NOT NULL DEFAULT now(),
    expires_at       timestamptz NOT NULL,
    revoked_at       timestamptz,
    auth_provider    text NOT NULL,
    CHECK (expires_at > issued_at),
    CHECK (token_version > 0)
);
CREATE INDEX workforce_sessions_member_live_idx ON workforce_sessions(member_id, expires_at) WHERE revoked_at IS NULL;

CREATE TABLE workforce_audit_events (
    event_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_member_id uuid REFERENCES workforce_members(member_id) ON DELETE RESTRICT,
    event_type     text NOT NULL,
    subject_type   text NOT NULL,
    subject_id     uuid,
    occurred_at    timestamptz NOT NULL DEFAULT now(),
    request_id     text,
    reason         text,
    before_state   jsonb,
    after_state    jsonb,
    metadata       jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX workforce_audit_subject_idx ON workforce_audit_events(subject_type, subject_id, occurred_at DESC);
CREATE INDEX workforce_audit_actor_idx ON workforce_audit_events(actor_member_id, occurred_at DESC);

-- Prevent overlapping active configuration windows and active assignments for the same member/position.
ALTER TABLE position_config_versions
    ADD CONSTRAINT position_config_versions_no_overlap
    EXCLUDE USING gist (
        position_id WITH =,
        tstzrange(effective_from, effective_to, '[)') WITH &&
    ) WHERE (status IN ('scheduled', 'active'));
ALTER TABLE position_assignments
    ADD CONSTRAINT position_assignments_no_overlap
    EXCLUDE USING gist (
        member_id WITH =,
        position_id WITH =,
        tstzrange(starts_at, ends_at, '[)') WITH &&
    ) WHERE (status IN ('scheduled', 'active'));

