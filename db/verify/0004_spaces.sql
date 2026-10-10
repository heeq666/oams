-- Verification for migration 0004 (spaces / floor plan).
-- Expects a throwaway database with migrations already applied:
--
--   docker run -d --name workhub-space-test -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=workhub_test \
--       -p 55437:5432 postgres:16-alpine
--   export WORKHUB_DATABASE_URL=postgresql://postgres:pw@localhost:55437/workhub_test
--   python -m workhub.cli migrate
--   docker exec -i workhub-space-test psql -U postgres -d workhub_test -f db/verify/0004_spaces.sql
--
-- Every check rolls itself back, and the negative cases are written as DO blocks
-- that catch the expected error: a constraint nobody can violate is not verified.
-- Anything printing FAIL is a real regression.
SET search_path = workhub, public;

\echo '=== 1. table and indexes ==='
SELECT CASE WHEN count(*) = 1 THEN 'PASS' ELSE 'FAIL' END AS spaces_table_exists
  FROM pg_tables WHERE schemaname = 'workhub' AND tablename = 'spaces';

SELECT CASE WHEN count(*) = 2 THEN 'PASS' ELSE 'FAIL' END AS unique_indexes_present
  FROM pg_indexes WHERE schemaname = 'workhub' AND tablename = 'spaces'
   AND indexname IN ('spaces_one_zone_per_org_unit', 'spaces_one_desk_per_position');

SELECT CASE WHEN count(*) = 1 THEN 'PASS' ELSE 'FAIL' END AS desk_department_trigger_exists
  FROM pg_trigger WHERE NOT tgisinternal
   AND tgrelid = 'spaces'::regclass AND tgname = 'spaces_enforce_desk_department';

\echo '=== 2. throwaway org + position ==='
INSERT INTO org_units (org_code, name, unit_type) VALUES ('FLOOR-TEST', '楼层验证', 'department');
INSERT INTO positions (position_code, title, org_unit_id)
  SELECT 'FT-D1', '验证工位', o.org_unit_id FROM org_units o WHERE o.org_code = 'FLOOR-TEST';
INSERT INTO org_units (org_code, name, unit_type) VALUES ('FLOOR-OTHER', '别的楼层', 'department');

\echo '=== 3. a calibrated zone fits ==='
DO $$
DECLARE n integer;
BEGIN
    INSERT INTO spaces (org_unit_id, kind, x, y, w, h, calibrated)
      SELECT o.org_unit_id, 'zone', 40, 40, 300, 200, true
        FROM org_units o WHERE o.org_code = 'FLOOR-TEST';
    GET DIAGNOSTICS n = ROW_COUNT;
    RAISE NOTICE '% zone insert', CASE WHEN n = 1 THEN 'PASS' ELSE 'FAIL' END;
END $$;

\echo '=== 4. one zone per department ==='
DO $$
BEGIN
    INSERT INTO spaces (org_unit_id, kind, x, y, w, h)
      SELECT o.org_unit_id, 'zone', 400, 40, 100, 100 FROM org_units o WHERE o.org_code = 'FLOOR-TEST';
    RAISE NOTICE 'FAIL second zone for one department was accepted';
EXCEPTION WHEN unique_violation THEN
    RAISE NOTICE 'PASS second zone refused';
END $$;

\echo '=== 5. geometry must stay on the canvas ==='
DO $$
BEGIN
    INSERT INTO spaces (org_unit_id, kind, x, y, w, h)
      SELECT o.org_unit_id, 'zone', 900, 900, 300, 300 FROM org_units o WHERE o.org_code = 'FLOOR-OTHER';
    RAISE NOTICE 'FAIL space outside the canvas was accepted';
EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'PASS space outside the canvas refused';
END $$;

\echo '=== 6. kind and reference must agree ==='
DO $$
BEGIN
    INSERT INTO spaces (org_unit_id, kind, position_id, x, y, w, h)
      SELECT o.org_unit_id, 'zone', p.position_id, 10, 10, 50, 50
        FROM org_units o JOIN positions p ON p.position_code = 'FT-D1'
       WHERE o.org_code = 'FLOOR-TEST';
    RAISE NOTICE 'FAIL zone carrying a position was accepted';
EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'PASS zone with a position refused';
END $$;

DO $$
BEGIN
    INSERT INTO spaces (org_unit_id, kind, x, y, w, h)
      SELECT o.org_unit_id, 'desk', 10, 10, 50, 50 FROM org_units o WHERE o.org_code = 'FLOOR-TEST';
    RAISE NOTICE 'FAIL desk without a position was accepted';
EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'PASS desk without a position refused';
END $$;

\echo '=== 7. a desk must sit in its own department ==='
DO $$
BEGIN
    INSERT INTO spaces (org_unit_id, kind, position_id, x, y, w, h)
      SELECT o.org_unit_id, 'desk', p.position_id, 10, 10, 50, 50
        FROM org_units o JOIN positions p ON p.position_code = 'FT-D1'
       WHERE o.org_code = 'FLOOR-OTHER';
    RAISE NOTICE 'FAIL desk in another department was accepted';
EXCEPTION WHEN raise_exception THEN
    RAISE NOTICE 'PASS desk in another department refused (%)', SQLERRM;
END $$;

\echo '=== 8. one desk per position ==='
DO $$
BEGIN
    INSERT INTO spaces (org_unit_id, kind, position_id, x, y, w, h)
      SELECT o.org_unit_id, 'desk', p.position_id, 100, 100, 60, 40
        FROM org_units o JOIN positions p ON p.position_code = 'FT-D1'
       WHERE o.org_code = 'FLOOR-TEST';
    INSERT INTO spaces (org_unit_id, kind, position_id, x, y, w, h)
      SELECT o.org_unit_id, 'desk', p.position_id, 300, 100, 60, 40
        FROM org_units o JOIN positions p ON p.position_code = 'FT-D1'
       WHERE o.org_code = 'FLOOR-TEST';
    RAISE NOTICE 'FAIL second desk for one position was accepted';
EXCEPTION WHEN unique_violation THEN
    RAISE NOTICE 'PASS second desk refused';
END $$;

\echo '=== 9. deleting the department takes its plan with it ==='
-- FLOOR-OTHER keeps no positions, so deleting it is allowed and must cascade to
-- its plan. (FLOOR-TEST could never be deleted: positions_org_unit_id_fkey is
-- RESTRICT, which is the behaviour we want -- a department with posts stays.)
INSERT INTO spaces (org_unit_id, kind, x, y, w, h)
  SELECT o.org_unit_id, 'zone', 10, 10, 80, 80 FROM org_units o WHERE o.org_code = 'FLOOR-OTHER';
DELETE FROM org_units WHERE org_code = 'FLOOR-OTHER';
SELECT CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS cascade_left_no_orphaned_space,
       count(*) AS orphaned
  FROM spaces s
 WHERE NOT EXISTS (SELECT 1 FROM org_units o WHERE o.org_unit_id = s.org_unit_id);

DELETE FROM spaces;
DELETE FROM positions WHERE position_code = 'FT-D1';
DELETE FROM org_units WHERE org_code IN ('FLOOR-TEST', 'FLOOR-OTHER');

\echo '=== done ==='
