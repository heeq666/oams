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
        # display 引本页自己 emit 的键（契约 §2 自产自用）：它仍然必须被
        # _descriptor_bindings 跳过，不能当成一条能聚合的绑定。
        {"metric_key": "dept.ROLLUPD.progress_sum", "mode": "display"},
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
                  title="AW24-001", blocked_reason=None, extra=None):
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
        " evidence, reported_by, entry_mode, status_entry, retracted_by, retracted_at, extra) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, 'SY-2410', %s, %s, %s, "
        "        CASE WHEN %s THEN %s END, CASE WHEN %s THEN now() END, %s::jsonb) "
        "RETURNING entry_id",
        (interface_id, work_item_id, title, status, blocked_reason, progress, note,
         member_id, entry_mode, status_entry,
         retracted, member_id, retracted,
         json.dumps(extra) if extra is not None else "{}")).fetchone()["entry_id"]


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
    """页面上那一列说的是「这一行在本部门算不算」，不是「进不进公司数」。

    部门一面只问两件事：这行是真的吗（不是草稿、没被撤回）。工作项是公司汇总
    才需要的门槛，所以没关联的行在部门这里照样算数，只是进不了公司数字。
    """
    rows = {row["note"]: row for row in api.page_entries(world["conn"], world["page"])}
    assert rows["rolled-done-40"]["rollup_state"] == "counted"
    assert rows["unlinked-done-97"]["rollup_state"] == "counted_unlinked"
    assert rows["draft-done-97"]["rollup_state"] == "draft"
    assert rows["retracted-done-97"]["rollup_state"] == "retracted"
    # 同一个行同时告诉你它进不进公司数：两种「算数」在这里分开
    assert rows["rolled-done-40"]["feeds_company"] is True
    assert rows["unlinked-done-97"]["feeds_company"] is False
    assert rows["draft-done-97"]["feeds_company"] is False
    assert rows["retracted-done-97"]["feeds_company"] is False


def test_retracted_entry_does_not_reach_the_sum(world):
    result = api.aggregate_entries(world["conn"], world["dept"],
                                   "dept.ROLLUPD.progress_sum")
    assert result["available"] is True
    # 部门数 = 40 + 60 + 100 + 97（未关联那条），撤回的 97 不加。
    assert result["entry_count"] == 4
    assert result["value"] == 297
    # 97 这个值在三条异常行里重复，所以按 entry_id 判，不按值判。
    counted = {entry["entry_id"] for entry in result["entries"]}
    assert world["entries"]["retracted"] not in counted
    assert world["entries"]["unlinked"] in counted, "未关联的算部门数"


def test_draft_entry_does_not_reach_the_sum(world):
    result = api.aggregate_entries(world["conn"], world["dept"],
                                   "dept.ROLLUPD.progress_sum")
    assert result["value"] == 297
    assert world["entries"]["draft"] not in {entry["entry_id"] for entry in result["entries"]}


def test_unlinked_entry_counts_here_but_not_in_the_company_number(world):
    """同一行、同一刻：部门页算它，公司数不算它。

    一条报销不需要先挂项目才叫真实发生。所以部门那一面只问「是真的吗」，
    公司那一面还要问「挂上工作项了吗」——「未关联」说的是不进公司数，
    从来没说过「这行不算数」。
    """
    department = api.aggregate_entries(world["conn"], world["dept"],
                                       "dept.ROLLUPD.progress_sum")
    assert department["value"] == 297, "40 + 60 + 100 + 97：未关联那条在部门数里"
    assert len(department["entries"]) == 4
    assert world["entries"]["unlinked"] in {e["entry_id"] for e in department["entries"]}

    company = api.rollup_entries(world["conn"], world["page"], "dept.ROLLUPD.progress_sum")
    assert company["value"] == 200, "公司数仍要工作项门槛"
    assert len(company["entries"]) == 3
    assert all(entry["work_item_id"] is not None for entry in company["entries"])


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
    assert result["value"] == 3, "三条 done（含未关联那条），blocked 被过滤掉"
    assert result["entry_count"] == 3


def test_sum_aggregation_adds_the_values(world):
    result = api.aggregate_entries(world["conn"], world["dept"],
                                   "dept.ROLLUPD.progress_sum")
    assert result["agg"] == "sum"
    assert result["value"] == 297, "40 + 60 + 100 + 97"


def test_avg_aggregation_averages_the_values(world):
    result = api.aggregate_entries(world["conn"], world["dept"],
                                   "dept.ROLLUPD.progress_avg")
    assert result["agg"] == "avg"
    assert result["value"] == pytest.approx(297 / 4)


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
    assert values["dept.ROLLUPD.finished"] == 3
    assert values["dept.ROLLUPD.progress_sum"] == 297
    assert values["dept.ROLLUPD.progress_avg"] == pytest.approx(297 / 4)
    assert values["dept.ROLLUPD.statuses"] == 2, "done / blocked 两种状态"
    assert values["dept.ROLLUPD.private"] is None
    # render_interface wants exactly this shape for metric_card / metric_grid.
    assert panel["aggregates"]["dept.ROLLUPD.progress_sum"] == 297
    assert "dept.ROLLUPD.private" not in panel["aggregates"]
    finished = next(metric for metric in panel["metrics"]
                    if metric["metric_key"] == "dept.ROLLUPD.finished")
    assert finished["formatted"] == "3"
    assert finished["label"] == "打样状态"


