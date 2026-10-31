"""Rollup contract: department entries -> company numbers (task-8, part H).

The promise of plugin-host-concept ch.6 is that exactly three conditions decide
whether an entry reaches a company number:

    status_entry = 'active' AND entry_mode = 'live' AND work_item_id IS NOT NULL

These tests pin that promise down on real rows in a real PostgreSQL: they insert a
scratch company / department / position / page / entries inside one transaction and
roll everything back at the end, so the assertions are backed by data the database
itself wrote rather than by hand-built dictionaries.  They also pin the degradations:
a missing metric or a broken connection must answer "unavailable + why", because
these functions are called while a page renders and a 500 would hide the reason.
"""

from __future__ import annotations

import contextlib
import json

import pytest

from oams import api

pytestmark = pytest.mark.db


# ---------------------------------------------------------------------------
# A scratch world, inserted for real and rolled back
# ---------------------------------------------------------------------------

DESCRIPTOR = {
    "schema_version": "0.2",
    "interface_id": "rollup-fixture",
    "name": "汇总测试页",
    "org_unit_code": "ROLLUPD",
    "capabilities": ["entries:write", "entries:read:dept", "aggregate:read:dept"],
    "modules": [{"key": "layout", "type": "rows", "params": {"gap": "md"}, "children": []}],
    "fields": [
        {"key": "title", "label": "款号", "type": "text", "required": True,
         "map_to": "title", "aggregatable": True},
        {"key": "state_word", "label": "打样状态", "type": "enum", "required": True,
         "map_to": "status", "aggregatable": True,
         "options": ["待打样", "打样中", "等面料", "已完成"]},
        {"key": "sample_done", "label": "完成进度", "type": "number", "required": False,
         "map_to": "progress_value", "aggregatable": True},
        {"key": "evidence_ref", "label": "依据", "type": "text", "required": True,
         "map_to": "evidence", "aggregatable": True},
        {"key": "fabric_note", "label": "面料备注", "type": "text", "required": False,
         "map_to": "display_only", "aggregatable": False},
    ],
    "status_map": {"已完成": "done", "打样中": "in_progress", "等面料": "blocked",
                   "待打样": "planned"},
    "rules": {"evidence_required": True, "progress_range": [0, 100]},
    "metrics": [
        {"metric_key": "dept.ROLLUPD.finished", "mode": "emit", "agg": "count",
         "from_field": "state_word", "where_status": "done"},
        {"metric_key": "dept.ROLLUPD.progress_sum", "mode": "emit", "agg": "sum",
         "from_field": "sample_done"},
        {"metric_key": "dept.ROLLUPD.progress_avg", "mode": "emit", "agg": "avg",
         "from_field": "sample_done"},
        {"metric_key": "dept.ROLLUPD.statuses", "mode": "emit", "agg": "count_distinct",
         "from_field": "state_word"},
        {"metric_key": "dept.ROLLUPD.private", "mode": "emit", "agg": "count",
         "from_field": "fabric_note"},
        # A display binding shows a platform number: it must never be treated as
        # an aggregation of entries, so _descriptor_bindings has to skip it.
        {"metric_key": "funnel.sample_done", "mode": "display"},
    ],
}


def _insert_org(conn, code, name, unit_type, parent=None):
    return conn.execute(
        "INSERT INTO oams.org_units (org_code, name, unit_type, parent_org_unit_id) "
        "VALUES (%s, %s, %s, %s) RETURNING org_unit_id",
        (code, name, unit_type, parent)).fetchone()["org_unit_id"]


def _insert_position(conn, code, org_unit_id):
    return conn.execute(
        "INSERT INTO oams.positions (position_code, title, org_unit_id) "
        "VALUES (%s, %s, %s) RETURNING position_id",
        (code, code + " 负责人", org_unit_id)).fetchone()["position_id"]


def _insert_work_item(conn, org_unit_id, position_id, title, status="in_progress",
                      blocked_reason=None):
    return conn.execute(
        "INSERT INTO oams.work_items "
        "(org_unit_id, position_id, title, kind, status, blocked_reason) "
        "VALUES (%s, %s, %s, 'project', %s, %s) RETURNING work_item_id",
        (org_unit_id, position_id, title, status, blocked_reason)).fetchone()["work_item_id"]


