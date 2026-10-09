-- Step 3: grant runtime access AFTER `workhub migrate` has created the tables.
-- Run as the PostgreSQL superuser, against the workhub database:
--     psql -U postgres -p 5433 -d workhub -f 03_grant_runtime.sql
--
-- The runtime role reads work items and writes progress reports. It must never be
-- able to rewrite or delete audit history: if an operator can edit the audit log,
-- the log is decoration.

GRANT USAGE ON SCHEMA workhub TO workhub_runtime;

-- ALL TABLES covers views, so work_item_current_state is included.
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA workhub TO workhub_runtime;

REVOKE UPDATE, DELETE ON workhub.workforce_audit_events FROM workhub_runtime;
REVOKE UPDATE, DELETE ON workhub.schema_migrations FROM workhub_runtime;
REVOKE UPDATE, DELETE ON workhub.sensitive_field_registry FROM workhub_runtime;
