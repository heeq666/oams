"""协作室四条核心规则：can_see / 幂等 / mark_read / unread_total。

一条用例只验一件事——「一个函数测四件事」失败了不知道该怪谁。

这些规则非得跑在真库上不可：幂等靠 `collab_messages` 的
`UNIQUE (thread_id, client_msg_id)`，未读靠 `collab_reads.last_seq`，
两者都是数据库层的约束与状态，替身测不出来。

所有数据都建在一个**测完就回滚**的事务里：`seeded_database` 是整个会话共享的，
测试不许留行。
"""

from __future__ import annotations

import contextlib
import uuid
from typing import Any

import pytest
from starlette.testclient import TestClient

import workhub.api  # noqa: F401  —— 先装配好路由，TestClient 才拿得到 app
from workhub import collab, web

pytestmark = pytest.mark.db

# 种子里的五个人，各代表一种权限位置（见 tests/conftest.py 的 SEED_SQL）：
ADMIN = "ADMIN001"         # 公司级管理员：bootstrap-admin，持 workforce.grants/manage
DESIGN_LEAD = "DESIGN001"  # 设计负责人：只有 interfaces:review，**不是**部门管理员
HR_MANAGER = "MVP1001"     # 人事经理：work_items/manage，是人事部管理员
HR_SPECIALIST = "MVP1002"  # 人事专员：只读，无管理权，和人事经理同一个部门
SALES_LEAD = "MVP2001"     # 业务负责人：只读，无管理权，另一个部门
SEEDED_PEOPLE = (ADMIN, DESIGN_LEAD, HR_MANAGER, HR_SPECIALIST, SALES_LEAD)

DESIGN_SAMPLING = "design-sampling"  # 唯一一个 active 页面，填报岗位是 DESIGN_LEAD


class Room:
    """一套在一笔事务里搭出来的协作室；房间里的东西不进别的用例。"""

    def __init__(self, conn, people: dict[str, uuid.UUID], database) -> None:
        self.conn = conn
        self.people = people
        self._database = database
        names = database.all(
            "SELECT employee_code, display_name FROM workhub.workforce_members"
            " WHERE employee_code = ANY(%s)",
            (list(people),),
        )
        self.names = {row["employee_code"]: row["display_name"] for row in names}

    # --- 人 ---
    def uid(self, code: str) -> uuid.UUID:
        return self.people[code]

    def principal(self, code: str) -> dict[str, Any]:
        """够模板用的最小成员字典；`web._member` 平时返回的就是这个形状。"""

        return {
            "member_id": self.uid(code),
            "employee_code": code,
            "display_name": self.names[code],
            "employment_status": "active",
        }

    # --- 会话 ---
    def manage(self, org_code: str, *, as_code: str = ADMIN) -> dict[str, Any]:
        return collab.ensure_thread(
            self.conn, self.uid(as_code),
            kind=collab.KIND_MANAGE, org_unit_id=self._database.org_unit_id(org_code),
        )

    def fill(self, slug: str = DESIGN_SAMPLING, *, as_code: str = ADMIN) -> dict[str, Any]:
        return collab.ensure_thread(
            self.conn, self.uid(as_code),
            kind=collab.KIND_FILL, interface_id=self.interface_id(slug),
        )

    def dm(self, a_code: str, b_code: str, *, as_code: str | None = None) -> dict[str, Any]:
        return collab.ensure_thread(
            self.conn, self.uid(as_code or a_code),
            kind=collab.KIND_DM, peer_id=self.uid(b_code),
        )

    def interface_id(self, slug: str) -> uuid.UUID:
        row = self.conn.execute(
            "SELECT interface_id FROM workhub.interfaces WHERE slug = %s AND status = 'active'",
            (slug,),
        ).fetchone()
        if row is None:  # pragma: no cover - 种子不对时给一句人话，不是 KeyError
            pytest.skip(f"这个库没有 {slug} 这个页面")
        return row["interface_id"]

    def thread(self, thread_id: uuid.UUID) -> dict[str, Any]:
        thread = collab.get_thread(self.conn, thread_id)
        assert thread is not None
        return thread

    def can_see(self, code: str, thread: dict[str, Any]) -> bool:
        return collab.can_see(self.conn, self.uid(code), thread)

    # --- 说话 ---
    def post(self, thread: dict[str, Any], code: str, body: str, *,
             client_msg_id: str | None = None) -> dict[str, Any]:
        return collab.post_message(
            self.conn, thread, self.uid(code), body, client_msg_id=client_msg_id,
        )

    def message_count(self, thread: dict[str, Any], client_msg_id: str) -> int:
        row = self.conn.execute(
            "SELECT count(*) AS n FROM workhub.collab_messages"
            " WHERE thread_id = %s AND client_msg_id = %s",
            (thread["thread_id"], client_msg_id),
        ).fetchone()
        return row["n"]

    def unread(self, code: str, conn=None) -> int:
        return collab.unread_total(conn or self.conn, self.uid(code))

    def active_threads(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT thread_id FROM workhub.collab_threads WHERE status = 'active'"
            " ORDER BY created_at, thread_id",
        ).fetchall()
        return [self.thread(row["thread_id"]) for row in rows]

    def unread_from_definition(self, code: str) -> int:
        """按 `can_see` 这份**定义**数未读：看得见、且读位之后、且不是自己发的。

        `unread_total` 里那条 SQL 是它的集合版（两条路必须同答案），
        所以这里是那份「人读的规则」的对照物。
        """

        total = 0
        for thread in self.active_threads():
            if not self.can_see(code, thread):
                continue
            row = self.conn.execute(
                "SELECT count(*) AS n FROM workhub.collab_messages"
                " WHERE thread_id = %s AND seq > %s AND author_id IS DISTINCT FROM %s",
                (
                    thread["thread_id"],
                    collab.last_seen_seq(self.conn, thread["thread_id"], self.uid(code)),
                    self.uid(code),
                ),
            ).fetchone()
            total += row["n"]
        return total