def _insert_interface(conn, org_unit_id, member_id, position_id, status="active",
                      descriptor=None, slug="rollup-fixture"):
    return conn.execute(
        "INSERT INTO oams.interfaces "
        "(org_unit_id, slug, name, status, visibility, schema_version, descriptor, "
        " position_id, version_no, created_by, updated_by) "
        "VALUES (%s, %s, %s, %s, 'department', '0.2', %s::jsonb, %s, 1, %s, %s) "
        "RETURNING interface_id",
        (org_unit_id, slug, "汇总测试页", status, json.dumps(descriptor or DESCRIPTOR),
         position_id, member_id, member_id)).fetchone()["interface_id"]


def _insert_entry(conn, interface_id, member_id, note, status="done", progress=None,
                  work_item_id=None, entry_mode="live", status_entry="active",
                  title="AW24-001", blocked_reason=None):
    # The database itself refuses a blocked entry without a reason (migration 0005),
    # so the fixture supplies one rather than working around the CHECK.
    if status == "blocked" and not blocked_reason:
        blocked_reason = "等面料到仓"
    # A retracted row must say who withdrew it and when (0005 CHECK), otherwise the
    # database rejects the fixture before the rollup logic is ever reached.
    retracted = status_entry == "retracted"
    return conn.execute(
        "INSERT INTO oams.interface_entries "
        "(interface_id, work_item_id, title, status, blocked_reason, progress_value, note, "
        " evidence, reported_by, entry_mode, status_entry, retracted_by, retracted_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, 'SY-2410', %s, %s, %s, "
        "        CASE WHEN %s THEN %s END, CASE WHEN %s THEN now() END) "
        "RETURNING entry_id",
        (interface_id, work_item_id, title, status, blocked_reason, progress, note,
         member_id, entry_mode, status_entry,
         retracted, member_id, retracted)).fetchone()["entry_id"]


@contextlib.contextmanager
def scratch_world(db_ready):
    """Real rows for one test, all inside a transaction that is rolled back.

    Nothing is committed, so the developer database is left exactly as it was while
    the queries under test still see genuine PostgreSQL rows, defaults, CHECK
    constraints and the org-consistency trigger.
    """

    with db_ready.connect() as conn:
        conn.execute("SAVEPOINT rollup_fixture")
        try:
            member_id = conn.execute(
                "SELECT member_id FROM oams.workforce_members "
                "WHERE employee_code = 'ADMIN001'").fetchone()
            if member_id is None:
                pytest.skip("no ADMIN001 member in this database")
            member_id = member_id["member_id"]

            company = _insert_org(conn, "ROLLUPCO", "汇总测试公司", "company")
            dept = _insert_org(conn, "ROLLUPD", "汇总测试部门", "department", company)
            other = _insert_org(conn, "ROLLUPO", "另一部门", "department", company)
            empty_dept = _insert_org(conn, "ROLLUPE", "没有页面的部门", "department", company)

            position = _insert_position(conn, "ROLLUP_LEAD", dept)
            other_position = _insert_position(conn, "ROLLUP_OTHER", other)

            item_a = _insert_work_item(conn, dept, position, "工作项 A")
            item_b = _insert_work_item(conn, dept, position, "工作项 B")
            item_c = _insert_work_item(conn, dept, position, "工作项 C")
            item_done = _insert_work_item(conn, dept, position, "已完工的工作项",
                                          status="done")
            item_cancelled = _insert_work_item(conn, dept, position, "取消的工作项",
                                               status="cancelled")
            foreign_item = _insert_work_item(conn, other, other_position, "别的部门的工作项")

            page = _insert_interface(conn, dept, member_id, position)
            archived = _insert_interface(
                conn, dept, member_id, position, status="archived",
                slug="rollup-fixture-archived",
                descriptor=dict(DESCRIPTOR, metrics=[
                    {"metric_key": "dept.ROLLUPD.hidden", "mode": "emit", "agg": "count",
                     "from_field": "state_word"}]))

            # The one entry shape that may count: active, live, linked.
            rolled_done_40 = _insert_entry(conn, page, member_id, "rolled-done-40",
                                           status="done", progress=40, work_item_id=item_a)
            rolled_done_60 = _insert_entry(conn, page, member_id, "rolled-done-60",
                                           status="done", progress=60, work_item_id=item_b)
            rolled_blocked_100 = _insert_entry(conn, page, member_id, "rolled-blocked-100",
                                               status="blocked", progress=100,
                                               work_item_id=item_c)
            # Each of these breaks exactly one of the three conditions.
            # 97 is the loudest value the 0..100 CHECK allows: if any of these three
            # ever leaked into the sum the total would stop being a round 200.
            unlinked = _insert_entry(conn, page, member_id, "unlinked-done-97",
                                     status="done", progress=97, work_item_id=None)
            draft = _insert_entry(conn, page, member_id, "draft-done-97",
                                  status="done", progress=97, work_item_id=item_a,
                                  entry_mode="draft")
            retracted = _insert_entry(conn, page, member_id, "retracted-done-97",
                                      status="done", progress=97, work_item_id=item_a,
                                      status_entry="retracted")

            yield {
                "conn": conn, "member_id": member_id,
                "company": company, "dept": dept, "other": other,
                "empty_dept": empty_dept, "position": position,
                "item_a": item_a, "item_b": item_b, "item_c": item_c,
                "item_done": item_done, "item_cancelled": item_cancelled,
                "foreign_item": foreign_item,
                "page": page, "archived": archived,
                "entries": {
                    "rolled_done_40": rolled_done_40,
                    "rolled_done_60": rolled_done_60,
                    "rolled_blocked_100": rolled_blocked_100,
                    "unlinked": unlinked, "draft": draft, "retracted": retracted,
                },
            }
        finally:
            conn.execute("ROLLBACK TO SAVEPOINT rollup_fixture")


