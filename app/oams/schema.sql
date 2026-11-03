-- OAMS 建库脚本（唯一的一份 schema 定义；不再有增量迁移文件）
--
-- 用法：oams db init
-- 幂等：所有对象都是 IF NOT EXISTS / CREATE OR REPLACE，或包在 DO 块里吞掉「已存在」；
--       在一个已建好的库上重复执行只会输出「已存在」而不是报错。
-- 来源：2026-11-01 由开发库 pg_dump --schema-only 导出的 schema 归一化而来
--       （schema 名改成 oams，去 owner/ACL，去掉旧的迁移台账表 schema_migrations）。

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
-- 下面所有对象都带 oams. 前缀；search_path 只用来找扩展提供的运算符类（btree_gist）。
SET search_path = oams, public, pg_catalog;
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: oams; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA IF NOT EXISTS oams;

-- 排他约束 position_assignments_no_overlap 用的是 gist + uuid 等值，
-- 需要 btree_gist 提供的运算符类；它不在 oams schema 里，pg_dump 按 schema 导出时不会带，这里显式建。
CREATE EXTENSION IF NOT EXISTS btree_gist;

--
-- Name: spaces_enforce_desk_department(); Type: FUNCTION; Schema: oams; Owner: -
--

CREATE OR REPLACE FUNCTION oams.spaces_enforce_desk_department() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NEW.position_id IS NOT NULL
       AND NOT EXISTS (SELECT 1
                         FROM oams.positions p
                        WHERE p.position_id = NEW.position_id
                          AND p.org_unit_id = NEW.org_unit_id) THEN
        RAISE EXCEPTION
            'a desk must belong to a position of its own department (position %, org unit %)',
            NEW.position_id, NEW.org_unit_id;
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END $$;

--
-- Name: work_items_position_org_unit_matches(); Type: FUNCTION; Schema: oams; Owner: -
--

CREATE OR REPLACE FUNCTION oams.work_items_position_org_unit_matches() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    position_org_unit uuid;
BEGIN
    SELECT p.org_unit_id INTO position_org_unit
      FROM oams.positions p
     WHERE p.position_id = NEW.position_id;

    IF position_org_unit IS NULL THEN
        RAISE EXCEPTION 'work_items.position_id % does not reference an existing position', NEW.position_id;
    END IF;

    IF position_org_unit IS NOT NULL AND position_org_unit <> NEW.org_unit_id THEN
        RAISE EXCEPTION 'work_items.org_unit_id % disagrees with position org_unit %', NEW.org_unit_id, position_org_unit;
    END IF;

    RETURN NEW;
END;
$$;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: approval_records; Type: TABLE; Schema: oams; Owner: -
--

CREATE TABLE IF NOT EXISTS oams.approval_records (
    approval_id uuid DEFAULT gen_random_uuid() NOT NULL,
    subject_type text NOT NULL,
    subject_id uuid NOT NULL,
    approver_member_id uuid NOT NULL,
    approver_role text NOT NULL,
    decision text NOT NULL,
    comment text,
    decided_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT approval_records_approver_role_check CHECK ((approver_role = ANY (ARRAY['department_head'::text, 'hr'::text, 'admin'::text]))),
    CONSTRAINT approval_records_decision_check CHECK ((decision = ANY (ARRAY['approved'::text, 'rejected'::text]))),
    CONSTRAINT approval_records_subject_type_check CHECK ((subject_type = ANY (ARRAY['position_assignment'::text, 'permission_grant'::text, 'position_config_version'::text])))
);

--
-- Name: collab_messages; Type: TABLE; Schema: oams; Owner: -
--

CREATE TABLE IF NOT EXISTS oams.collab_messages (
    message_id uuid DEFAULT gen_random_uuid() NOT NULL,
    thread_id uuid NOT NULL,
    seq bigint NOT NULL,
    author_id uuid,
    author_kind text DEFAULT 'human'::text NOT NULL,
    agent_model text,
    body text NOT NULL,
    mentions uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    client_msg_id text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT collab_messages_agent_has_model CHECK (((author_kind <> 'agent'::text) OR (agent_model IS NOT NULL))),
    CONSTRAINT collab_messages_author_kind_check CHECK ((author_kind = ANY (ARRAY['human'::text, 'agent'::text]))),
    CONSTRAINT collab_messages_body_check CHECK (((char_length(body) >= 1) AND (char_length(body) <= 4000)))
);

--
-- Name: TABLE collab_messages; Type: COMMENT; Schema: oams; Owner: -
--

COMMENT ON TABLE oams.collab_messages IS '协作室消息，append-only：不更新、不删除、不撤回';

--
-- Name: collab_reads; Type: TABLE; Schema: oams; Owner: -
--

