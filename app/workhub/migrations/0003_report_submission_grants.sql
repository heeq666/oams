-- Company Workhub: grant the report submission action to existing administrators.
-- Apply only to the dedicated workhub database, never to the product-center database.
--
-- Why this exists: `bootstrap-admin` granted `workforce.reports` with `read` only,
-- because the last reports with `submitted_via` provenance did not exist yet when
-- the bootstrap role list was written. Migration 0002 added the reports table, so
-- the write action now has to be granted.
--
-- `bootstrap-admin` can only ever run once, so existing deployments cannot pick
-- the new action up from it. This backfill derives the new grant from whatever
-- already holds the all-scope administrator grant, which keeps the one property
-- that matters: whoever can manage grants can also submit reports.

CREATE SCHEMA IF NOT EXISTS workhub;
SET LOCAL search_path = workhub, public;

INSERT INTO workhub.permission_grants
    (subject_type, position_id, member_id, resource, action, scope_type, scope_org_unit_id,
     scope_definition, effect, valid_from, reason, created_by)
SELECT g.subject_type, g.position_id, g.member_id, 'workforce.reports', 'submit',
       g.scope_type, g.scope_org_unit_id, g.scope_definition, 'allow', now(),
       'backfill: report submission action added with migration 0002', g.created_by
  FROM workhub.permission_grants g
 WHERE g.resource = 'workforce.grants'
   AND g.action = 'manage'
   AND g.scope_type = 'all'
   AND g.revoked_at IS NULL
   AND NOT EXISTS (
        SELECT 1 FROM workhub.permission_grants existing
         WHERE existing.resource = 'workforce.reports'
           AND existing.action = 'submit'
           AND existing.revoked_at IS NULL
           AND existing.position_id IS NOT DISTINCT FROM g.position_id
           AND existing.member_id IS NOT DISTINCT FROM g.member_id);