@pytest.fixture()
def world(bootstrapped_database):
    # A database of its own, empty apart from the bootstrapped administrator.
    # The company scope of build_department_aggregates means "every active
    # department", so running this against a developer database would also count
    # whatever pages happen to live there.  scratch_world still inserts real rows
    # and rolls them back, so the assertions stay backed by the database itself.
    with scratch_world(bootstrapped_database) as data:
        yield data


class _ExplodingConnection:
    """A connection whose every query blows up, to prove the callers degrade.

    Only the failure path uses this: the assertion is that the function answers
    "unavailable + reason" instead of raising into a page render.
    """

    def execute(self, *args, **kwargs):
        raise RuntimeError("connection exploded on purpose")


# ---------------------------------------------------------------------------
# The predicate: three conditions, each one disproved on its own
# ---------------------------------------------------------------------------


def test_rollup_predicate_names_exactly_the_three_conditions():
    assert "status_entry" in api.ROLLUP_PREDICATE
    assert "entry_mode" in api.ROLLUP_PREDICATE
    assert "work_item_id IS NOT NULL" in api.ROLLUP_PREDICATE
    assert api.ROLLUP_PREDICATE.count(" AND ") == 2, "exactly three conditions, joined"
    assert set(api.ROLLUP_STATES) == {"rolled", "unlinked", "draft", "retracted"}


def test_each_broken_condition_gets_its_own_state(world):
    states = {row["note"]: row["rollup_state"]
              for row in api.page_entries(world["conn"], world["page"])}
    assert states["rolled-done-40"] == "rolled"
    assert states["unlinked-done-97"] == "unlinked"
    assert states["draft-done-97"] == "draft"
    assert states["retracted-done-97"] == "retracted"


def test_retracted_entry_does_not_reach_the_sum(world):
    result = api.aggregate_entries(world["conn"], world["dept"],
                                   "dept.ROLLUPD.progress_sum")
    assert result["available"] is True
    assert result["entry_count"] == 3, "only the three linked live active entries count"
    assert result["value"] == 200, "40 + 60 + 100; the retracted 97 must not be added"
    assert 97 not in [entry["progress_value"] for entry in result["entries"]]


def test_draft_entry_does_not_reach_the_sum(world):
    result = api.aggregate_entries(world["conn"], world["dept"],
                                   "dept.ROLLUPD.progress_sum")
    assert result["value"] == 200
    assert 97 not in [entry["progress_value"] for entry in result["entries"]]