def test_build_department_aggregates_counts_the_four_states(world):
    panel = api.build_department_aggregates(world["conn"], world["dept"])
    assert panel["counts"]["counted"] == 3
    assert panel["counts"]["counted_unlinked"] == 1
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
    assert values["dept.ROLLUPD.progress_sum"] == 297, "the department page is picked up"
    assert values["dept.ROLLUPD.finished"] == 3
    assert panel["counts"]["counted"] == 3


def test_entry_state_counts_matches_the_rows(world):
    counts = api.entry_state_counts(world["conn"], world["page"])
    assert counts == {"counted": 3, "counted_unlinked": 1, "draft": 1, "retracted": 1}


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


def test_linking_an_entry_moves_it_into_the_company_number_not_the_department_one(world):
    """关联只改变「进不进公司数」，部门自己的数从头到尾没动过。

    这是两套口径最容易被写错的地方：以前关联前后部门数会从 200 跳到 297，
    让「关联」看起来像是让数字变真的动作；其实它只决定了这行能不能进公司汇总。
    """

    def department():
        return api.aggregate_entries(world["conn"], world["dept"], "dept.ROLLUPD.progress_sum")

    def company():
        return api.rollup_entries(world["conn"], world["page"], "dept.ROLLUPD.progress_sum")

    assert department()["value"] == 297
    assert company()["value"] == 200

    world["conn"].execute(
        "UPDATE oams.interface_entries SET work_item_id = %s WHERE entry_id = %s",
        (world["item_c"], world["entries"]["unlinked"]))

    assert department()["value"] == 297, "部门数不因关联而变"
    assert department()["entry_count"] == 4
    assert company()["value"] == 297, "关联后才进公司数"
    assert company()["entry_count"] == 4

    rows = {row["note"]: row for row in api.page_entries(world["conn"], world["page"])}
    assert rows["unlinked-done-97"]["rollup_state"] == "counted"
    assert rows["unlinked-done-97"]["feeds_company"] is True

    world["conn"].execute(
        "UPDATE oams.interface_entries SET work_item_id = NULL WHERE entry_id = %s",
        (world["entries"]["unlinked"],))
    assert department()["value"] == 297, "取消关联也不影响部门数"
    assert company()["value"] == 200, "撤出公司数"


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

    # sum: both pages contribute (297 was page one alone, 4 rows).
    total = api.aggregate_entries(conn, world["dept"], "dept.ROLLUPD.progress_sum")
    assert total["value"] == 297 + 80
    assert total["entry_count"] == 6

    # count: 部门数按行数，不按页数。
    counted = api.aggregate_entries(conn, world["dept"], "dept.ROLLUPD.finished")
    assert counted["value"] == 4, "页一三条 done（含未关联），页二一条 done"


def test_average_is_weighted_by_rows_not_by_pages(world):
    """Page means are not averagable: a one-row page must not outvote a big one."""

    second = _insert_interface(world["conn"], world["dept"], world["member_id"],
                               world["position"], slug="rollup-fixture-two")
    # 页一 40 / 60 / 100 / 97（4 行，部门口径）；页二 30 / 50（2 行）。
    # 合并均值 377 / 6 = 62.83，不是两页均值的平均。
    _insert_entry(world["conn"], second, world["member_id"], "second-done-30",
                  status="done", progress=30, work_item_id=world["item_a"])
    _insert_entry(world["conn"], second, world["member_id"], "second-wip-50",
                  status="in_progress", progress=50, work_item_id=world["item_b"])

    average = api.aggregate_entries(world["conn"], world["dept"], "dept.ROLLUPD.progress_avg")
    assert average["value"] == pytest.approx(377 / 6), (
        "mean of the page means would be (74.25 + 40) / 2 = 57.13 -- 权重要按行算")


def test_count_distinct_takes_the_union_across_pages(world):
    """A status reported on two pages is still one status."""

    second = _insert_interface(world["conn"], world["dept"], world["member_id"],
                               world["position"], slug="rollup-fixture-two")
    # 页一 done / done / blocked / done，页二 done / in_progress。
    # 并集 = 3 种；把两页的 distinct 相加会得到 4。
    _insert_entry(world["conn"], second, world["member_id"], "second-done-30",
                  status="done", progress=30, work_item_id=world["item_a"])
    _insert_entry(world["conn"], second, world["member_id"], "second-wip-50",
                  status="in_progress", progress=50, work_item_id=world["item_b"])

    distinct = api.aggregate_entries(world["conn"], world["dept"], "dept.ROLLUPD.statuses")
    assert distinct["value"] == 3
    assert {page["entry_count"] for page in distinct["pages"]} == {4, 2}


