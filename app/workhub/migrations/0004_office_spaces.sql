-- Company Workhub: the floor plan -- departments as zones, positions as desks.
-- Apply only to the dedicated workhub database, never to the product-center database.
--
-- Requires 0002 to be applied first (desks reference positions).
--
-- Why coordinates are manual and not derived: a plan drawn by counting positions
-- and laying them out on a grid looks exactly like information while carrying
-- none. Nobody sat there. So a zone only exists when somebody with the floor in
-- front of them enters where it actually is, and until then it is drawn dashed
-- and labelled uncalibrated. Missing data stays visibly missing.

CREATE SCHEMA IF NOT EXISTS workhub;
SET LOCAL search_path = workhub, public;

-- ---------------------------------------------------------------------------
-- 1. Spaces
--
-- Two kinds share one table because they are edited together and are unique
-- the same way: a department has at most one zone, a position at most one desk.
--
-- Coordinates live on a 0-1000 integer grid, which keeps them portable between
-- renderers and obvious to edit by hand.
--   kind = 'zone'  x/y/w/h are absolute on the floor canvas
--   kind = 'desk'  x/y/w/h are INSIDE the zone box (0-1000 local), so moving a
--                  department across the floor never drags its desks along or
--                  require re-entering them
-- ---------------------------------------------------------------------------

CREATE TABLE spaces (
    space_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_unit_id  uuid NOT NULL REFERENCES org_units(org_unit_id) ON DELETE CASCADE,
    kind         text NOT NULL CHECK (kind IN ('zone', 'desk')),
    -- Desks belong to a position, the way work items do. A desk outlives whoever
    -- sits at it; someone leaving never leaves the plan without an owner.
    position_id  uuid REFERENCES positions(position_id) ON DELETE CASCADE,
    label        text CHECK (label IS NULL OR btrim(label) <> ''),
    x            integer NOT NULL CHECK (x BETWEEN 0 AND 999),
    y            integer NOT NULL CHECK (y BETWEEN 0 AND 999),
    w            integer NOT NULL CHECK (w > 0),
    h            integer NOT NULL CHECK (h > 0),
    -- False until somebody reads it off the actual floor. Rendered dashed.
    calibrated   boolean NOT NULL DEFAULT false,
    sort_order   integer NOT NULL DEFAULT 0,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT spaces_within_canvas CHECK (x + w <= 1000 AND y + h <= 1000),
    CONSTRAINT spaces_kind_matches_reference CHECK (
        (kind = 'zone' AND position_id IS NULL)
        OR (kind = 'desk' AND position_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX spaces_one_zone_per_org_unit
    ON spaces (org_unit_id) WHERE kind = 'zone';
CREATE UNIQUE INDEX spaces_one_desk_per_position
    ON spaces (position_id) WHERE kind = 'desk';

CREATE INDEX spaces_by_org_unit ON spaces (org_unit_id, kind, sort_order);

COMMENT ON TABLE spaces IS
    'Manual floor coordinates: one zone per department, one desk per position. '
    'Uncalibrated rows are placeholders and must render as such.';
COMMENT ON COLUMN spaces.calibrated IS
    'false = placeholder geometry nobody has verified against the real floor';

-- ---------------------------------------------------------------------------
-- 2. A desk may only belong to the department that owns the position.
--
-- This cannot be a CHECK: PostgreSQL refuses subqueries in check constraints.
-- A trigger can also raise a message that says what is actually wrong, which a
-- bare constraint violation never does.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION spaces_enforce_desk_department() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.position_id IS NOT NULL
       AND NOT EXISTS (SELECT 1
                         FROM workhub.positions p
                        WHERE p.position_id = NEW.position_id
                          AND p.org_unit_id = NEW.org_unit_id) THEN
        RAISE EXCEPTION
            'a desk must belong to a position of its own department (position %, org unit %)',
            NEW.position_id, NEW.org_unit_id;
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END $$;

CREATE TRIGGER spaces_enforce_desk_department
    BEFORE INSERT OR UPDATE ON spaces
    FOR EACH ROW EXECUTE FUNCTION spaces_enforce_desk_department();