def test_unlinked_entry_does_not_reach_the_sum(world):
    result = api.aggregate_entries(world["conn"], world["dept"],
                                   "dept.ROLLUPD.progress_sum")
    assert result["value"] == 200
    assert len(result["entries"]) == 3
    assert all(entry["work_item_id"] is not None for entry in result["entries"])


def test_rollup_entries_lists_only_the_entries_that_counted(world):
    result = api.rollup_entries(world["conn"], world["page"], "dept.ROLLUPD.progress_sum")
    assert result["available"] is True
    assert result["mode"] == "emit"
    assert result["agg"] == "sum"
    assert result["from_field"] == "sample_done"
    assert result["entry_count"] == 3
    assert result["value"] == 200
    assert {entry["progress_value"] for entry in result["entries"]} == {40, 60, 100}
    for entry in result["entries"]:
        assert entry["entry_id"] is not None
        assert entry["reported_at"] is not None


def test_rollup_entries_without_metric_key_lists_the_counted_entries(world):
    result = api.rollup_entries(world["conn"], world["page"])
    assert result["available"] is True
    assert result["entry_count"] == 3
    assert all(entry["work_item_id"] for entry in result["entries"])


def test_rollup_entries_unknown_metric_is_an_answer_not_an_error(world):
    result = api.rollup_entries(world["conn"], world["page"], "dept.ROLLUPD.nope")
    assert result["available"] is False
    assert "dept.ROLLUPD.nope" in result["reason"]
    assert result["entries"] == []


def test_rollup_entries_missing_page_is_an_answer_not_an_error(world):
    result = api.rollup_entries(world["conn"], world["empty_dept"],
                                "dept.ROLLUPD.progress_sum")
    assert result["available"] is False
    assert result["reason"] == "找不到这个界面"


# ---------------------------------------------------------------------------
# The four aggregation modes
# ---------------------------------------------------------------------------


def test_count_aggregation_counts_rows(world):
    result = api.aggregate_entries(world["conn"], world["dept"], "dept.ROLLUPD.finished")
    assert result["agg"] == "count"
    assert result["where_status"] == "done"
    assert result["value"] == 2, "two entries are done; the blocked one is filtered out"
    assert result["entry_count"] == 2


def test_sum_aggregation_adds_the_values(world):
    result = api.aggregate_entries(world["conn"], world["dept"],
                                   "dept.ROLLUPD.progress_sum")
    assert result["agg"] == "sum"
    assert result["value"] == 200


def test_avg_aggregation_averages_the_values(world):
    result = api.aggregate_entries(world["conn"], world["dept"],
                                   "dept.ROLLUPD.progress_avg")
    assert result["agg"] == "avg"
    assert result["value"] == pytest.approx(200 / 3)


def test_count_distinct_aggregation_deduplicates(world):
    result = api.aggregate_entries(world["conn"], world["dept"], "dept.ROLLUPD.statuses")
    assert result["agg"] == "count_distinct"
    assert result["value"] == 2, "the counted entries are done, done, blocked"


def test_where_status_really_filters(world):
    counting_everything = api.rollup_entries(world["conn"], world["page"],
                                             "dept.ROLLUPD.statuses")
    only_done = api.rollup_entries(world["conn"], world["page"], "dept.ROLLUPD.finished")
    assert counting_everything["entry_count"] == 3
    assert only_done["entry_count"] == 2
    assert {entry["status"] for entry in only_done["entries"]} == {"done"}


def test_field_that_is_only_visible_in_the_department_is_not_aggregated(world):
    result = api.aggregate_entries(world["conn"], world["dept"], "dept.ROLLUPD.private")
    assert result["available"] is True
    assert result["value"] is None
    assert "不参与汇总" in result["reason"]


# ---------------------------------------------------------------------------
# descriptor helpers
# ---------------------------------------------------------------------------


def test_descriptor_bindings_keeps_emit_and_drops_display():
    bindings = api._descriptor_bindings(DESCRIPTOR)
    keys = [binding["metric_key"] for binding in bindings]
    assert "dept.ROLLUPD.finished" in keys
    assert "funnel.sample_done" not in keys
    assert all(binding["mode"] == "emit" for binding in bindings)


def test_descriptor_bindings_is_defensive_about_junk():
    assert api._descriptor_bindings(None) == []
    assert api._descriptor_bindings({}) == []
    assert api._descriptor_bindings({"metrics": "not a list"}) == []
    assert api._descriptor_bindings({"metrics": [None, 3, {"mode": "emit"}]}) == []


