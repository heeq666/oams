"""Seed the hands-on environment: enable the design department interface.

Idempotent: rerunning archives the previous live version and enables a new one,
exactly the way the setup page behaves.

The descriptor on disk is schema 0.2. If somebody drops a 0.1 file back in here it
is upgraded first with the same pure function the CLI uses, and the machine-made
decisions are printed, so a stale demo file can never be seeded back into the DB
as a page the renderer refuses to open.
"""
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import psycopg
from psycopg.types.json import Jsonb
from workhub.descriptor import validate_descriptor
from workhub.upgrade import LEGACY_SCHEMA_VERSION, upgrade_descriptor

SOURCE = Path("data/live-design-interface.json")
descriptor = json.loads(SOURCE.read_text(encoding="utf-8"))

if descriptor.get("schema_version") == LEGACY_SCHEMA_VERSION:
    descriptor, hints = upgrade_descriptor(descriptor)
    print(f"{SOURCE}: schema 0.1 已自动升级到 {descriptor['schema_version']}，请确认：")
    for hint in hints:
        print("  -", hint)

with psycopg.connect(os.environ["WORKHUB_DATABASE_URL"], row_factory=psycopg.rows.dict_row) as conn:
    org = conn.execute("SELECT org_unit_id FROM workhub.org_units WHERE org_code = 'DESIGN'").fetchone()["org_unit_id"]
    admin = conn.execute("SELECT member_id FROM workhub.workforce_members WHERE employee_code = 'ADMIN001'").fetchone()["member_id"]
    position = conn.execute("SELECT position_id FROM workhub.positions WHERE position_code = 'DESIGN_LEAD'").fetchone()["position_id"]
    codes = {row["org_code"] for row in conn.execute("SELECT org_code FROM workhub.org_units").fetchall()}
    positions = {row["position_code"] for row in conn.execute("SELECT position_code FROM workhub.positions").fetchall()}
    problems = validate_descriptor(descriptor, known_org_unit_codes=codes, known_position_codes=positions)
    if problems:
        raise SystemExit("descriptor rejected: " + "; ".join(problems))
    conn.execute("UPDATE workhub.interfaces SET status='archived' WHERE slug='design-sampling' AND status='active'")
    version = conn.execute(
        """SELECT COALESCE(max(version_no), 0) + 1 FROM workhub.interfaces
            WHERE org_unit_id = %s AND slug = 'design-sampling'""", (org,)).fetchone()["?column?"]
    row = conn.execute(
        """INSERT INTO workhub.interfaces
               (org_unit_id, slug, name, status, visibility, schema_version, descriptor,
                position_id, version_no, created_by, updated_by)
           VALUES (%s, 'design-sampling', %s, 'active', 'department', %s, %s, %s, %s, %s, %s)
           RETURNING interface_id""",
        (org, descriptor["name"], descriptor["schema_version"], Jsonb(descriptor),
         position, version, admin, admin)).fetchone()
print("interface enabled:", row["interface_id"], "version", version,
      "schema", descriptor["schema_version"])
