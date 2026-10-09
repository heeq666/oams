-- Step 1: create the dedicated database and roles.
-- Run as the PostgreSQL superuser, against any database (for example `postgres`):
--     psql -U postgres -p 5433 -d postgres -f 01_create_database.sql
--
-- This script deliberately contains no passwords. Set them interactively afterwards
-- so they never land in shell history, this file, or the repository:
--     psql -U postgres -p 5433 -c "\password workhub_migrator"
--     psql -U postgres -p 5433 -c "\password workhub_runtime"
-- A role created with LOGIN and no password cannot authenticate until this is done.

CREATE ROLE workhub_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE ROLE workhub_runtime  LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;

CREATE DATABASE workhub OWNER workhub_migrator;

-- Keep other roles out of the new database by default.
REVOKE ALL ON DATABASE workhub FROM PUBLIC;
GRANT CONNECT ON DATABASE workhub TO workhub_migrator;
GRANT CONNECT ON DATABASE workhub TO workhub_runtime;