class CountingConn:
    """数 `execute` 调用次数；`unread_total` 的 N+1 回退就是靠这个盯住的。"""

    def __init__(self, conn) -> None:
        self._conn = conn
        self.queries = 0

    def execute(self, *args, **kwargs):
        self.queries += 1
        return self._conn.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


@pytest.fixture()
def room(db_ready, seeded_database):
    """开一笔事务给用例用，退出时整笔回滚。"""

    conn = seeded_database.connect()
    try:
        people = {code: seeded_database.member_id(code) for code in SEEDED_PEOPLE}
        missing = sorted(code for code, member_id in people.items() if member_id is None)
        if missing:  # pragma: no cover - 别的种子集
            pytest.skip(f"这个库的种子缺人：{', '.join(missing)}")
        yield Room(conn, people, seeded_database)
    finally:
        conn.rollback()
        conn.close()


@contextlib.contextmanager
def _shares(conn):
    """把路由里的 `with connection() as conn` 换成「用用例这一笔事务」。"""

    yield conn


@contextlib.contextmanager
def _served_as(monkeypatch, room: Room, code: str):
    """以某个人的身份请求那个应用，并且共用用例的事务（看得到还没提交的行）。"""

    monkeypatch.setattr(web, "_member", lambda request: room.principal(code))
    monkeypatch.setattr(web, "connection", lambda: _shares(room.conn))
    yield TestClient(workhub.api.app)


# --------------------------------------------------------------------------
# can_see：谁能看见哪个会话
# --------------------------------------------------------------------------


def test_manage_thread_is_visible_to_a_company_scope_admin(room):
    thread = room.manage("HR")
    assert room.can_see(ADMIN, thread) is True


def test_manage_thread_is_visible_to_its_department_admin(room):
    """人事经理有 work_items/manage，所以他算人事部的管理员。"""

    thread = room.manage("HR")
    assert room.can_see(HR_MANAGER, thread) is True


def test_manage_thread_is_hidden_from_a_plain_member_of_that_department(room):
    """同一个部门不等于能看管理群：人事专员只读，没有管理权。"""

    thread = room.manage("HR")
    assert room.can_see(HR_SPECIALIST, thread) is False


def test_manage_thread_is_hidden_from_another_department(room):
    thread = room.manage("HR")
    assert room.can_see(SALES_LEAD, thread) is False


