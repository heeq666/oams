-- Step 2: prepare the workhub database before applying migrations.
-- Run as the PostgreSQL superuser, against the new database:
--     psql -U postgres -p 5433 -d workhub -f 02_prepare_schema.sql
--
-- The migration calls CREATE EXTENSION for pgcrypto and btree_gist, but neither is
-- a trusted extension, so workhub_migrator cannot create them. Installing them here
-- lets the migration's IF NOT EXISTS clause skip the creation instead of failing.
--
-- The ALTER DEFAULT PRIVILEGES statements must run BEFORE the migration so that
-- every table the migration creates is granted to the runtime role automatically.

CREATE SCHEMA IF NOT EXISTS workhub AUTHORIZATION workhub_migrator;

CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA workhub;
CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA workhub;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA workhub TO workhub_runtime;

ALTER DEFAULT PRIVILEGES FOR ROLE workhub_migrator IN SCHEMA workhub
    GRANT SELECT, INSERT, UPDATE ON TABLES TO workhub_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE workhub_migrator IN SCHEMA workhub
    GRANT USAGE, SELECT ON SEQUENCES TO workhub_runtime;