def test_field_map_to_returns_the_standard_column():
    assert api._field_map_to(DESCRIPTOR, "title") == "title"
    assert api._field_map_to(DESCRIPTOR, "state_word") == "status"
    assert api._field_map_to(DESCRIPTOR, "sample_done") == "progress_value"
    assert api._field_map_to(DESCRIPTOR, "evidence_ref") == "evidence"


def test_field_map_to_is_none_for_display_only_unknown_and_absent_fields():
    assert api._field_map_to(DESCRIPTOR, "fabric_note") is None, "display_only column"
    assert api._field_map_to(DESCRIPTOR, "no_such_field") is None
    assert api._field_map_to(DESCRIPTOR, None) is None
    assert api._field_map_to(None, "title") is None
    assert api._field_map_to({"fields": [{"key": "weird", "map_to": "secret"}]},
                             "weird") is None, "map_to outside ROLLUP_COLUMNS"


def test_field_label_falls_back_to_none():
    assert api._field_label(DESCRIPTOR, "sample_done") == "完成进度"
    assert api._field_label(DESCRIPTOR, "nope") is None
    assert api._field_label(None, "title") is None


# ---------------------------------------------------------------------------
# Degradation: a broken lookup must never become a 500
# ---------------------------------------------------------------------------


def test_aggregate_entries_without_a_declaring_page_is_unavailable(world):
    result = api.aggregate_entries(world["conn"], world["dept"],
                                   "dept.ROLLUPD.never_declared")
    assert result["available"] is False
    assert "dept.ROLLUPD.never_declared" in result["reason"]
    assert result["value"] is None
    assert result["entries"] == []


def test_aggregate_entries_ignores_archived_pages(world):
    result = api.aggregate_entries(world["conn"], world["dept"], "dept.ROLLUPD.hidden")
    assert result["available"] is False, "an archived page may not feed a company number"
    assert "dept.ROLLUPD.hidden" in result["reason"]


def test_aggregate_entries_never_raises_on_a_broken_connection():
    result = api.aggregate_entries(_ExplodingConnection(), None, "dept.ROLLUPD.finished")
    assert result["available"] is False
    assert "connection exploded on purpose" in result["reason"]


def test_rollup_entries_never_raises_on_a_broken_connection():
    result = api.rollup_entries(_ExplodingConnection(), None, "dept.ROLLUPD.finished")
    assert result["available"] is False
    assert "connection exploded on purpose" in result["reason"]


def test_build_department_aggregates_never_raises_on_a_broken_connection():
    panel = api.build_department_aggregates(_ExplodingConnection(), None)
    assert panel["available"] is False
    assert "connection exploded on purpose" in panel["reason"]
    assert panel["metrics"] == []


# ---------------------------------------------------------------------------
# The department panel
# ---------------------------------------------------------------------------


def test_build_department_aggregates_reports_real_values(world):
    panel = api.build_department_aggregates(world["conn"], world["dept"])
    assert panel["available"] is True
    values = {metric["metric_key"]: metric["value"] for metric in panel["metrics"]}
    assert values["dept.ROLLUPD.finished"] == 2
    assert values["dept.ROLLUPD.progress_sum"] == 200
    assert values["dept.ROLLUPD.progress_avg"] == pytest.approx(200 / 3)
    assert values["dept.ROLLUPD.statuses"] == 2
    assert values["dept.ROLLUPD.private"] is None
    # render_interface wants exactly this shape for metric_card / metric_grid.
    assert panel["aggregates"]["dept.ROLLUPD.progress_sum"] == 200
    assert "dept.ROLLUPD.private" not in panel["aggregates"]
    finished = next(metric for metric in panel["metrics"]
                    if metric["metric_key"] == "dept.ROLLUPD.finished")
    assert finished["formatted"] == "2"
    assert finished["label"] == "打样状态"


def test_build_department_aggregates_counts_the_three_states(world):
    panel = api.build_department_aggregates(world["conn"], world["dept"])
    assert panel["counts"]["rolled"] == 3
    assert panel["counts"]["unlinked"] == 1
    assert panel["counts"]["draft"] == 1
    assert panel["counts"]["retracted"] == 1
    assert panel["total"] == 6
    assert [page["interface_id"] for page in panel["interfaces"]] == [world["page"]]


