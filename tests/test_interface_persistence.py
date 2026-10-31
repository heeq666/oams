"""Interface persistence: save a validated descriptor, fill it in, read it back.

Runs against an isolated database. It proves the three decisions the base had to
make on its own: descriptors live in the database (auditable, versioned,
scannable), departments edit their own (public templates need review), and a
filled screen writes standard fields into the aggregate while keeping unmapped
department fields beside them without aggregating them.
"""
import json
import os
import sys
from pathlib import Path


import pytest  # noqa: E402  (test-module wrapper)


pytestmark = pytest.mark.db  # needs a live PostgreSQL and a served app


def _script() -> None:
    """Body of the original standalone e2e script, unchanged."""

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
    import psycopg
    from psycopg.types.json import Jsonb
    from oams.descriptor import validate_descriptor
    from oams.render import render_interface, render_read_only
    from oams.upgrade import upgrade_descriptor

    failures = []

    def check(name, condition, detail=""):
        if condition:
            print("PASS", name)
        else:
            failures.append(name)
            print("FAIL", name, detail)

    descriptor = {
        "schema_version": "0.1",
        "interface_id": "design-sampling",
        "name": "设计部打样进度",
        "org_unit_code": "DESIGN",
        "position_codes": ["DESIGN_LEAD"],
        "modules": [
            {"key": "item", "type": "item_card", "title": "事项", "order": 1,
             "source_fields": ["title"], "aggregatable": True},
            {"key": "progress", "type": "progress_bar", "title": "打样进度", "order": 2,
             "source_fields": ["sample_done"], "aggregatable": True},
            {"key": "evidence", "type": "evidence_input", "title": "依据", "order": 3,
             "source_fields": ["evidence_ref"], "aggregatable": True},
            {"key": "state", "type": "metric_card", "title": "状态", "order": 4,
             "source_fields": ["state_word"], "aggregatable": True},
            {"key": "private", "type": "list_block", "title": "面料备注", "order": 5,
             "source_fields": ["fabric_note"], "aggregatable": False},
        ],
        "fields": [
            {"key": "title", "label": "款号", "type": "text", "required": True,
             "map_to": "title", "aggregatable": True, "example": "AW24-001"},
            {"key": "sample_done", "label": "完成打样数", "type": "number", "required": True,
             "map_to": "progress_value", "aggregatable": True, "example": "12"},
            {"key": "evidence_ref", "label": "打样单号", "type": "text", "required": True,
             "map_to": "evidence", "aggregatable": True, "example": "SY-2410"},
            {"key": "state_word", "label": "打样状态", "type": "enum", "required": True,
             "map_to": "status", "aggregatable": True, "example": "打样中",
             "options": ["待打样", "打样中", "等面料", "已完成"]},
            {"key": "fabric_note", "label": "面料备注", "type": "text", "required": False,
             "map_to": "display_only", "aggregatable": False, "example": "待补货"},
        ],
        "status_map": {"待打样": "planned", "打样中": "in_progress", "等面料": "blocked", "已完成": "done"},
        "rules": {"evidence_required": True, "progress_range": [0, 100]},
        "meta": {"author_position_code": "DESIGN_LEAD", "version": 1,
                 "visibility": "department", "review": {"status": "draft"}},
    }

    # The stored document uses the current schema: a 0.1 descriptor written by an
    # older deployment goes through the same upgrade path the CLI uses, so this
    # script doubles as a regression test for the upgrader itself.
    descriptor, hints = upgrade_descriptor(descriptor)
    check("legacy descriptor upgrades to the current schema",
          descriptor["schema_version"] == "0.2", hints)
    check("descriptor passes validation", validate_descriptor(descriptor) == [],
          validate_descriptor(descriptor))

    def ensure_unit(conn, code, name):
        row = conn.execute("SELECT org_unit_id FROM oams.org_units WHERE org_code = %s", (code,)).fetchone()
        if row:
            return row[0]
        return conn.execute(
            "INSERT INTO oams.org_units (org_code, name, unit_type) VALUES (%s, %s, 'department') RETURNING org_unit_id",
            (code, name)).fetchone()[0]

    def ensure_member(conn, code, name):
        row = conn.execute("SELECT member_id FROM oams.workforce_members WHERE employee_code = %s", (code,)).fetchone()
        if row:
            return row[0]
        return conn.execute(
            "INSERT INTO oams.workforce_members (employee_code, display_name, employment_status) VALUES (%s, %s, 'active') RETURNING member_id",
            (code, name)).fetchone()[0]

    def ensure_position(conn, code, title, org):
        row = conn.execute("SELECT position_id FROM oams.positions WHERE position_code = %s", (code,)).fetchone()
        if row:
            return row[0]
        return conn.execute(
            "INSERT INTO oams.positions (position_code, title, org_unit_id) VALUES (%s, %s, %s) RETURNING position_id",
            (code, title, org)).fetchone()[0]

    with psycopg.connect(os.environ["OAMS_DATABASE_URL"]) as conn:
        org = ensure_unit(conn, "DESIGN", "设计部")
        member = ensure_member(conn, "IFACE001", "设计负责人")
        position = ensure_position(conn, "DESIGN_LEAD", "设计负责人", org)
        # A rerun should add a new version rather than trip over the previous one.
        conn.execute("UPDATE oams.interfaces SET status='archived' WHERE slug='design-sampling' AND status='active'")
        next_version = conn.execute(
            """SELECT COALESCE(max(version_no), 0) + 1 FROM oams.interfaces
                WHERE org_unit_id = %s AND slug = 'design-sampling'""", (org,)).fetchone()[0]

        iface = conn.execute(
            """INSERT INTO oams.interfaces
                   (org_unit_id, slug, name, status, visibility, schema_version, descriptor, position_id, created_by, version_no)
               VALUES (%s,'design-sampling','设计部打样进度','active','department','0.2',%s,%s,%s,%s)
               RETURNING interface_id, version_no""",
            (org, Jsonb(descriptor), position, member, next_version)).fetchone()
    check("interface saved", iface[0] is not None)
    check("interface version recorded", iface[1] >= 1)

    with psycopg.connect(os.environ["OAMS_DATABASE_URL"]) as conn:
        # Only one live screen per department slug: a department can draft freely but
        # cannot have two drawings claiming to be the current one.
        try:
            conn.execute(
                """INSERT INTO oams.interfaces
                       (org_unit_id, slug, name, status, visibility, schema_version, descriptor, version_no)
                   VALUES (%s,'design-sampling','第二版','active','department','0.2',%s,2)""",
                (org, Jsonb(descriptor)))
            conn.commit()
            check("second live draft rejected", False, "two active interfaces accepted")
        except psycopg.errors.UniqueViolation:
            conn.rollback()
            check("second live draft rejected", True)
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            check("second live draft rejected", False, repr(exc))

        # A public template without a reviewer is refused by the database, not just by
        # the application: the constraint survives a direct SQL write.
        try:
            conn.execute(
                """INSERT INTO oams.interfaces
                       (org_unit_id, slug, name, status, visibility, schema_version, descriptor, version_no)
                   VALUES (%s,'public-no-review','公共未审','draft','public','0.2',%s,1)""",
                (org, Jsonb(descriptor)))
            conn.commit()
            check("public template without review rejected", False, "accepted without reviewer")
        except psycopg.errors.CheckViolation:
            conn.rollback()
            check("public template without review rejected", True)
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            check("public template without review rejected", False, repr(exc))

    with psycopg.connect(os.environ["OAMS_DATABASE_URL"]) as conn:
        entry = conn.execute(
            """INSERT INTO oams.interface_entries
                   (interface_id, title, status, blocked_reason, needs_decision, progress_value,
                    note, evidence, extra, reported_by, submitted_via)
               VALUES (%s,'AW24-001','blocked','等面料到仓，供应商承诺 10-20',true,40,
                    '已完成初版打样','样衣确认单 SY-2410',%s,%s,'human')
               RETURNING entry_id, status, progress_value""",
            (iface[0], Jsonb({"fabric_note": "待补货"}), member)).fetchone()
    check("entry saved with standard fields", entry[1] == "blocked" and entry[2] == 40)

    with psycopg.connect(os.environ["OAMS_DATABASE_URL"]) as conn:
        row = conn.execute("SELECT title, status, evidence, extra, submitted_via FROM oams.interface_entries WHERE entry_id=%s", (entry[0],)).fetchone()
    check("standard field readable for aggregate", row[0] == "AW24-001" and row[1] == "blocked")
    check("unmapped field kept beside it", row[3].get("fabric_note") == "待补货")

    # Blocked without a reason is rejected: the whole point of the aggregate is
    # that a manager can read why something is stuck.
    with psycopg.connect(os.environ["OAMS_DATABASE_URL"]) as conn:
        try:
            conn.execute(
                """INSERT INTO oams.interface_entries
                       (interface_id, title, status, evidence, reported_by)
                   VALUES (%s,'AW24-002','blocked','单号 SY-2',%s)""", (iface[0], member))
            conn.commit()
            check("blocked without reason rejected", False, "accepted")
        except psycopg.errors.CheckViolation:
            conn.rollback()
            check("blocked without reason rejected", True)
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            check("blocked without reason rejected", False, repr(exc))

    with psycopg.connect(os.environ["OAMS_DATABASE_URL"]) as conn:
        stored = conn.execute("SELECT descriptor FROM oams.interfaces WHERE interface_id=%s", (iface[0],)).fetchone()[0]
    html = render_interface(stored, values={"title": "AW24-001", "sample_done": "40"})
    check("saved descriptor renders", "设计部打样进度" in html and "AW24-001" in html)
    readonly = render_read_only(stored, values={"title": "AW24-001", "fabric_note": "待补货"})
    check("manager view hides private field", "AW24-001" in readonly and "待补货" not in readonly)

    with psycopg.connect(os.environ["OAMS_DATABASE_URL"]) as conn:
        used = conn.execute(
            """SELECT count(DISTINCT i.org_unit_id) FROM oams.interface_entries e
                 JOIN oams.interfaces i USING (interface_id)
                WHERE e.extra ? 'fabric_note'""").fetchone()[0]
    check("evolution scan can count departments using a key", used >= 1)

    if failures:
        raise AssertionError("failed checks: " + ", ".join(failures))
    print("")
    print("all interface persistence checks passed")


def test_interface_persistence(site) -> None:
    """Run the original standalone script as a pytest test."""

    _script()