def test_manage_thread_is_hidden_from_a_lead_without_admin_rights(room):
    """设计负责人只有 interfaces:review，不是部门管理员，所以设计部管理群他也看不见。"""

    thread = room.manage("DESIGN")
    assert room.can_see(DESIGN_LEAD, thread) is False


def test_fill_thread_is_visible_to_the_position_that_fills_it(room):
    thread = room.fill()
    assert room.can_see(DESIGN_LEAD, thread) is True


def test_fill_thread_is_visible_to_a_company_scope_admin(room):
    thread = room.fill()
    assert room.can_see(ADMIN, thread) is True


def test_fill_thread_is_hidden_from_someone_outside_its_department(room):
    thread = room.fill()
    assert room.can_see(SALES_LEAD, thread) is False


def test_fill_thread_is_hidden_from_a_member_without_the_position(room):
    """人事部的人跟设计部这个页面没关系，读权限再多也不算填报岗位。"""

    thread = room.fill()
    assert room.can_see(HR_SPECIALIST, thread) is False


def test_direct_message_is_visible_to_both_people(room):
    thread = room.dm(HR_MANAGER, HR_SPECIALIST)
    assert [room.can_see(HR_MANAGER, thread), room.can_see(HR_SPECIALIST, thread)] == [True, True]


def test_direct_message_is_hidden_from_a_company_scope_admin(room):
    """私聊是唯一连公司级管理员都不给看的会话——不然「私聊」两个字就白写了。"""

    thread = room.dm(HR_MANAGER, HR_SPECIALIST)
    assert room.can_see(ADMIN, thread) is False


def test_the_left_column_lists_the_manage_group_i_am_admin_of(room):
    thread = room.manage("HR")
    mine = [row for row in collab.visible_objects(room.conn, room.uid(HR_MANAGER))
            if row["kind"] == collab.KIND_MANAGE]
    assert [row["thread_id"] for row in mine] == [thread["thread_id"]]


def test_the_left_column_does_not_list_a_manage_group_i_am_not_admin_of(room):
    """部门管理员只看见自己那个部门的管理群，别人的不出现。"""

    room.manage("HR")
    room.manage("DESIGN")
    mine = [row for row in collab.visible_objects(room.conn, room.uid(HR_MANAGER))
            if row["kind"] == collab.KIND_MANAGE]
    assert [row["title"] for row in mine] == ["人事部 · 管理群"]


def test_the_left_column_lists_the_page_my_position_fills(room):
    thread = room.fill()
    mine = [row for row in collab.visible_objects(room.conn, room.uid(DESIGN_LEAD))
            if row["kind"] == collab.KIND_FILL]
    assert [row["thread_id"] for row in mine] == [thread["thread_id"]]


def test_the_left_column_hides_a_page_i_do_not_fill(room):
    room.fill()
    mine = [row for row in collab.visible_objects(room.conn, room.uid(HR_SPECIALIST))
            if row["kind"] == collab.KIND_FILL]
    assert mine == []


def test_opening_a_thread_i_cannot_see_answers_with_the_error_panel(monkeypatch, room):
    """打不开的会话给的是「行里一句实话 + 左栏照常可用」，而且传输层就说打不开。

    传输层用 404，不用 403 也不用 200（负责人 2026-10-30 拍）：403 等于承认
    「这个会话确实存在」，给枚举留缝；200 又让浏览器和外部工具以为这是一张
    打开了的正常页面。文案本来就同时盖住「不存在」与「不开放给你」两种情况，
    所以两种原因共用一个码，页面本身照旧画出来。
    """

    thread = room.manage("HR")
    with _served_as(monkeypatch, room, SALES_LEAD) as client:
        response = client.get(f"/collab/threads/{thread['thread_id']}")
    assert response.status_code == 404
    assert "这个会话不存在，或者不开放给你" in response.text
    # 回的是右栏那一块带 role="alert" 的提示，不是一张空白页
    assert 'role="alert"' in response.text


def test_the_messages_fragment_of_a_thread_i_cannot_see_is_forbidden(monkeypatch, room):
    thread = room.manage("HR")
    with _served_as(monkeypatch, room, SALES_LEAD) as client:
        response = client.get(
            f"/collab/threads/{thread['thread_id']}/messages", params={"before_seq": 99},
        )
    assert response.status_code == 403


