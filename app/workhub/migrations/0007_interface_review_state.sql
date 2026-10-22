-- Company Workhub: an explicit review state between "a department is building it"
-- and "the company relies on it".
--
-- Why a separate state instead of reusing draft: a department needs to tell the
-- company "this one is finished, please look". Without that signal there is no
-- queue for a reviewer, and approval would have to be inferred from silence.
--
-- draft   -- the department is still building; only the department sees it
-- pending -- the department says it is done; still only the department sees it,
--            and it still collects trial entries, because they must keep testing
-- active  -- approved; its live entries now feed the company aggregate
SET LOCAL search_path = workhub, public;

ALTER TABLE interfaces DROP CONSTRAINT interfaces_status_check;
ALTER TABLE interfaces
    ADD CONSTRAINT interfaces_status_check
        CHECK (status IN ('draft', 'pending', 'active', 'archived'));

-- The reviewer's note is kept where the decision is, so a rejected page can say
-- why without somebody hunting through the audit log.
ALTER TABLE interfaces ADD COLUMN review_note text;

CREATE INDEX interfaces_pending_idx ON interfaces(org_unit_id) WHERE status = 'pending';