def test_build_department_aggregates_for_a_department_without_pages(world):
    panel = api.build_department_aggregates(world["conn"], world["empty_dept"])
    assert panel["available"] is True
    assert panel["metrics"] == []
    assert panel["aggregates"] == {}
    assert panel["total"] == 0


def test_build_department_aggregates_for_a_company_picks_up_departments(world):
    panel = api.build_department_aggregates(world["conn"], world["company"])
    assert panel["available"] is True
    values = {metric["metric_key"]: metric["value"] for metric in panel["metrics"]}
    assert values["dept.ROLLUPD.progress_sum"] == 200, "the department page is picked up"
    assert values["dept.ROLLUPD.finished"] == 2
    assert panel["counts"]["rolled"] == 3


def test_entry_state_counts_matches_the_rows(world):
    counts = api.entry_state_counts(world["conn"], world["page"])
    assert counts == {"rolled": 3, "unlinked": 1, "draft": 1, "retracted": 1}


# ---------------------------------------------------------------------------
# The work items an entry may be linked to
# ---------------------------------------------------------------------------


def test_list_open_work_items_stays_inside_the_department(world):
    rows = api.list_open_work_items(world["conn"], org_unit_id=world["dept"])
    titles = {row["title"] for row in rows}
    assert "工作项 A" in titles
    assert "别的部门的工作项" not in titles


def test_list_open_work_items_company_scope_sees_every_department(world):
    rows = api.list_open_work_items(world["conn"], company_scope=True)
    titles = {row["title"] for row in rows}
    assert "工作项 A" in titles
    assert "别的部门的工作项" in titles


def test_list_open_work_items_without_a_scope_returns_nothing(world):
    assert api.list_open_work_items(world["conn"], org_unit_id=None,
                                    company_scope=False) == []


def test_list_open_work_items_excludes_finished_and_cancelled(world):
    rows = api.list_open_work_items(world["conn"], org_unit_id=world["dept"], limit=500)
    titles = {row["title"] for row in rows}
    assert "已完工的工作项" not in titles
    assert "取消的工作项" not in titles
    assert all(row["status"] in ("planned", "in_progress", "blocked") for row in rows)


def test_list_open_work_items_clamps_the_limit(world):
    everything = api.list_open_work_items(world["conn"], org_unit_id=world["dept"],
                                          limit=500)
    assert len(everything) >= 3
    assert len(api.list_open_work_items(world["conn"], org_unit_id=world["dept"],
                                        limit=0)) == 1, "0 is clamped up to 1"
    assert len(api.list_open_work_items(world["conn"], org_unit_id=world["dept"],
                                        limit=9999)) == len(everything)


def test_list_open_work_items_carries_what_the_link_form_shows(world):
    rows = api.list_open_work_items(world["conn"], org_unit_id=world["dept"])
    assert rows, "the fixture inserted open work items"
    for row in rows:
        assert row["work_item_id"] is not None
        assert row["title"]
        assert row["org_unit_name"] == "汇总测试部门"


def test_an_entry_can_be_linked_and_then_it_counts(world):
    """The whole point, end to end on real rows: linking changes the number."""

    before = api.aggregate_entries(world["conn"], world["dept"],
                                   "dept.ROLLUPD.progress_sum")
    assert before["value"] == 200

    world["conn"].execute(
        "UPDATE oams.interface_entries SET work_item_id = %s WHERE entry_id = %s",
        (world["item_c"], world["entries"]["unlinked"]))

    after = api.aggregate_entries(world["conn"], world["dept"],
                                  "dept.ROLLUPD.progress_sum")
    assert after["value"] == 200 + 97, "the newly linked entry now feeds the sum"
    assert after["entry_count"] == 4

    states = {row["note"]: row["rollup_state"]
              for row in api.page_entries(world["conn"], world["page"])}
    assert states["unlinked-done-97"] == "rolled"

    world["conn"].execute(
        "UPDATE oams.interface_entries SET work_item_id = NULL WHERE entry_id = %s",
        (world["entries"]["unlinked"],))
    back = api.aggregate_entries(world["conn"], world["dept"], "dept.ROLLUPD.progress_sum")
    assert back["value"] == 200, "unlinking removes it again"