# --------------------------------------------------------------------------
# 幂等：同一个 client_msg_id 只落一条
# --------------------------------------------------------------------------


def test_the_same_client_msg_id_is_stored_once(room):
    thread = room.manage("HR")
    for _ in range(2):
        room.post(thread, HR_MANAGER, "断网重发的那一句", client_msg_id="c-1")
    assert room.message_count(thread, "c-1") == 1


def test_the_same_client_msg_id_returns_the_first_message(room):
    """重发要拿回**同一条**消息（id 与 seq 都一样），界面才能把那句占位换成真的。"""

    thread = room.manage("HR")
    first = room.post(thread, HR_MANAGER, "断网重发的那一句", client_msg_id="c-2")
    again = room.post(thread, HR_MANAGER, "断网重发的那一句", client_msg_id="c-2")
    assert again["message_id"] == first["message_id"]
    assert again["seq"] == first["seq"]


def test_a_second_client_msg_id_is_a_second_message(room):
    """幂等不能宽到把不同的句子也并掉。"""

    thread = room.manage("HR")
    first = room.post(thread, HR_MANAGER, "第一句", client_msg_id="c-3")
    second = room.post(thread, HR_MANAGER, "第二句", client_msg_id="c-4")
    assert second["message_id"] != first["message_id"]


def test_the_same_client_msg_id_may_be_reused_in_another_thread(room):
    """唯一索引是 (thread_id, client_msg_id)：两个会话各用一次 c-5 是合法的。"""

    hr = room.manage("HR")
    design = room.manage("DESIGN")
    here = room.post(hr, ADMIN, "人事部这边", client_msg_id="c-5")
    there = room.post(design, ADMIN, "设计部这边", client_msg_id="c-5")
    assert here["message_id"] != there["message_id"]


def test_posting_without_a_client_msg_id_appends_every_time(room):
    thread = room.manage("HR")
    first = room.post(thread, HR_MANAGER, "同一句话")
    second = room.post(thread, HR_MANAGER, "同一句话")
    assert second["seq"] == first["seq"] + 1


# --------------------------------------------------------------------------
# mark_read：已读位只往前走
# --------------------------------------------------------------------------


def test_mark_read_returns_the_latest_seq(room):
    thread = room.manage("HR")
    room.post(thread, HR_MANAGER, "一")
    room.post(thread, HR_MANAGER, "二")
    assert collab.mark_read(room.conn, thread["thread_id"], room.uid(HR_SPECIALIST)) == 2


def test_mark_read_clears_the_unread_total(room):
    thread = room.manage("HR")
    room.post(thread, ADMIN, "一")
    room.post(thread, ADMIN, "二")
    assert room.unread(HR_MANAGER) == 2
    collab.mark_read(room.conn, thread["thread_id"], room.uid(HR_MANAGER))
    assert room.unread(HR_MANAGER) == 0


def test_mark_read_never_moves_the_read_position_backwards(room):
    """拉平只往前走：读位已经到 99，再来一次只算出 2，也得留着 99。"""

    thread = room.manage("HR")
    room.post(thread, HR_MANAGER, "一")
    room.post(thread, HR_MANAGER, "二")
    room.conn.execute(
        "INSERT INTO workhub.collab_reads (thread_id, member_id, last_seq)"
        " VALUES (%s, %s, 99)"
        " ON CONFLICT (thread_id, member_id) DO UPDATE SET last_seq = 99",
        (thread["thread_id"], room.uid(HR_MANAGER)),
    )
    assert collab.mark_read(room.conn, thread["thread_id"], room.uid(HR_MANAGER)) == 99


def test_mark_read_writes_the_read_timestamp(room):
    thread = room.manage("HR")
    room.post(thread, HR_MANAGER, "一")
    room.conn.execute(
        "DELETE FROM workhub.collab_reads WHERE thread_id = %s AND member_id = %s",
        (thread["thread_id"], room.uid(HR_MANAGER)),
    )
    collab.mark_read(room.conn, thread["thread_id"], room.uid(HR_MANAGER))
    row = room.conn.execute(
        "SELECT read_at FROM workhub.collab_reads WHERE thread_id = %s AND member_id = %s",
        (thread["thread_id"], room.uid(HR_MANAGER)),
    ).fetchone()
    assert row is not None and row["read_at"] is not None