CREATE TABLE IF NOT EXISTS oams.collab_reads (
    thread_id uuid NOT NULL,
    member_id uuid NOT NULL,
    last_seq bigint DEFAULT 0 NOT NULL,
    read_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: TABLE collab_reads; Type: COMMENT; Schema: oams; Owner: -
--

COMMENT ON TABLE oams.collab_reads IS '未读的唯一来源：每个成员每个会话一个 last_seq，存服务端';

--
-- Name: collab_threads; Type: TABLE; Schema: oams; Owner: -
--

CREATE TABLE IF NOT EXISTS oams.collab_threads (
    thread_id uuid DEFAULT gen_random_uuid() NOT NULL,
    kind text NOT NULL,
    org_unit_id uuid,
    interface_id uuid,
    member_a uuid,
    member_b uuid,
    status text DEFAULT 'active'::text NOT NULL,
    title text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT collab_threads_kind_check CHECK ((kind = ANY (ARRAY['manage'::text, 'fill'::text, 'dm'::text]))),
    CONSTRAINT collab_threads_shape CHECK ((((kind = 'manage'::text) AND (org_unit_id IS NOT NULL) AND (interface_id IS NULL)) OR ((kind = 'fill'::text) AND (interface_id IS NOT NULL)) OR ((kind = 'dm'::text) AND (member_a IS NOT NULL) AND (member_b IS NOT NULL) AND (member_a < member_b)))),
    CONSTRAINT collab_threads_status_check CHECK ((status = ANY (ARRAY['active'::text, 'closed'::text])))
);

--
-- Name: TABLE collab_threads; Type: COMMENT; Schema: oams; Owner: -
--

COMMENT ON TABLE oams.collab_threads IS '协作室会话：manage 部门管理群 / fill 页面汇报群 / dm 私聊';

--
-- Name: interface_entries; Type: TABLE; Schema: oams; Owner: -
--

CREATE TABLE IF NOT EXISTS oams.interface_entries (
    entry_id uuid DEFAULT gen_random_uuid() NOT NULL,
    interface_id uuid NOT NULL,
    work_item_id uuid,
    title text,
    status text,
    blocked_reason text,
    needs_decision boolean DEFAULT false NOT NULL,
    progress_value integer,
    note text,
    evidence text,
    extra jsonb DEFAULT '{}'::jsonb NOT NULL,
    reported_by uuid NOT NULL,
    submitted_via text DEFAULT 'human'::text NOT NULL,
    reported_at timestamp with time zone DEFAULT now() NOT NULL,
    status_entry text DEFAULT 'active'::text NOT NULL,
    retracted_by uuid,
    retracted_at timestamp with time zone,
    entry_mode text DEFAULT 'live'::text NOT NULL,
    CONSTRAINT interface_entries_check CHECK (((status_entry <> 'retracted'::text) OR ((retracted_by IS NOT NULL) AND (retracted_at IS NOT NULL)))),
    CONSTRAINT interface_entries_check1 CHECK (((status IS NULL) OR (status <> 'blocked'::text) OR ((blocked_reason IS NOT NULL) AND (btrim(blocked_reason) <> ''::text)))),
    CONSTRAINT interface_entries_entry_mode_check CHECK ((entry_mode = ANY (ARRAY['live'::text, 'draft'::text]))),
    CONSTRAINT interface_entries_evidence_check CHECK (((evidence IS NULL) OR (btrim(evidence) <> ''::text))),
    CONSTRAINT interface_entries_progress_value_check CHECK (((progress_value IS NULL) OR ((progress_value >= 0) AND (progress_value <= 100)))),
    CONSTRAINT interface_entries_status_check CHECK (((status IS NULL) OR (status = ANY (ARRAY['planned'::text, 'in_progress'::text, 'blocked'::text, 'done'::text])))),
    CONSTRAINT interface_entries_status_entry_check CHECK ((status_entry = ANY (ARRAY['active'::text, 'retracted'::text]))),
    CONSTRAINT interface_entries_submitted_via_check CHECK ((submitted_via = ANY (ARRAY['human'::text, 'agent'::text])))
);

--
-- Name: interfaces; Type: TABLE; Schema: oams; Owner: -
--

CREATE TABLE IF NOT EXISTS oams.interfaces (
    interface_id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_unit_id uuid NOT NULL,
    slug text NOT NULL,
    name text NOT NULL,
    status text DEFAULT 'draft'::text NOT NULL,
    visibility text DEFAULT 'department'::text NOT NULL,
    schema_version text NOT NULL,
    descriptor jsonb NOT NULL,
    based_on uuid,
    version_no integer DEFAULT 1 NOT NULL,
    position_id uuid,
    created_by uuid,
    updated_by uuid,
    reviewed_by uuid,
    reviewed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    review_note text,
    review_code text,
    review_stage text DEFAULT 'department'::text NOT NULL,
    CONSTRAINT interfaces_check CHECK (((visibility <> 'public'::text) OR ((reviewed_by IS NOT NULL) AND (reviewed_at IS NOT NULL)))),
    CONSTRAINT interfaces_check1 CHECK (((status <> 'active'::text) OR ((visibility = 'department'::text) OR (reviewed_by IS NOT NULL)))),
    CONSTRAINT interfaces_review_code_check CHECK (((review_code IS NULL) OR (review_code = ANY (ARRAY['FIELD_UNMAPPED'::text, 'STATUS_UNMAPPED'::text, 'METRIC_UNDEFINED'::text, 'CAPABILITY_ESCALATION'::text, 'MODULE_NOT_WHITELISTED'::text, 'LAYOUT_TOO_DEEP'::text, 'DATA_RULE_VIOLATION'::text])))),
    CONSTRAINT interfaces_review_stage_check CHECK ((review_stage = ANY (ARRAY['department'::text, 'company'::text]))),
    CONSTRAINT interfaces_status_check CHECK ((status = ANY (ARRAY['draft'::text, 'pending'::text, 'active'::text, 'archived'::text]))),
    CONSTRAINT interfaces_visibility_check CHECK ((visibility = ANY (ARRAY['department'::text, 'public'::text])))
);

--
-- Name: COLUMN interfaces.review_stage; Type: COMMENT; Schema: oams; Owner: -
--

COMMENT ON COLUMN oams.interfaces.review_stage IS 'Which approval status=''pending'' is waiting for: department (first level) or company (final). Only meaningful while pending; a draft or active row reads department, which is where a new submission starts.';

--
-- Name: member_external_identities; Type: TABLE; Schema: oams; Owner: -
--

CREATE TABLE IF NOT EXISTS oams.member_external_identities (
    identity_id uuid DEFAULT gen_random_uuid() NOT NULL,
    member_id uuid NOT NULL,
    provider text NOT NULL,
    tenant_id text NOT NULL,
    subject_id text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    linked_at timestamp with time zone DEFAULT now() NOT NULL,
    revoked_at timestamp with time zone,
    CONSTRAINT member_external_identities_check CHECK ((((status = 'active'::text) AND (revoked_at IS NULL)) OR ((status = 'revoked'::text) AND (revoked_at IS NOT NULL)))),
    CONSTRAINT member_external_identities_status_check CHECK ((status = ANY (ARRAY['active'::text, 'revoked'::text])))
);

--
-- Name: org_unit_external_refs; Type: TABLE; Schema: oams; Owner: -
--

CREATE TABLE IF NOT EXISTS oams.org_unit_external_refs (
    external_ref_id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_unit_id uuid NOT NULL,
    source_system text NOT NULL,
    external_id text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: org_units; Type: TABLE; Schema: oams; Owner: -
--

CREATE TABLE IF NOT EXISTS oams.org_units (
    org_unit_id uuid DEFAULT gen_random_uuid() NOT NULL,
    parent_org_unit_id uuid,
    org_code text NOT NULL,
    name text NOT NULL,
    unit_type text DEFAULT 'department'::text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT org_units_check CHECK (((parent_org_unit_id IS NULL) OR (parent_org_unit_id <> org_unit_id))),
    CONSTRAINT org_units_status_check CHECK ((status = ANY (ARRAY['active'::text, 'inactive'::text]))),
    CONSTRAINT org_units_unit_type_check CHECK ((unit_type = ANY (ARRAY['company'::text, 'department'::text, 'team'::text, 'group'::text])))
);

--
-- Name: permission_grants; Type: TABLE; Schema: oams; Owner: -
--

CREATE TABLE IF NOT EXISTS oams.permission_grants (
    grant_id uuid DEFAULT gen_random_uuid() NOT NULL,
    subject_type text NOT NULL,
    position_id uuid,
    member_id uuid,
    resource text NOT NULL,
    action text NOT NULL,
    scope_type text NOT NULL,
    scope_org_unit_id uuid,
    scope_definition jsonb DEFAULT '{}'::jsonb NOT NULL,
    effect text DEFAULT 'allow'::text NOT NULL,
    valid_from timestamp with time zone DEFAULT now() NOT NULL,
    valid_until timestamp with time zone,
    revoked_at timestamp with time zone,
    reason text NOT NULL,
    source_assignment_id uuid,
    approved_by uuid,
    approved_at timestamp with time zone,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT permission_grants_check CHECK ((((subject_type = 'position'::text) AND (position_id IS NOT NULL) AND (member_id IS NULL)) OR ((subject_type = 'member'::text) AND (member_id IS NOT NULL) AND (position_id IS NULL)))),
    CONSTRAINT permission_grants_check1 CHECK (((valid_until IS NULL) OR (valid_until > valid_from))),
    CONSTRAINT permission_grants_check2 CHECK (((subject_type <> 'member'::text) OR (valid_until IS NOT NULL))),
    CONSTRAINT permission_grants_check3 CHECK (((scope_type <> 'org_unit'::text) OR (scope_org_unit_id IS NOT NULL))),
    CONSTRAINT permission_grants_check4 CHECK (((approved_at IS NULL) OR (approved_by IS NOT NULL))),
    CONSTRAINT permission_grants_effect_check CHECK ((effect = ANY (ARRAY['allow'::text, 'deny'::text]))),
    CONSTRAINT permission_grants_scope_type_check CHECK ((scope_type = ANY (ARRAY['self_position'::text, 'org_unit'::text, 'selected_positions'::text, 'company_aggregate'::text, 'all'::text]))),
    CONSTRAINT permission_grants_subject_type_check CHECK ((subject_type = ANY (ARRAY['position'::text, 'member'::text])))
);

--
-- Name: position_assignments; Type: TABLE; Schema: oams; Owner: -
--

CREATE TABLE IF NOT EXISTS oams.position_assignments (
    assignment_id uuid DEFAULT gen_random_uuid() NOT NULL,
    member_id uuid NOT NULL,
    position_id uuid NOT NULL,
    assignment_type text NOT NULL,
    status text DEFAULT 'scheduled'::text NOT NULL,
    starts_at timestamp with time zone NOT NULL,
    ends_at timestamp with time zone,
    approved_by uuid,
    approved_at timestamp with time zone,
    reason text NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    ended_by uuid,
    ended_at timestamp with time zone,
    CONSTRAINT position_assignments_acting_max_90_days CHECK (((assignment_type <> 'acting'::text) OR (ends_at <= (starts_at + '90 days'::interval)))),
    CONSTRAINT position_assignments_assignment_type_check CHECK ((assignment_type = ANY (ARRAY['primary'::text, 'concurrent'::text, 'acting'::text]))),
    CONSTRAINT position_assignments_check CHECK (((ends_at IS NULL) OR (ends_at > starts_at))),
    CONSTRAINT position_assignments_check1 CHECK (((assignment_type <> 'acting'::text) OR (ends_at IS NOT NULL))),
    CONSTRAINT position_assignments_check2 CHECK ((((status = 'ended'::text) AND (ended_at IS NOT NULL)) OR ((status <> 'ended'::text) AND (ended_at IS NULL)))),
    CONSTRAINT position_assignments_check3 CHECK (((approved_at IS NULL) OR (approved_by IS NOT NULL))),
    CONSTRAINT position_assignments_status_check CHECK ((status = ANY (ARRAY['scheduled'::text, 'active'::text, 'ended'::text, 'cancelled'::text])))
);

--
-- Name: position_config_versions; Type: TABLE; Schema: oams; Owner: -
--

CREATE TABLE IF NOT EXISTS oams.position_config_versions (
    config_version_id uuid DEFAULT gen_random_uuid() NOT NULL,
    position_id uuid NOT NULL,
    version_no integer NOT NULL,
    status text DEFAULT 'draft'::text NOT NULL,
    effective_from timestamp with time zone,
    effective_to timestamp with time zone,
    based_on_version_id uuid,
    config jsonb DEFAULT '{}'::jsonb NOT NULL,
    change_reason text NOT NULL,
    created_by uuid,
    approved_by uuid,
    approved_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT position_config_versions_check CHECK (((effective_to IS NULL) OR (effective_from IS NULL) OR (effective_to > effective_from))),
    CONSTRAINT position_config_versions_check1 CHECK (((status <> ALL (ARRAY['active'::text, 'scheduled'::text])) OR (effective_from IS NOT NULL))),
    CONSTRAINT position_config_versions_check2 CHECK (((approved_at IS NULL) OR (approved_by IS NOT NULL))),
    CONSTRAINT position_config_versions_status_check CHECK ((status = ANY (ARRAY['draft'::text, 'scheduled'::text, 'active'::text, 'superseded'::text, 'withdrawn'::text]))),
    CONSTRAINT position_config_versions_version_no_check CHECK ((version_no > 0))
);

--
-- Name: positions; Type: TABLE; Schema: oams; Owner: -
--

CREATE TABLE IF NOT EXISTS oams.positions (
    position_id uuid DEFAULT gen_random_uuid() NOT NULL,
    position_code text NOT NULL,
    title text NOT NULL,
    org_unit_id uuid NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT positions_status_check CHECK ((status = ANY (ARRAY['active'::text, 'inactive'::text, 'retired'::text])))
);

--
-- Name: sensitive_field_registry; Type: TABLE; Schema: oams; Owner: -
--

CREATE TABLE IF NOT EXISTS oams.sensitive_field_registry (
    field_key text NOT NULL,
    label text NOT NULL,
    description text,
    requires_explicit_grant boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: work_item_reports; Type: TABLE; Schema: oams; Owner: -
--

CREATE TABLE IF NOT EXISTS oams.work_item_reports (
    report_id uuid DEFAULT gen_random_uuid() NOT NULL,
    work_item_id uuid NOT NULL,
    reported_by_member_id uuid NOT NULL,
    submitted_via text NOT NULL,
    progress_value smallint,
    note text,
    evidence text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    retracted_by_member_id uuid,
    retracted_at timestamp with time zone,
    reported_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT work_item_reports_check CHECK ((((status = 'active'::text) AND (retracted_at IS NULL) AND (retracted_by_member_id IS NULL)) OR ((status = 'retracted'::text) AND (retracted_at IS NOT NULL) AND (retracted_by_member_id IS NOT NULL)))),
    CONSTRAINT work_item_reports_evidence_check CHECK ((btrim(evidence) <> ''::text)),
    CONSTRAINT work_item_reports_progress_value_check CHECK (((progress_value IS NULL) OR ((progress_value >= 0) AND (progress_value <= 100)))),
    CONSTRAINT work_item_reports_status_check CHECK ((status = ANY (ARRAY['active'::text, 'retracted'::text]))),
    CONSTRAINT work_item_reports_submitted_via_check CHECK ((submitted_via = ANY (ARRAY['human'::text, 'agent'::text])))
);

--
-- Name: work_item_current_state; Type: VIEW; Schema: oams; Owner: -
--

CREATE OR REPLACE VIEW oams.work_item_current_state AS
 SELECT DISTINCT ON (work_item_id) work_item_id,
    report_id AS latest_report_id,
    reported_by_member_id AS latest_reported_by_member_id,
    submitted_via AS latest_submitted_via,
    progress_value AS latest_progress_value,
    note AS latest_note,
    evidence AS latest_evidence,
    reported_at AS latest_reported_at
   FROM oams.work_item_reports r
  WHERE (status = 'active'::text)
  ORDER BY work_item_id, reported_at DESC, report_id DESC;

--
-- Name: work_items; Type: TABLE; Schema: oams; Owner: -
--

CREATE TABLE IF NOT EXISTS oams.work_items (
    work_item_id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_unit_id uuid NOT NULL,
    position_id uuid NOT NULL,
    title text NOT NULL,
    kind text DEFAULT 'routine'::text NOT NULL,
    status text DEFAULT 'planned'::text NOT NULL,
    blocked_reason text,
    needs_decision boolean DEFAULT false NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT work_items_check CHECK (((status <> 'blocked'::text) OR ((blocked_reason IS NOT NULL) AND (btrim(blocked_reason) <> ''::text)))),
    CONSTRAINT work_items_kind_check CHECK ((kind = ANY (ARRAY['project'::text, 'routine'::text, 'adhoc'::text]))),
    CONSTRAINT work_items_status_check CHECK ((status = ANY (ARRAY['planned'::text, 'in_progress'::text, 'blocked'::text, 'done'::text, 'cancelled'::text]))),
    CONSTRAINT work_items_title_check CHECK ((btrim(title) <> ''::text))
);

--
-- Name: workforce_audit_events; Type: TABLE; Schema: oams; Owner: -
--

CREATE TABLE IF NOT EXISTS oams.workforce_audit_events (
    event_id uuid DEFAULT gen_random_uuid() NOT NULL,
    actor_member_id uuid,
    event_type text NOT NULL,
    subject_type text NOT NULL,
    subject_id uuid,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL,
    request_id text,
    reason text,
    before_state jsonb,
    after_state jsonb,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);

--
-- Name: workforce_members; Type: TABLE; Schema: oams; Owner: -
--

CREATE TABLE IF NOT EXISTS oams.workforce_members (
    member_id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_code text,
    display_name text NOT NULL,
    employment_status text DEFAULT 'pending'::text NOT NULL,
    token_version bigint DEFAULT 1 NOT NULL,
    joined_at date,
    departed_at date,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT workforce_members_check CHECK (((departed_at IS NULL) OR (joined_at IS NULL) OR (departed_at >= joined_at))),
    CONSTRAINT workforce_members_check1 CHECK (((employment_status <> 'departed'::text) OR (departed_at IS NOT NULL))),
    CONSTRAINT workforce_members_employment_status_check CHECK ((employment_status = ANY (ARRAY['pending'::text, 'active'::text, 'suspended'::text, 'departed'::text]))),
    CONSTRAINT workforce_members_token_version_check CHECK ((token_version > 0))
);

--
-- Name: workforce_sessions; Type: TABLE; Schema: oams; Owner: -
--

CREATE TABLE IF NOT EXISTS oams.workforce_sessions (
    session_id uuid DEFAULT gen_random_uuid() NOT NULL,
    member_id uuid NOT NULL,
    token_digest bytea NOT NULL,
    token_version bigint NOT NULL,
    issued_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone,
    auth_provider text NOT NULL,
    CONSTRAINT workforce_sessions_check CHECK ((expires_at > issued_at)),
    CONSTRAINT workforce_sessions_token_version_check CHECK ((token_version > 0))
);

--
-- Name: connected_apps; Type: TABLE; Schema: oams; Owner: -
--

-- 接入方：一个「应用」是跑在外面的 AI / 脚本给自己起的名字（WorkBuddy 这类）。
-- 它没有密码，也不能自己决定代表谁——只能拿一次性码换到「某人点过允许」的短期票。
CREATE TABLE IF NOT EXISTS oams.connected_apps (
    app_id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    owner_member_id uuid,
    status text DEFAULT 'active'::text NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    revoked_at timestamp with time zone,
    CONSTRAINT connected_apps_name_key UNIQUE (name),
    CONSTRAINT connected_apps_status_check CHECK ((status = ANY (ARRAY['active'::text, 'revoked'::text])))
);

--
-- Name: app_codes; Type: TABLE; Schema: oams; Owner: -
--

-- 一次性 6 位码：应用拿来换一张「等人点允许」的凭证。发码时不认人，认人发生在
-- 人打开 /connect/<code> 点允许那一刻——码本身决定不了代表谁。
CREATE TABLE IF NOT EXISTS oams.app_codes (
    code text NOT NULL,
    app_id uuid NOT NULL,
    purpose text DEFAULT 'connect'::text NOT NULL,
    requested_scopes text[] DEFAULT '{}'::text[] NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by uuid,
    approved_at timestamp with time zone,
    approved_member_id uuid,
    authorization_id uuid,
    token_issued_at timestamp with time zone,
    CONSTRAINT app_codes_check CHECK ((expires_at > created_at))
);

--
-- Name: app_authorizations; Type: TABLE; Schema: oams; Owner: -
--

-- 人点过「允许」的那一票：(应用, 人) 一行，生效到 valid_until。撤销就是 revoked_at，
-- 同时把这个应用的短期票一起吊销——票不会因为还没到期就继续能用。
CREATE TABLE IF NOT EXISTS oams.app_authorizations (
    authorization_id uuid DEFAULT gen_random_uuid() NOT NULL,
    app_id uuid NOT NULL,
    member_id uuid NOT NULL,
    scopes text[] DEFAULT '{}'::text[] NOT NULL,
    valid_until timestamp with time zone NOT NULL,
    approved_by uuid NOT NULL,
    approved_at timestamp with time zone DEFAULT now() NOT NULL,
    revoked_at timestamp with time zone,
    revoke_reason text,
    last_token_issued_at timestamp with time zone,
    CONSTRAINT app_authorizations_check CHECK ((valid_until > approved_at))
);

--
-- Name: approval_records approval_records_pkey; Type: CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.approval_records
        ADD CONSTRAINT approval_records_pkey PRIMARY KEY (approval_id);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: approval_records approval_records_subject_type_subject_id_approver_member_id_key; Type: CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.approval_records
        ADD CONSTRAINT approval_records_subject_type_subject_id_approver_member_id_key UNIQUE (subject_type, subject_id, approver_member_id);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: collab_messages collab_messages_pkey; Type: CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.collab_messages
        ADD CONSTRAINT collab_messages_pkey PRIMARY KEY (message_id);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: collab_messages collab_messages_thread_id_client_msg_id_key; Type: CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.collab_messages
        ADD CONSTRAINT collab_messages_thread_id_client_msg_id_key UNIQUE (thread_id, client_msg_id);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: collab_messages collab_messages_thread_id_seq_key; Type: CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.collab_messages
        ADD CONSTRAINT collab_messages_thread_id_seq_key UNIQUE (thread_id, seq);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: collab_reads collab_reads_pkey; Type: CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.collab_reads
        ADD CONSTRAINT collab_reads_pkey PRIMARY KEY (thread_id, member_id);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: collab_threads collab_threads_pkey; Type: CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.collab_threads
        ADD CONSTRAINT collab_threads_pkey PRIMARY KEY (thread_id);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: interface_entries interface_entries_pkey; Type: CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.interface_entries
        ADD CONSTRAINT interface_entries_pkey PRIMARY KEY (entry_id);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: interfaces interfaces_org_unit_id_slug_version_no_key; Type: CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.interfaces
        ADD CONSTRAINT interfaces_org_unit_id_slug_version_no_key UNIQUE (org_unit_id, slug, version_no);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: interfaces interfaces_pkey; Type: CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.interfaces
        ADD CONSTRAINT interfaces_pkey PRIMARY KEY (interface_id);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: member_external_identities member_external_identities_pkey; Type: CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.member_external_identities
        ADD CONSTRAINT member_external_identities_pkey PRIMARY KEY (identity_id);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: org_unit_external_refs org_unit_external_refs_org_unit_id_source_system_key; Type: CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.org_unit_external_refs
        ADD CONSTRAINT org_unit_external_refs_org_unit_id_source_system_key UNIQUE (org_unit_id, source_system);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: org_unit_external_refs org_unit_external_refs_pkey; Type: CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.org_unit_external_refs
        ADD CONSTRAINT org_unit_external_refs_pkey PRIMARY KEY (external_ref_id);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: org_unit_external_refs org_unit_external_refs_source_system_external_id_key; Type: CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.org_unit_external_refs
        ADD CONSTRAINT org_unit_external_refs_source_system_external_id_key UNIQUE (source_system, external_id);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: org_units org_units_org_code_key; Type: CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.org_units
        ADD CONSTRAINT org_units_org_code_key UNIQUE (org_code);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: org_units org_units_pkey; Type: CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.org_units
        ADD CONSTRAINT org_units_pkey PRIMARY KEY (org_unit_id);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: permission_grants permission_grants_pkey; Type: CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.permission_grants
        ADD CONSTRAINT permission_grants_pkey PRIMARY KEY (grant_id);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: position_assignments position_assignments_no_overlap; Type: CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.position_assignments
        ADD CONSTRAINT position_assignments_no_overlap EXCLUDE USING gist (member_id WITH =, position_id WITH =, tstzrange(starts_at, ends_at, '[)'::text) WITH &&) WHERE ((status = ANY (ARRAY['scheduled'::text, 'active'::text])));
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: position_assignments position_assignments_pkey; Type: CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.position_assignments
        ADD CONSTRAINT position_assignments_pkey PRIMARY KEY (assignment_id);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: position_assignments position_assignments_single_primary_per_position; Type: CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.position_assignments
        ADD CONSTRAINT position_assignments_single_primary_per_position EXCLUDE USING gist (position_id WITH =, tstzrange(starts_at, ends_at, '[)'::text) WITH &&) WHERE (((assignment_type = 'primary'::text) AND (status = ANY (ARRAY['scheduled'::text, 'active'::text]))));
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: position_config_versions position_config_versions_no_overlap; Type: CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.position_config_versions
        ADD CONSTRAINT position_config_versions_no_overlap EXCLUDE USING gist (position_id WITH =, tstzrange(effective_from, effective_to, '[)'::text) WITH &&) WHERE ((status = ANY (ARRAY['scheduled'::text, 'active'::text])));
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: position_config_versions position_config_versions_pkey; Type: CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.position_config_versions
        ADD CONSTRAINT position_config_versions_pkey PRIMARY KEY (config_version_id);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: position_config_versions position_config_versions_position_id_version_no_key; Type: CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.position_config_versions
        ADD CONSTRAINT position_config_versions_position_id_version_no_key UNIQUE (position_id, version_no);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: positions positions_pkey; Type: CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.positions
        ADD CONSTRAINT positions_pkey PRIMARY KEY (position_id);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: positions positions_position_code_key; Type: CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.positions
        ADD CONSTRAINT positions_position_code_key UNIQUE (position_code);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: sensitive_field_registry sensitive_field_registry_pkey; Type: CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.sensitive_field_registry
        ADD CONSTRAINT sensitive_field_registry_pkey PRIMARY KEY (field_key);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: work_item_reports work_item_reports_pkey; Type: CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.work_item_reports
        ADD CONSTRAINT work_item_reports_pkey PRIMARY KEY (report_id);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: work_items work_items_pkey; Type: CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.work_items
        ADD CONSTRAINT work_items_pkey PRIMARY KEY (work_item_id);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: workforce_audit_events workforce_audit_events_pkey; Type: CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.workforce_audit_events
        ADD CONSTRAINT workforce_audit_events_pkey PRIMARY KEY (event_id);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: workforce_members workforce_members_employee_code_key; Type: CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.workforce_members
        ADD CONSTRAINT workforce_members_employee_code_key UNIQUE (employee_code);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: workforce_members workforce_members_pkey; Type: CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.workforce_members
        ADD CONSTRAINT workforce_members_pkey PRIMARY KEY (member_id);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: workforce_sessions workforce_sessions_pkey; Type: CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.workforce_sessions
        ADD CONSTRAINT workforce_sessions_pkey PRIMARY KEY (session_id);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: workforce_sessions workforce_sessions_token_digest_key; Type: CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.workforce_sessions
        ADD CONSTRAINT workforce_sessions_token_digest_key UNIQUE (token_digest);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: approval_records_subject_idx; Type: INDEX; Schema: oams; Owner: -
--

CREATE INDEX IF NOT EXISTS approval_records_subject_idx ON oams.approval_records USING btree (subject_type, subject_id, decided_at DESC);

--
-- Name: collab_messages_thread_seq_idx; Type: INDEX; Schema: oams; Owner: -
--

CREATE INDEX IF NOT EXISTS collab_messages_thread_seq_idx ON oams.collab_messages USING btree (thread_id, seq DESC);

--
-- Name: collab_threads_dm_uq; Type: INDEX; Schema: oams; Owner: -
--

CREATE UNIQUE INDEX IF NOT EXISTS collab_threads_dm_uq ON oams.collab_threads USING btree (member_a, member_b) WHERE (kind = 'dm'::text);

--
-- Name: collab_threads_fill_uq; Type: INDEX; Schema: oams; Owner: -
--

CREATE UNIQUE INDEX IF NOT EXISTS collab_threads_fill_uq ON oams.collab_threads USING btree (interface_id) WHERE (kind = 'fill'::text);

--
-- Name: collab_threads_iface_idx; Type: INDEX; Schema: oams; Owner: -
--

CREATE INDEX IF NOT EXISTS collab_threads_iface_idx ON oams.collab_threads USING btree (interface_id) WHERE (status = 'active'::text);

--
-- Name: collab_threads_manage_uq; Type: INDEX; Schema: oams; Owner: -
--

CREATE UNIQUE INDEX IF NOT EXISTS collab_threads_manage_uq ON oams.collab_threads USING btree (org_unit_id) WHERE (kind = 'manage'::text);

--
-- Name: collab_threads_org_idx; Type: INDEX; Schema: oams; Owner: -
--

CREATE INDEX IF NOT EXISTS collab_threads_org_idx ON oams.collab_threads USING btree (org_unit_id) WHERE (status = 'active'::text);

--
-- Name: interface_entries_extra_idx; Type: INDEX; Schema: oams; Owner: -
--

CREATE INDEX IF NOT EXISTS interface_entries_extra_idx ON oams.interface_entries USING gin (extra);

--
-- Name: interface_entries_interface_idx; Type: INDEX; Schema: oams; Owner: -
--

CREATE INDEX IF NOT EXISTS interface_entries_interface_idx ON oams.interface_entries USING btree (interface_id, reported_at DESC);

--
-- Name: interface_entries_mode_idx; Type: INDEX; Schema: oams; Owner: -
--

CREATE INDEX IF NOT EXISTS interface_entries_mode_idx ON oams.interface_entries USING btree (interface_id, entry_mode, reported_at DESC);

--
-- Name: interface_entries_reporter_idx; Type: INDEX; Schema: oams; Owner: -
--

CREATE INDEX IF NOT EXISTS interface_entries_reporter_idx ON oams.interface_entries USING btree (reported_by);

--
-- Name: interface_entries_work_item_idx; Type: INDEX; Schema: oams; Owner: -
--

CREATE INDEX IF NOT EXISTS interface_entries_work_item_idx ON oams.interface_entries USING btree (work_item_id);

--
-- Name: interfaces_one_active_idx; Type: INDEX; Schema: oams; Owner: -
--

CREATE UNIQUE INDEX IF NOT EXISTS interfaces_one_active_idx ON oams.interfaces USING btree (org_unit_id, slug) WHERE (status = 'active'::text);

--
-- Name: interfaces_one_open_version_idx; Type: INDEX; Schema: oams; Owner: -
--

CREATE UNIQUE INDEX IF NOT EXISTS interfaces_one_open_version_idx ON oams.interfaces USING btree (org_unit_id, slug) WHERE (status = ANY (ARRAY['draft'::text, 'pending'::text]));

--
-- Name: interfaces_org_idx; Type: INDEX; Schema: oams; Owner: -
--

CREATE INDEX IF NOT EXISTS interfaces_org_idx ON oams.interfaces USING btree (org_unit_id, status);

--
-- Name: interfaces_pending_idx; Type: INDEX; Schema: oams; Owner: -
--

CREATE INDEX IF NOT EXISTS interfaces_pending_idx ON oams.interfaces USING btree (org_unit_id) WHERE (status = 'pending'::text);

--
-- Name: interfaces_pending_stage_idx; Type: INDEX; Schema: oams; Owner: -
--

CREATE INDEX IF NOT EXISTS interfaces_pending_stage_idx ON oams.interfaces USING btree (review_stage, org_unit_id) WHERE (status = 'pending'::text);

--
-- Name: interfaces_public_idx; Type: INDEX; Schema: oams; Owner: -
--

CREATE INDEX IF NOT EXISTS interfaces_public_idx ON oams.interfaces USING btree (visibility) WHERE (visibility = 'public'::text);

--
-- Name: member_external_identities_active_subject_idx; Type: INDEX; Schema: oams; Owner: -
--

CREATE UNIQUE INDEX IF NOT EXISTS member_external_identities_active_subject_idx ON oams.member_external_identities USING btree (provider, tenant_id, subject_id) WHERE (status = 'active'::text);

--
-- Name: member_external_identities_member_idx; Type: INDEX; Schema: oams; Owner: -
--

CREATE INDEX IF NOT EXISTS member_external_identities_member_idx ON oams.member_external_identities USING btree (member_id);

--
-- Name: permission_grants_member_lookup_idx; Type: INDEX; Schema: oams; Owner: -
--

CREATE INDEX IF NOT EXISTS permission_grants_member_lookup_idx ON oams.permission_grants USING btree (member_id, resource, action, valid_from, valid_until) WHERE ((revoked_at IS NULL) AND (subject_type = 'member'::text));

--
-- Name: permission_grants_position_lookup_idx; Type: INDEX; Schema: oams; Owner: -
--

CREATE INDEX IF NOT EXISTS permission_grants_position_lookup_idx ON oams.permission_grants USING btree (position_id, resource, action, valid_from, valid_until) WHERE ((revoked_at IS NULL) AND (subject_type = 'position'::text));

--
-- Name: position_assignments_member_active_idx; Type: INDEX; Schema: oams; Owner: -
--

CREATE INDEX IF NOT EXISTS position_assignments_member_active_idx ON oams.position_assignments USING btree (member_id, starts_at, ends_at) WHERE (status = ANY (ARRAY['scheduled'::text, 'active'::text]));

--
-- Name: position_assignments_position_active_idx; Type: INDEX; Schema: oams; Owner: -
--

CREATE INDEX IF NOT EXISTS position_assignments_position_active_idx ON oams.position_assignments USING btree (position_id, starts_at, ends_at) WHERE (status = ANY (ARRAY['scheduled'::text, 'active'::text]));

--
-- Name: position_config_versions_effective_idx; Type: INDEX; Schema: oams; Owner: -
--

CREATE INDEX IF NOT EXISTS position_config_versions_effective_idx ON oams.position_config_versions USING btree (position_id, status, effective_from DESC);

--
-- Name: positions_org_unit_idx; Type: INDEX; Schema: oams; Owner: -
--

CREATE INDEX IF NOT EXISTS positions_org_unit_idx ON oams.positions USING btree (org_unit_id, status);

--
-- Name: work_item_reports_item_idx; Type: INDEX; Schema: oams; Owner: -
--

CREATE INDEX IF NOT EXISTS work_item_reports_item_idx ON oams.work_item_reports USING btree (work_item_id, reported_at DESC) WHERE (status = 'active'::text);

--
-- Name: work_item_reports_reporter_idx; Type: INDEX; Schema: oams; Owner: -
--

CREATE INDEX IF NOT EXISTS work_item_reports_reporter_idx ON oams.work_item_reports USING btree (reported_by_member_id, reported_at DESC);

--
-- Name: work_items_org_unit_idx; Type: INDEX; Schema: oams; Owner: -
--

CREATE INDEX IF NOT EXISTS work_items_org_unit_idx ON oams.work_items USING btree (org_unit_id, status);

--
-- Name: work_items_position_idx; Type: INDEX; Schema: oams; Owner: -
--

CREATE INDEX IF NOT EXISTS work_items_position_idx ON oams.work_items USING btree (position_id) WHERE (status <> ALL (ARRAY['done'::text, 'cancelled'::text]));

--
-- Name: workforce_audit_actor_idx; Type: INDEX; Schema: oams; Owner: -
--

CREATE INDEX IF NOT EXISTS workforce_audit_actor_idx ON oams.workforce_audit_events USING btree (actor_member_id, occurred_at DESC);

--
-- Name: workforce_audit_subject_idx; Type: INDEX; Schema: oams; Owner: -
--

CREATE INDEX IF NOT EXISTS workforce_audit_subject_idx ON oams.workforce_audit_events USING btree (subject_type, subject_id, occurred_at DESC);

--
-- Name: workforce_sessions_member_live_idx; Type: INDEX; Schema: oams; Owner: -
--

CREATE INDEX IF NOT EXISTS workforce_sessions_member_live_idx ON oams.workforce_sessions USING btree (member_id, expires_at) WHERE (revoked_at IS NULL);

--
-- Name: work_items work_items_org_unit_consistency; Type: TRIGGER; Schema: oams; Owner: -
--

DROP TRIGGER IF EXISTS work_items_org_unit_consistency ON oams.work_items;
CREATE TRIGGER work_items_org_unit_consistency BEFORE INSERT OR UPDATE OF org_unit_id, position_id ON oams.work_items FOR EACH ROW EXECUTE FUNCTION oams.work_items_position_org_unit_matches();

--
-- Name: approval_records approval_records_approver_member_id_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.approval_records
        ADD CONSTRAINT approval_records_approver_member_id_fkey FOREIGN KEY (approver_member_id) REFERENCES oams.workforce_members(member_id) ON DELETE RESTRICT;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: collab_messages collab_messages_author_id_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.collab_messages
        ADD CONSTRAINT collab_messages_author_id_fkey FOREIGN KEY (author_id) REFERENCES oams.workforce_members(member_id) ON DELETE SET NULL;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: collab_messages collab_messages_thread_id_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.collab_messages
        ADD CONSTRAINT collab_messages_thread_id_fkey FOREIGN KEY (thread_id) REFERENCES oams.collab_threads(thread_id) ON DELETE CASCADE;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: collab_reads collab_reads_member_id_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.collab_reads
        ADD CONSTRAINT collab_reads_member_id_fkey FOREIGN KEY (member_id) REFERENCES oams.workforce_members(member_id) ON DELETE CASCADE;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: collab_reads collab_reads_thread_id_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.collab_reads
        ADD CONSTRAINT collab_reads_thread_id_fkey FOREIGN KEY (thread_id) REFERENCES oams.collab_threads(thread_id) ON DELETE CASCADE;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: collab_threads collab_threads_interface_id_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.collab_threads
        ADD CONSTRAINT collab_threads_interface_id_fkey FOREIGN KEY (interface_id) REFERENCES oams.interfaces(interface_id) ON DELETE CASCADE;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: collab_threads collab_threads_member_a_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.collab_threads
        ADD CONSTRAINT collab_threads_member_a_fkey FOREIGN KEY (member_a) REFERENCES oams.workforce_members(member_id) ON DELETE CASCADE;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: collab_threads collab_threads_member_b_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.collab_threads
        ADD CONSTRAINT collab_threads_member_b_fkey FOREIGN KEY (member_b) REFERENCES oams.workforce_members(member_id) ON DELETE CASCADE;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: collab_threads collab_threads_org_unit_id_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.collab_threads
        ADD CONSTRAINT collab_threads_org_unit_id_fkey FOREIGN KEY (org_unit_id) REFERENCES oams.org_units(org_unit_id) ON DELETE CASCADE;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: interface_entries interface_entries_interface_id_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.interface_entries
        ADD CONSTRAINT interface_entries_interface_id_fkey FOREIGN KEY (interface_id) REFERENCES oams.interfaces(interface_id) ON DELETE CASCADE;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: interface_entries interface_entries_reported_by_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.interface_entries
        ADD CONSTRAINT interface_entries_reported_by_fkey FOREIGN KEY (reported_by) REFERENCES oams.workforce_members(member_id);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: interface_entries interface_entries_retracted_by_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.interface_entries
        ADD CONSTRAINT interface_entries_retracted_by_fkey FOREIGN KEY (retracted_by) REFERENCES oams.workforce_members(member_id);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: interface_entries interface_entries_work_item_id_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.interface_entries
        ADD CONSTRAINT interface_entries_work_item_id_fkey FOREIGN KEY (work_item_id) REFERENCES oams.work_items(work_item_id) ON DELETE SET NULL;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: interfaces interfaces_based_on_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.interfaces
        ADD CONSTRAINT interfaces_based_on_fkey FOREIGN KEY (based_on) REFERENCES oams.interfaces(interface_id);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: interfaces interfaces_created_by_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.interfaces
        ADD CONSTRAINT interfaces_created_by_fkey FOREIGN KEY (created_by) REFERENCES oams.workforce_members(member_id);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: interfaces interfaces_org_unit_id_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.interfaces
        ADD CONSTRAINT interfaces_org_unit_id_fkey FOREIGN KEY (org_unit_id) REFERENCES oams.org_units(org_unit_id) ON DELETE CASCADE;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: interfaces interfaces_position_id_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.interfaces
        ADD CONSTRAINT interfaces_position_id_fkey FOREIGN KEY (position_id) REFERENCES oams.positions(position_id);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: interfaces interfaces_reviewed_by_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.interfaces
        ADD CONSTRAINT interfaces_reviewed_by_fkey FOREIGN KEY (reviewed_by) REFERENCES oams.workforce_members(member_id);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: interfaces interfaces_updated_by_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.interfaces
        ADD CONSTRAINT interfaces_updated_by_fkey FOREIGN KEY (updated_by) REFERENCES oams.workforce_members(member_id);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: member_external_identities member_external_identities_member_id_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.member_external_identities
        ADD CONSTRAINT member_external_identities_member_id_fkey FOREIGN KEY (member_id) REFERENCES oams.workforce_members(member_id) ON DELETE RESTRICT;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: org_unit_external_refs org_unit_external_refs_org_unit_id_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.org_unit_external_refs
        ADD CONSTRAINT org_unit_external_refs_org_unit_id_fkey FOREIGN KEY (org_unit_id) REFERENCES oams.org_units(org_unit_id) ON DELETE RESTRICT;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: org_units org_units_parent_org_unit_id_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.org_units
        ADD CONSTRAINT org_units_parent_org_unit_id_fkey FOREIGN KEY (parent_org_unit_id) REFERENCES oams.org_units(org_unit_id) ON DELETE RESTRICT;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: permission_grants permission_grants_approved_by_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.permission_grants
        ADD CONSTRAINT permission_grants_approved_by_fkey FOREIGN KEY (approved_by) REFERENCES oams.workforce_members(member_id) ON DELETE RESTRICT;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: permission_grants permission_grants_created_by_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.permission_grants
        ADD CONSTRAINT permission_grants_created_by_fkey FOREIGN KEY (created_by) REFERENCES oams.workforce_members(member_id) ON DELETE RESTRICT;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: permission_grants permission_grants_member_id_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.permission_grants
        ADD CONSTRAINT permission_grants_member_id_fkey FOREIGN KEY (member_id) REFERENCES oams.workforce_members(member_id) ON DELETE RESTRICT;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: permission_grants permission_grants_position_id_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.permission_grants
        ADD CONSTRAINT permission_grants_position_id_fkey FOREIGN KEY (position_id) REFERENCES oams.positions(position_id) ON DELETE RESTRICT;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: permission_grants permission_grants_scope_org_unit_id_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.permission_grants
        ADD CONSTRAINT permission_grants_scope_org_unit_id_fkey FOREIGN KEY (scope_org_unit_id) REFERENCES oams.org_units(org_unit_id) ON DELETE RESTRICT;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: permission_grants permission_grants_source_assignment_id_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.permission_grants
        ADD CONSTRAINT permission_grants_source_assignment_id_fkey FOREIGN KEY (source_assignment_id) REFERENCES oams.position_assignments(assignment_id) ON DELETE RESTRICT;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: position_assignments position_assignments_approved_by_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.position_assignments
        ADD CONSTRAINT position_assignments_approved_by_fkey FOREIGN KEY (approved_by) REFERENCES oams.workforce_members(member_id) ON DELETE RESTRICT;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: position_assignments position_assignments_created_by_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.position_assignments
        ADD CONSTRAINT position_assignments_created_by_fkey FOREIGN KEY (created_by) REFERENCES oams.workforce_members(member_id) ON DELETE RESTRICT;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: position_assignments position_assignments_ended_by_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.position_assignments
        ADD CONSTRAINT position_assignments_ended_by_fkey FOREIGN KEY (ended_by) REFERENCES oams.workforce_members(member_id) ON DELETE RESTRICT;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: position_assignments position_assignments_member_id_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.position_assignments
        ADD CONSTRAINT position_assignments_member_id_fkey FOREIGN KEY (member_id) REFERENCES oams.workforce_members(member_id) ON DELETE RESTRICT;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: position_assignments position_assignments_position_id_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.position_assignments
        ADD CONSTRAINT position_assignments_position_id_fkey FOREIGN KEY (position_id) REFERENCES oams.positions(position_id) ON DELETE RESTRICT;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: position_config_versions position_config_versions_approved_by_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.position_config_versions
        ADD CONSTRAINT position_config_versions_approved_by_fkey FOREIGN KEY (approved_by) REFERENCES oams.workforce_members(member_id) ON DELETE RESTRICT;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: position_config_versions position_config_versions_based_on_version_id_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.position_config_versions
        ADD CONSTRAINT position_config_versions_based_on_version_id_fkey FOREIGN KEY (based_on_version_id) REFERENCES oams.position_config_versions(config_version_id) ON DELETE RESTRICT;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: position_config_versions position_config_versions_created_by_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.position_config_versions
        ADD CONSTRAINT position_config_versions_created_by_fkey FOREIGN KEY (created_by) REFERENCES oams.workforce_members(member_id) ON DELETE RESTRICT;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: position_config_versions position_config_versions_position_id_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.position_config_versions
        ADD CONSTRAINT position_config_versions_position_id_fkey FOREIGN KEY (position_id) REFERENCES oams.positions(position_id) ON DELETE RESTRICT;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: positions positions_org_unit_id_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.positions
        ADD CONSTRAINT positions_org_unit_id_fkey FOREIGN KEY (org_unit_id) REFERENCES oams.org_units(org_unit_id) ON DELETE RESTRICT;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: work_item_reports work_item_reports_reported_by_member_id_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.work_item_reports
        ADD CONSTRAINT work_item_reports_reported_by_member_id_fkey FOREIGN KEY (reported_by_member_id) REFERENCES oams.workforce_members(member_id) ON DELETE RESTRICT;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: work_item_reports work_item_reports_retracted_by_member_id_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.work_item_reports
        ADD CONSTRAINT work_item_reports_retracted_by_member_id_fkey FOREIGN KEY (retracted_by_member_id) REFERENCES oams.workforce_members(member_id) ON DELETE RESTRICT;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: work_item_reports work_item_reports_work_item_id_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.work_item_reports
        ADD CONSTRAINT work_item_reports_work_item_id_fkey FOREIGN KEY (work_item_id) REFERENCES oams.work_items(work_item_id) ON DELETE RESTRICT;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: work_items work_items_created_by_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.work_items
        ADD CONSTRAINT work_items_created_by_fkey FOREIGN KEY (created_by) REFERENCES oams.workforce_members(member_id) ON DELETE RESTRICT;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: work_items work_items_org_unit_id_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.work_items
        ADD CONSTRAINT work_items_org_unit_id_fkey FOREIGN KEY (org_unit_id) REFERENCES oams.org_units(org_unit_id) ON DELETE RESTRICT;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: work_items work_items_position_id_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.work_items
        ADD CONSTRAINT work_items_position_id_fkey FOREIGN KEY (position_id) REFERENCES oams.positions(position_id) ON DELETE RESTRICT;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: workforce_audit_events workforce_audit_events_actor_member_id_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.workforce_audit_events
        ADD CONSTRAINT workforce_audit_events_actor_member_id_fkey FOREIGN KEY (actor_member_id) REFERENCES oams.workforce_members(member_id) ON DELETE RESTRICT;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: workforce_sessions workforce_sessions_member_id_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.workforce_sessions
        ADD CONSTRAINT workforce_sessions_member_id_fkey FOREIGN KEY (member_id) REFERENCES oams.workforce_members(member_id) ON DELETE RESTRICT;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: connected_apps connected_apps_pkey; Type: CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.connected_apps
        ADD CONSTRAINT connected_apps_pkey PRIMARY KEY (app_id);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: app_codes app_codes_pkey; Type: CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.app_codes
        ADD CONSTRAINT app_codes_pkey PRIMARY KEY (code);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: app_authorizations app_authorizations_pkey; Type: CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.app_authorizations
        ADD CONSTRAINT app_authorizations_pkey PRIMARY KEY (authorization_id);
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: connected_apps connected_apps_owner_member_id_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.connected_apps
        ADD CONSTRAINT connected_apps_owner_member_id_fkey FOREIGN KEY (owner_member_id) REFERENCES oams.workforce_members(member_id) ON DELETE RESTRICT;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: connected_apps connected_apps_created_by_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.connected_apps
        ADD CONSTRAINT connected_apps_created_by_fkey FOREIGN KEY (created_by) REFERENCES oams.workforce_members(member_id) ON DELETE RESTRICT;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: app_codes app_codes_app_id_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.app_codes
        ADD CONSTRAINT app_codes_app_id_fkey FOREIGN KEY (app_id) REFERENCES oams.connected_apps(app_id) ON DELETE RESTRICT;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: app_codes app_codes_approved_member_id_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.app_codes
        ADD CONSTRAINT app_codes_approved_member_id_fkey FOREIGN KEY (approved_member_id) REFERENCES oams.workforce_members(member_id) ON DELETE RESTRICT;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: app_authorizations app_authorizations_app_id_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.app_authorizations
        ADD CONSTRAINT app_authorizations_app_id_fkey FOREIGN KEY (app_id) REFERENCES oams.connected_apps(app_id) ON DELETE RESTRICT;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: app_authorizations app_authorizations_member_id_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.app_authorizations
        ADD CONSTRAINT app_authorizations_member_id_fkey FOREIGN KEY (member_id) REFERENCES oams.workforce_members(member_id) ON DELETE RESTRICT;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: app_authorizations app_authorizations_approved_by_fkey; Type: FK CONSTRAINT; Schema: oams; Owner: -
--

DO $$
BEGIN
    ALTER TABLE ONLY oams.app_authorizations
        ADD CONSTRAINT app_authorizations_approved_by_fkey FOREIGN KEY (approved_by) REFERENCES oams.workforce_members(member_id) ON DELETE RESTRICT;
EXCEPTION
        WHEN duplicate_object OR duplicate_table OR invalid_table_definition THEN NULL;
END $$;

--
-- Name: app_authorizations_live; Type: INDEX; Schema: oams; Owner: -
--

-- 一个 (应用, 人) 同时只有一票有效；撤销过的留着当历史，不挡下一次允许。
CREATE UNIQUE INDEX IF NOT EXISTS app_authorizations_live
    ON oams.app_authorizations (app_id, member_id) WHERE (revoked_at IS NULL);

--
-- PostgreSQL database dump complete
--

