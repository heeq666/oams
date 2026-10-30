-- Company Workhub: page review happens in two stages, and the database says which.
--
-- Until now "pending" meant one thing and three routes treated every reviewer as
-- company-wide: a department lead with the review grant saw every department's
-- drafts, and the platform could not tell "waiting for the department to finish"
-- from "waiting for us to sign off". Adding a second permission point alone would
-- not fix that -- the queue has to know which approval it is waiting for.
--
-- The status enum stays exactly as it is (draft / pending / active / archived):
-- migrations, tests and the screens all speak it, and a second enum would mean
-- every reader learning a second vocabulary for the same thing. `pending` plus
-- review_stage answers "waiting for whom" without touching it.
--
-- Existing pending rows are first-level: they were submitted under the old rule
-- and nobody has signed them off yet, so the default back-fills them honestly.
SET LOCAL search_path = workhub, public;

ALTER TABLE interfaces ADD COLUMN review_stage text NOT NULL DEFAULT 'department';

ALTER TABLE interfaces
    ADD CONSTRAINT interfaces_review_stage_check
        CHECK (review_stage IN ('department', 'company'));

COMMENT ON COLUMN interfaces.review_stage IS
    'Which approval status=''pending'' is waiting for: department (first level) '
    'or company (final). Only meaningful while pending; a draft or active row '
    'reads department, which is where a new submission starts.';

-- The queue asks "what is pending at this stage in these departments", which is
-- exactly this index; the old pending index cannot answer the stage.
CREATE INDEX interfaces_pending_stage_idx
    ON interfaces(review_stage, org_unit_id) WHERE status = 'pending';


-- Who signs at the company stage, decided once here rather than left to whoever
-- reads the plan: the platform reviewers are the positions that already hold
-- workforce.grants/manage -- the same people 0008 back-filled page review for,
-- now expressed as the narrow permission. Department leads hold `review` without
-- grants/manage and therefore get only the first level, which is the point.
-- Nothing is granted to members directly: a person's range comes from the post
-- they hold, so a reorganisation carries the rights with it.
INSERT INTO permission_grants
    (subject_type, position_id, member_id, resource, action, scope_type, scope_org_unit_id,
     scope_definition, effect, valid_from, reason, created_by)
SELECT g.subject_type, g.position_id, g.member_id, 'workforce.interfaces', 'review_company',
       g.scope_type, g.scope_org_unit_id, g.scope_definition, 'allow', now(),
       'backfill: the platform side of the page-review split', g.created_by
  FROM permission_grants g
 WHERE g.resource = 'workforce.grants'
   AND g.action = 'manage'
   AND g.revoked_at IS NULL
   AND NOT EXISTS (
        SELECT 1 FROM permission_grants existing
         WHERE existing.resource = 'workforce.interfaces'
           AND existing.action = 'review_company'
           AND existing.revoked_at IS NULL
           AND existing.position_id IS NOT DISTINCT FROM g.position_id
           AND existing.member_id IS NOT DISTINCT FROM g.member_id);