def test_mark_read_only_touches_my_own_read_position(room):
    """两个人在同一个群里：我读了不等于他读了。"""

    thread = room.manage("HR")
    room.post(thread, HR_MANAGER, "一")
    collab.mark_read(room.conn, thread["thread_id"], room.uid(ADMIN))
    assert collab.last_seen_seq(room.conn, thread["thread_id"], room.uid(HR_MANAGER)) == 0


def test_last_seen_seq_of_a_thread_i_never_opened_is_zero(room):
    thread = room.manage("HR")
    room.post(thread, HR_MANAGER, "一")
    assert collab.last_seen_seq(room.conn, thread["thread_id"], room.uid(DESIGN_LEAD)) == 0


# --------------------------------------------------------------------------
# unread_total：看得见的、别人发的、读位之后的
# --------------------------------------------------------------------------


def test_unread_total_ignores_threads_i_cannot_see(room):
    """人事管理群里说三句，业务负责人一句都不该被算进未读。"""

    thread = room.manage("HR")
    for body in ("一", "二", "三"):
        room.post(thread, HR_MANAGER, body)
    assert room.unread(SALES_LEAD) == 0


def test_unread_total_counts_messages_in_a_thread_i_can_see(room):
    thread = room.manage("HR")
    room.post(thread, HR_MANAGER, "一")
    room.post(thread, HR_MANAGER, "二")
    assert room.unread(ADMIN) == 2


def test_unread_total_ignores_my_own_messages(room):
    thread = room.manage("HR")
    room.post(thread, ADMIN, "我发的")
    room.post(thread, HR_MANAGER, "他发的")
    assert room.unread(ADMIN) == 1


def test_unread_total_sums_across_visible_threads(room):
    hr = room.manage("HR")
    design = room.manage("DESIGN")
    room.post(hr, HR_MANAGER, "人事一")
    room.post(hr, HR_MANAGER, "人事二")
    room.post(design, DESIGN_LEAD, "设计一")
    assert room.unread(ADMIN) == 3


def test_unread_total_drops_to_zero_after_mark_read(room):
    thread = room.manage("HR")
    room.post(thread, HR_MANAGER, "一")
    collab.mark_read(room.conn, thread["thread_id"], room.uid(ADMIN))
    assert room.unread(ADMIN) == 0


def test_unread_total_agrees_with_can_see_for_every_seeded_member(room):
    """`can_see` 是规则的定义，`unread_total` 里那条 SQL 是它的集合版。

    两处各写了一遍这条规则，就得有一个地方盯着它们给出同一个答案——
    改规则时漏改一边，这条先红。
    """

    hr = room.manage("HR")
    design = room.manage("DESIGN")
    page = room.fill()
    room.dm(HR_MANAGER, HR_SPECIALIST)
    for thread, code in ((hr, HR_MANAGER), (hr, HR_SPECIALIST), (design, DESIGN_LEAD),
                         (page, DESIGN_LEAD), (page, ADMIN)):
        room.post(thread, code, f"{code} 说了一句")

    disagreeing = [
        code for code in SEEDED_PEOPLE
        if room.unread(code) != room.unread_from_definition(code)
    ]
    assert disagreeing == []


def test_unread_total_does_not_query_once_per_thread(room):
    """查询条数不能随「有未读的会话数」增长——这个数长在每一页顶上。

    注意必须有未读：逐条问权限那版只在会话有未读时才问，空群再多也问不出一声，
    那样的用例是空检。
    """

    def queries() -> int:
        counter = CountingConn(room.conn)
        room.unread(ADMIN, conn=counter)
        return counter.queries

    hr = room.manage("HR")
    design = room.manage("DESIGN")
    room.post(hr, HR_MANAGER, "人事部这边有话")
    room.post(design, DESIGN_LEAD, "设计部这边也有话")
    with_two = queries()

    sales = room.manage("SALES")  # 公司级管理员看得见所有部门管理群
    page = room.fill()            # 加上唯一那个 active 页面的汇报群
    room.post(sales, SALES_LEAD, "业务部这边也有话")
    room.post(page, DESIGN_LEAD, "页面上也有话")
    with_four = queries()

    assert with_four == with_two