def test_archived_page_still_does_not_count_even_beside_a_live_one(world):
    """Two pages is the fix; the archived fixture page must stay excluded."""

    second = _insert_interface(world["conn"], world["dept"], world["member_id"],
                               world["position"], slug="rollup-fixture-two")
    _insert_entry(world["conn"], second, world["member_id"], "second-done-30",
                  status="done", progress=30, work_item_id=world["item_a"])

    total = api.aggregate_entries(world["conn"], world["dept"], "dept.ROLLUPD.progress_sum")
    # 新加的这条是 active 页，它的 30 要计入；夹具里那个 archived 页不在这份名单里。
    assert total["value"] == 297 + 30
    assert {page["interface_id"] for page in total["pages"]} == {world["page"], second}, (
        "归档页不在聚合名单里——它声明的 dept.ROLLUPD.hidden 也不该被算进这个键")


# ---------------------------------------------------------------------------
# 没有独立列的字段：金额落在 extra 里，仍然要算得出
# ---------------------------------------------------------------------------

MONEY_DESCRIPTOR = dict(
    DESCRIPTOR,
    fields=list(DESCRIPTOR["fields"]) + [
        {"key": "money", "label": "报销金额", "type": "number", "required": False,
         "map_to": "amount", "aggregatable": True},
    ],
    metrics=[
        {"metric_key": "dept.ROLLUPD.money_total", "mode": "emit", "agg": "sum",
         "from_field": "money"},
        {"metric_key": "dept.ROLLUPD.money_avg", "mode": "emit", "agg": "avg",
         "from_field": "money"},
    ],
)


def test_a_field_without_its_own_column_still_sums_from_extra(world):
    """金额没有独立列，落进 `extra`——但部门长要的总数必须算得出。

    "字段存在但不能求和"不是部门能接受的答案，所以这类标准字段在 extra 里
    也要参与 sum / avg；写死在列上的口径不该把金额挡在外面。
    """
    page = _insert_interface(world["conn"], world["dept"], world["member_id"],
                             world["position"], slug="rollup-fixture-money",
                             descriptor=MONEY_DESCRIPTOR)
    _insert_entry(world["conn"], page, world["member_id"], "money-1205",
                  status="done", progress=10, work_item_id=world["item_a"],
                  extra={"amount": "120.50"})
    _insert_entry(world["conn"], page, world["member_id"], "money-80",
                  status="done", progress=10, work_item_id=world["item_b"],
                  extra={"amount": 80})

    total = api.aggregate_entries(world["conn"], world["dept"], "dept.ROLLUPD.money_total")
    assert total["available"] is True
    assert total["value"] == 200.5, "120.50 + 80，字符串也按数字读"

    average = api.aggregate_entries(world["conn"], world["dept"], "dept.ROLLUPD.money_avg")
    assert average["value"] == pytest.approx(100.25)


def test_a_non_numeric_amount_is_skipped_not_counted_as_zero(world):
    """写了个「待定」的金额是**没填**，不是 0 元。

    把它当 0 会把部门总和悄悄拉低，而且没人看得见：数字还是个数，只是小了。
    """
    page = _insert_interface(world["conn"], world["dept"], world["member_id"],
                             world["position"], slug="rollup-fixture-money",
                             descriptor=MONEY_DESCRIPTOR)
    _insert_entry(world["conn"], page, world["member_id"], "money-ok",
                  status="done", progress=10, work_item_id=world["item_a"],
                  extra={"amount": 50})
    _insert_entry(world["conn"], page, world["member_id"], "money-tbd",
                  status="done", progress=10, work_item_id=world["item_b"],
                  extra={"amount": "待定"})

    total = api.aggregate_entries(world["conn"], world["dept"], "dept.ROLLUPD.money_total")
    assert total["value"] == 50, "「待定」跳过，不是加 0"


def test_a_page_whose_amounts_are_all_unreadable_has_no_total(world):
    """一条都读不出数字时给「没有值」，不是 0——0 是个会被当真结论看的数。"""
    page = _insert_interface(world["conn"], world["dept"], world["member_id"],
                             world["position"], slug="rollup-fixture-money",
                             descriptor=MONEY_DESCRIPTOR)
    _insert_entry(world["conn"], page, world["member_id"], "money-tbd",
                  status="done", progress=10, work_item_id=world["item_a"],
                  extra={"amount": "待定"})

    total = api.aggregate_entries(world["conn"], world["dept"], "dept.ROLLUPD.money_total")
    assert total["available"] is True
    assert total["value"] is None
    assert total["entry_count"] == 1, "行还在，只是这一列没有可读的数字"


def test_work_item_limit_survives_junk_instead_of_raising(world):
    """web.py passes no limit today; a string or None must not become a 500."""

    conn = world["conn"]
    assert api.list_open_work_items(conn, world["dept"], limit=None)
    assert api.list_open_work_items(conn, world["dept"], limit="abc")
    assert len(api.list_open_work_items(conn, world["dept"], limit=0)) == 1
    assert api.list_open_work_items(conn, world["dept"], limit=9999)

