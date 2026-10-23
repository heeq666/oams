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

-- "Only one proposal at a time per slug": a department that opens two drafts of
-- the same page gets two diffs against one live version and cannot tell which
-- one the reviewer is looking at.  The unique index is partial so archived
-- versions do not count and drafts of *different* slugs stay independent.
CREATE UNIQUE INDEX interfaces_one_open_version_idx
    ON interfaces(org_unit_id, slug)
    WHERE status IN ('draft', 'pending');
