-- Company Workhub: draft trial data for interfaces that are not yet approved.
-- Apply only to the dedicated workhub database, never to the product-center database.
--
-- Why this exists: a department has to be able to fill a screen it is still
-- building, otherwise nobody can test it before asking for approval. But trial
-- entries must never reach the company aggregate -- a manager reading "what is
-- stuck" must not be reading somebody's test row.
--
-- So an entry records which mode it was made in, and the aggregate only ever
-- reads 'live'. The draft rows stay visible to the department that wrote them.
SET LOCAL search_path = workhub, public;

ALTER TABLE interface_entries
    ADD COLUMN entry_mode text NOT NULL DEFAULT 'live'
        CHECK (entry_mode IN ('live', 'draft'));

-- Existing rows predate the split and were all real submissions.
UPDATE interface_entries SET entry_mode = 'live' WHERE entry_mode IS NULL;

-- The aggregate reads live entries only; the draft list reads drafts only.
CREATE INDEX interface_entries_mode_idx ON interface_entries(interface_id, entry_mode, reported_at DESC);
