-- 约束验证记录（双角色 / 增量迁移时期：0002）。对象名按今天的 oams schema 写，
-- 当年那套分角色建库脚本已于 2026-11-02 删除，建库建表现在只看 `oams db init`。
-- Run ONLY against a disposable database: it inserts test rows and never cleans up.
--
--   docker run -d --name <测试容器> -e POSTGRES_PASSWORD=pw \
--       -e POSTGRES_DB=<测试库> -p <测试端口>:5432 postgres:16-alpine
--   OAMS_DATABASE_URL=postgresql://postgres:pw@localhost:<测试端口>/<测试库> oams db init
--   psql -f db/verify/0002_constraints.sql
--
-- Every check prints PASS-n or FAIL-n. A FAIL means the constraint is not actually
-- enforced, even though the migration applied without error.

DO $$
DECLARE
    v_org uuid; v_pos uuid; v_other_org uuid;
    v_m1 uuid; v_m2 uuid; v_m3 uuid;
    v_item uuid; v_r1 uuid; v_r2 uuid;
    v_view_value smallint; v_view_via text;
BEGIN
    INSERT INTO oams.org_units (org_code, name, unit_type) VALUES ('T-CO','测试公司','company') RETURNING org_unit_id INTO v_org;
    INSERT INTO oams.org_units (org_code, name, unit_type, parent_org_unit_id) VALUES ('T-OTHER','另一个部门','department', v_org) RETURNING org_unit_id INTO v_other_org;
    INSERT INTO oams.workforce_members (employee_code, display_name, employment_status) VALUES ('T-1','张三','active') RETURNING member_id INTO v_m1;
    INSERT INTO oams.workforce_members (employee_code, display_name, employment_status) VALUES ('T-2','李四','active') RETURNING member_id INTO v_m2;
    INSERT INTO oams.workforce_members (employee_code, display_name, employment_status) VALUES ('T-3','王五','active') RETURNING member_id INTO v_m3;
    INSERT INTO oams.positions (position_code, title, org_unit_id) VALUES ('T-HR','人事专员', v_org) RETURNING position_id INTO v_pos;

    INSERT INTO oams.position_assignments (member_id, position_id, assignment_type, status, starts_at, reason)
        VALUES (v_m1, v_pos, 'primary', 'active', now(), 'setup');

    BEGIN
        INSERT INTO oams.position_assignments (member_id, position_id, assignment_type, status, starts_at, reason)
            VALUES (v_m2, v_pos, 'primary', 'active', now(), 'second primary');
        RAISE NOTICE 'FAIL-1 同岗位两条 primary 未被拦截';
    EXCEPTION WHEN exclusion_violation THEN
        RAISE NOTICE 'PASS-1 同岗位第二条 primary 被拦截';
    END;

    BEGIN
        INSERT INTO oams.position_assignments (member_id, position_id, assignment_type, status, starts_at, reason)
            VALUES (v_m2, v_pos, 'concurrent', 'active', now(), 'concurrent holder');
        RAISE NOTICE 'PASS-2 concurrent 兼岗允许';
    EXCEPTION WHEN others THEN
        RAISE NOTICE 'FAIL-2 concurrent 被误拦截: %', SQLERRM;
    END;

    BEGIN
        INSERT INTO oams.position_assignments (member_id, position_id, assignment_type, status, starts_at, ends_at, reason)
            VALUES (v_m3, v_pos, 'acting', 'active', now(), now() + interval '91 days', 'too long');
        RAISE NOTICE 'FAIL-3 acting 91 天未被拦截';
    EXCEPTION WHEN check_violation THEN
        RAISE NOTICE 'PASS-3 acting 91 天被拦截';
    END;

    BEGIN
        INSERT INTO oams.position_assignments (member_id, position_id, assignment_type, status, starts_at, ends_at, reason)
            VALUES (v_m3, v_pos, 'acting', 'active', now(), now() + interval '90 days', 'within cap');
        RAISE NOTICE 'PASS-4 acting 90 天允许';
    EXCEPTION WHEN others THEN
        RAISE NOTICE 'FAIL-4 acting 90 天被误拦截: %', SQLERRM;
    END;

    BEGIN
        INSERT INTO oams.work_items (org_unit_id, position_id, title, status)
            VALUES (v_org, v_pos, '无原因的阻塞项', 'blocked');
        RAISE NOTICE 'FAIL-5 blocked 无原因未被拦截';
    EXCEPTION WHEN check_violation THEN
        RAISE NOTICE 'PASS-5 blocked 无原因被拦截';
    END;

    BEGIN
        INSERT INTO oams.work_items (org_unit_id, position_id, title)
            VALUES (v_other_org, v_pos, '部门与岗位不一致');
        RAISE NOTICE 'FAIL-6 部门与岗位不一致未被拦截';
    EXCEPTION WHEN others THEN
        IF SQLERRM LIKE '%disagrees%' THEN
            RAISE NOTICE 'PASS-6 部门与岗位不一致被触发器拦截';
        ELSE
            RAISE NOTICE 'FAIL-6 被拦截但原因不对: %', SQLERRM;
        END IF;
    END;

    INSERT INTO oams.work_items (org_unit_id, position_id, title, status, blocked_reason, needs_decision)
        VALUES (v_org, v_pos, '招聘实施', 'blocked', '编制未批', true) RETURNING work_item_id INTO v_item;

    BEGIN
        INSERT INTO oams.work_item_reports (work_item_id, reported_by_member_id, submitted_via, progress_value, evidence)
            VALUES (v_item, v_m1, 'human', 30, '   ');
        RAISE NOTICE 'FAIL-7 空白 evidence 未被拦截';
    EXCEPTION WHEN check_violation THEN
        RAISE NOTICE 'PASS-7 空白 evidence 被拦截';
    END;

    BEGIN
        INSERT INTO oams.work_item_reports (work_item_id, reported_by_member_id, submitted_via, progress_value, evidence)
            VALUES (v_item, v_m1, 'human', 120, '依据');
        RAISE NOTICE 'FAIL-8 进度 120 未被拦截';
    EXCEPTION WHEN check_violation THEN
        RAISE NOTICE 'PASS-8 进度 120 被拦截';
    END;

    INSERT INTO oams.work_item_reports (work_item_id, reported_by_member_id, submitted_via, progress_value, note, evidence, reported_at)
        VALUES (v_item, v_m1, 'human', 30, '已发 offer', '邮件确认', now()) RETURNING report_id INTO v_r1;

    INSERT INTO oams.work_item_reports (work_item_id, reported_by_member_id, submitted_via, progress_value, note, evidence, reported_at)
        VALUES (v_item, v_m1, 'agent', 70, 'AI 汇总', 'AI 读取招聘系统', now() + interval '1 minute') RETURNING report_id INTO v_r2;

    SELECT latest_progress_value, latest_submitted_via INTO v_view_value, v_view_via
      FROM oams.work_item_current_state WHERE work_item_id = v_item;
    IF v_view_value = 70 AND v_view_via = 'agent' THEN
        RAISE NOTICE 'PASS-9 视图取最新申报 (值=%, 来源=%)', v_view_value, v_view_via;
    ELSE
        RAISE NOTICE 'FAIL-9 视图未取最新 (值=%, 来源=%)', v_view_value, v_view_via;
    END IF;

    UPDATE oams.work_item_reports
       SET status = 'retracted', retracted_at = now(), retracted_by_member_id = v_m1
     WHERE report_id = v_r2;

    SELECT latest_progress_value, latest_submitted_via INTO v_view_value, v_view_via
      FROM oams.work_item_current_state WHERE work_item_id = v_item;
    IF v_view_value = 30 AND v_view_via = 'human' THEN
        RAISE NOTICE 'PASS-10 撤回后回退到上一条 (值=%, 来源=%)', v_view_value, v_view_via;
    ELSE
        RAISE NOTICE 'FAIL-10 撤回后视图错误 (值=%, 来源=%)', v_view_value, v_view_via;
    END IF;

    INSERT INTO oams.approval_records (subject_type, subject_id, approver_member_id, approver_role, decision)
        VALUES ('position_assignment', v_item, v_m1, 'department_head', 'approved');
    BEGIN
        INSERT INTO oams.approval_records (subject_type, subject_id, approver_member_id, approver_role, decision)
            VALUES ('position_assignment', v_item, v_m1, 'hr', 'approved');
        RAISE NOTICE 'FAIL-11 同一审批人重复签名未被拦截';
    EXCEPTION WHEN unique_violation THEN
        RAISE NOTICE 'PASS-11 同一审批人重复签名被拦截';
    END;

    BEGIN
        INSERT INTO oams.approval_records (subject_type, subject_id, approver_member_id, approver_role, decision)
            VALUES ('position_assignment', v_item, v_m2, 'hr', 'approved');
        RAISE NOTICE 'PASS-12 第二位审批人（HR）可签名，双签可表达';
    EXCEPTION WHEN others THEN
        RAISE NOTICE 'FAIL-12 第二位审批人被误拦截: %', SQLERRM;
    END;
END $$;

SELECT count(*) AS sensitive_fields FROM oams.sensitive_field_registry;
-- 旧迁移台账（双角色时期有；现在 oams db init 既不建也不删它）：
-- SELECT version FROM schema_migrations ORDER BY version;
