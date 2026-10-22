-- Company Workhub: reviewing department pages is its own permission.
--
-- Why not reuse workforce.grants/manage: that permission also lets its holder
-- change authorisations and member status. A department lead who should approve
-- a page is a business reviewer, not a security administrator, and handing over
-- the administrator permission to get review done would quietly hand over the
-- power to grant anything to anyone.
--
-- So review becomes an ordinary resource. Nothing is granted here: who reviews is
-- a business decision, made explicitly per person or per position.
SET LOCAL search_path = workhub, public;

-- Backfill for existing deployments: whoever could review before (the security
-- administrators) keeps that ability, now expressed as the narrow permission.
INSERT INTO permission_grants
    (subject_type, position_id, member_id, resource, action, scope_type, scope_org_unit_id,
     scope_definition, effect, valid_from, reason, created_by)
SELECT g.subject_type, g.position_id, g.member_id, 'workforce.interfaces', 'review',
       g.scope_type, g.scope_org_unit_id, g.scope_definition, 'allow', now(),
       'backfill: page review split out of workforce.grants/manage', g.created_by
  FROM permission_grants g
 WHERE g.resource = 'workforce.grants'
   AND g.action = 'manage'
   AND g.revoked_at IS NULL
   AND NOT EXISTS (
        SELECT 1 FROM permission_grants existing
         WHERE existing.resource = 'workforce.interfaces'
           AND existing.action = 'review'
           AND existing.revoked_at IS NULL
           AND existing.position_id IS NOT DISTINCT FROM g.position_id
           AND existing.member_id IS NOT DISTINCT FROM g.member_id);