# ---------------------------------------------------------------------------
# Two active pages, one metric key: the arithmetic has to compose
# ---------------------------------------------------------------------------


def test_two_pages_declaring_one_metric_are_pooled_not_first_wins(world):
    """The schema allows it (the unique index is on slug, not metric_key)."""

    second = _insert_interface(world["conn"], world["dept"], world["member_id"],
                               world["position"], slug="rollup-fixture-two")
    _insert_entry(world["conn"], second, world["member_id"], "second-done-30",
                  status="done", progress=30, work_item_id=world["item_a"])
    _insert_entry(world["conn"], second, world["member_id"], "second-wip-50",
                  status="in_progress", progress=50, work_item_id=world["item_b"])

    conn = world["conn"]

    # sum: both pages contribute (200 was page one alone).
    total = api.aggregate_entries(conn, world["dept"], "dept.ROLLUPD.progress_sum")
    assert total["value"] == 200 + 80
    assert total["entry_count"] == 5

    # count: five rows, not two.
    counted = api.aggregate_entries(conn, world["dept"], "dept.ROLLUPD.finished")
    assert counted["value"] == 3, "two done rows on page one plus one on page two"


def test_average_is_weighted_by_rows_not_by_pages(world):
    """Page means are not averagable: a one-row page must not outvote a big one."""

    second = _insert_interface(world["conn"], world["dept"], world["member_id"],
                               world["position"], slug="rollup-fixture-two")
    # Page one pools 40 / 60 / 100 (mean 66.67 over 3 rows); page two adds 30 / 50
    # (mean 40 over 2 rows). The pooled mean is 280 / 5 = 56.
    _insert_entry(world["conn"], second, world["member_id"], "second-done-30",
                  status="done", progress=30, work_item_id=world["item_a"])
    _insert_entry(world["conn"], second, world["member_id"], "second-wip-50",
                  status="in_progress", progress=50, work_item_id=world["item_b"])

    average = api.aggregate_entries(world["conn"], world["dept"], "dept.ROLLUPD.progress_avg")
    assert average["value"] == pytest.approx(56.0), (
        "mean of the page means would be 53.33 -- that is the bug this pins down")


def test_count_distinct_takes_the_union_across_pages(world):
    """A status reported on two pages is still one status."""

    second = _insert_interface(world["conn"], world["dept"], world["member_id"],
                               world["position"], slug="rollup-fixture-two")
    # Page one contributes done / done / blocked; page two contributes done and
    # in_progress. Union = 3. Adding the per-page distinct counts would give 4.
    _insert_entry(world["conn"], second, world["member_id"], "second-done-30",
                  status="done", progress=30, work_item_id=world["item_a"])
    _insert_entry(world["conn"], second, world["member_id"], "second-wip-50",
                  status="in_progress", progress=50, work_item_id=world["item_b"])

    distinct = api.aggregate_entries(world["conn"], world["dept"], "dept.ROLLUPD.statuses")
    assert distinct["value"] == 3
    assert {page["entry_count"] for page in distinct["pages"]} == {3, 2}


def test_archived_page_still_does_not_count_even_beside_a_live_one(world):
    """Two pages is the fix; the archived fixture page must stay excluded."""

    second = _insert_interface(world["conn"], world["dept"], world["member_id"],
                               world["position"], slug="rollup-fixture-two")
    _insert_entry(world["conn"], second, world["member_id"], "second-done-30",
                  status="done", progress=30, work_item_id=world["item_a"])

    total = api.aggregate_entries(world["conn"], world["dept"], "dept.ROLLUPD.progress_sum")
    assert total["value"] == 230
    assert {page["interface_id"] for page in total["pages"]} == {world["page"], second}


def test_work_item_limit_survives_junk_instead_of_raising(world):
    """web.py passes no limit today; a string or None must not become a 500."""

    conn = world["conn"]
    assert api.list_open_work_items(conn, world["dept"], limit=None)
    assert api.list_open_work_items(conn, world["dept"], limit="abc")
    assert len(api.list_open_work_items(conn, world["dept"], limit=0)) == 1
    assert api.list_open_work_items(conn, world["dept"], limit=9999)

