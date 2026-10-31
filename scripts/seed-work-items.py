"""Create the design department's open work items.

The rollup predicate needs work_item_id IS NOT NULL, and oams.work_items is
empty, so no department entry could ever be linked. This seeds the items the
design department is actually working on (idempotent by title)."""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import psycopg

URL = os.environ["OAMS_DATABASE_URL"]
ITEMS = [
    ("AW24 秋冬针织外套打样", "project", "in_progress", None),
    ("AW24 卫衣面辅料确认", "routine", "blocked", "等面料到仓，供应商承诺 10-20"),
    ("SS25 早春系列企划", "project", "planned", None),
]
with psycopg.connect(URL, row_factory=psycopg.rows.dict_row) as conn:
    unit = conn.execute("SELECT org_unit_id FROM oams.org_units WHERE org_code='DESIGN'").fetchone()["org_unit_id"]
    admin = conn.execute("SELECT member_id FROM oams.workforce_members WHERE employee_code='ADMIN001'").fetchone()["member_id"]
    position = conn.execute("SELECT position_id FROM oams.positions WHERE position_code='DESIGN_LEAD'").fetchone()["position_id"]
    for title, kind, status, reason in ITEMS:
        exists = conn.execute("SELECT 1 FROM oams.work_items WHERE org_unit_id=%s AND title=%s", (unit, title)).fetchone()
        if exists:
            print("exists:", title)
            continue
        row = conn.execute(
            "INSERT INTO oams.work_items (org_unit_id, position_id, title, kind, status, blocked_reason, created_by)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING work_item_id, title",
            (unit, position, title, kind, status, reason, admin)).fetchone()
        print("created:", row["work_item_id"], row["title"])
