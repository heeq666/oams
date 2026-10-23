-- Company Workhub: a rejection carries a code, not only a sentence.
--
-- Why a code on top of the note: "退回" without a reason class cannot be counted.
-- A department asking "what do we keep getting wrong" has no answer if every
-- rejection is free prose, and the platform cannot tell a field-mapping mistake
-- from a layout mistake when deciding what to fix in the module library.
--
-- The seven codes are the same ones the validator prefixes onto its problems
-- (see app/workhub/review.py and descriptor.py), so a department running
-- "workhub plugin check" and a department reading a rejection see one vocabulary.
--
-- Past decisions keep review_code NULL: back-filling a code by guessing from the
-- prose would invent a fact the reviewer never said.
SET LOCAL search_path = workhub, public;

ALTER TABLE interfaces ADD COLUMN review_code text;

ALTER TABLE interfaces
    ADD CONSTRAINT interfaces_review_code_check
        CHECK (review_code IS NULL OR review_code IN (
            'FIELD_UNMAPPED',
            'STATUS_UNMAPPED',
            'METRIC_UNDEFINED',
            'CAPABILITY_ESCALATION',
            'MODULE_NOT_WHITELISTED',
            'LAYOUT_TOO_DEEP',
            'DATA_RULE_VIOLATION'
        ));

-- The old rule let these pile up: every "new version" created a draft and left
-- the previous draft untouched, so one page could carry eight open versions at
-- once.  The index below refuses that, which means the existing pile has to be
-- collapsed first -- otherwise this migration fails on the very database it is
-- meant to fix.  The newest version is the one a person would have carried on
-- with, so it stays open and the others are archived rather than deleted: the
-- history of what was proposed is still there to read.
UPDATE interfaces AS i
   SET status = 'archived',
       review_note = COALESCE(NULLIF(i.review_note, ''), '') 
                     || CASE WHEN COALESCE(i.review_note, '') = '' THEN '' ELSE ' ｜ ' END
                     || '版本收敛：同一页面只保留一版在改或待审，旧版本已归档',
       updated_at = now()
 WHERE i.status IN ('draft', 'pending')
   AND i.interface_id NOT IN (
        SELECT DISTINCT ON (org_unit_id, slug) interface_id
          FROM interfaces
         WHERE status IN ('draft', 'pending')
         ORDER BY org_unit_id, slug, version_no DESC, created_at DESC);

-- "Only one proposal at a time per slug": a department that opens two drafts of
-- the same page gets two diffs against one live version and cannot tell which
-- one the reviewer is looking at.  The unique index is partial so archived
-- versions do not count and drafts of *different* slugs stay independent.
CREATE UNIQUE INDEX interfaces_one_open_version_idx
    ON interfaces(org_unit_id, slug)
    WHERE status IN ('draft', 'pending');
