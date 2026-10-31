"""Seed the hands-on environment: enable a department interface from a descriptor file.

Idempotent: rerunning archives the previous live version and enables a new one,
exactly the way the setup page behaves.

    .venv/bin/python scripts/seed-live-interface.py                          # 默认样本（tests/fixtures 里那份）
    .venv/bin/python scripts/seed-live-interface.py tests/fixtures/department-weekly-test.json

The slug, the department and the position all come from the descriptor itself, so
one script seeds every demo page and the file name never has to agree with the slug.

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
from oams.descriptor import validate_descriptor
from oams.upgrade import LEGACY_SCHEMA_VERSION, upgrade_descriptor

#: 默认样本放在 `tests/fixtures/` 里：`data/` 只保留新克隆真会种进去的东西，
#: 否则一份克隆下来没有的路径会让脚本在第一次试用就报错。
SOURCE = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("tests/fixtures/live-design-interface.json")
descriptor = json.loads(SOURCE.read_text(encoding="utf-8"))

if descriptor.get("schema_version") == LEGACY_SCHEMA_VERSION:
    descriptor, hints = upgrade_descriptor(descriptor)
    print(f"{SOURCE}: schema 0.1 已自动升级到 {descriptor['schema_version']}，请确认：")
    for hint in hints:
        print("  -", hint)

slug = descriptor["interface_id"]
org_code = descriptor["org_unit_code"]
position_code = (descriptor.get("position_codes") or [None])[0]

with psycopg.connect(os.environ["OAMS_DATABASE_URL"], row_factory=psycopg.rows.dict_row) as conn:
    org = conn.execute("SELECT org_unit_id FROM oams.org_units WHERE org_code = %s",
                       (org_code,)).fetchone()["org_unit_id"]
    admin = conn.execute("SELECT member_id FROM oams.workforce_members WHERE employee_code = 'ADMIN001'").fetchone()["member_id"]
    position = None
    if position_code:
        position = conn.execute("SELECT position_id FROM oams.positions WHERE position_code = %s",
                                (position_code,)).fetchone()["position_id"]
    codes = {row["org_code"] for row in conn.execute("SELECT org_code FROM oams.org_units").fetchall()}
    positions = {row["position_code"] for row in conn.execute("SELECT position_code FROM oams.positions").fetchall()}
    problems = validate_descriptor(descriptor, known_org_unit_codes=codes, known_position_codes=positions)
    if problems:
        raise SystemExit("descriptor rejected: " + "; ".join(problems))
    version = conn.execute(
        """SELECT COALESCE(max(version_no), 0) + 1 FROM oams.interfaces
            WHERE org_unit_id = %s AND slug = %s""", (org, slug)).fetchone()["?column?"]
    previous = conn.execute(
        """SELECT interface_id FROM oams.interfaces
            WHERE org_unit_id = %s AND slug = %s AND status = 'active'""", (org, slug)).fetchone()
    # Archive before insert: the schema allows one active row per (org, slug), and
    # both statements sit in one transaction, so the page is never observably blank.
    conn.execute(
        """UPDATE oams.interfaces SET status = 'archived'
            WHERE org_unit_id = %s AND slug = %s AND status = 'active'""", (org, slug))
    row = conn.execute(
        """INSERT INTO oams.interfaces
               (org_unit_id, slug, name, status, visibility, schema_version, descriptor,
                position_id, version_no, based_on, created_by, updated_by)
           VALUES (%s, %s, %s, 'active', 'department', %s, %s, %s, %s, %s, %s, %s)
           RETURNING interface_id""",
        (org, slug, descriptor["name"], descriptor["schema_version"], Jsonb(descriptor),
         position, version, previous["interface_id"] if previous is not None else None,
         admin, admin)).fetchone()
print("interface enabled:", row["interface_id"], "slug", slug, "version", version,
      "schema", descriptor["schema_version"])
