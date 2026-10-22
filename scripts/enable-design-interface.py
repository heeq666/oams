"""Enable the real design interface in the running local system."""
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from workhub.descriptor import validate_descriptor

descriptor = json.loads(Path("data/design-style-development.json").read_text(encoding="utf-8"))
with psycopg.connect(os.environ["WORKHUB_DATABASE_URL"], row_factory=dict_row) as conn:
    org = conn.execute("SELECT org_unit_id FROM workhub.org_units WHERE org_code = 'DESIGN'").fetchone()["org_unit_id"]
    admin = conn.execute("SELECT member_id FROM workhub.workforce_members WHERE employee_code = 'ADMIN001'").fetchone()["member_id"]
    position = conn.execute("SELECT position_id FROM workhub.positions WHERE position_code = 'DESIGN_LEAD'").fetchone()["position_id"]
    codes = {r["org_code"] for r in conn.execute("SELECT org_code FROM workhub.org_units").fetchall()}
    positions = {r["position_code"] for r in conn.execute("SELECT position_code FROM workhub.positions").fetchall()}
    problems = validate_descriptor(descriptor, known_org_unit_codes=codes, known_position_codes=positions)
    if problems:
        raise SystemExit("rejected: " + "; ".join(problems))
    conn.execute("UPDATE workhub.interfaces SET status='archived' WHERE slug='design-style-development' AND status='active'")
    version = conn.execute(
        """SELECT COALESCE(max(version_no), 0) + 1 FROM workhub.interfaces
            WHERE org_unit_id = %s AND slug = 'design-style-development'""", (org,)).fetchone()["?column?"]
    row = conn.execute(
        """INSERT INTO workhub.interfaces
               (org_unit_id, slug, name, status, visibility, schema_version, descriptor,
                position_id, version_no, created_by, updated_by)
           VALUES (%s, 'design-style-development', %s, 'active', 'department', %s, %s, %s, %s, %s, %s)
           RETURNING interface_id""",
        (org, descriptor["name"], descriptor["schema_version"], Jsonb(descriptor),
         position, version, admin, admin)).fetchone()
print("enabled", row["interface_id"], "version", version)
