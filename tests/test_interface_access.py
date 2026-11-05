"""页面权限（谁能看 / 谁能填）：契约 `docs/architecture/interface-access-contract.md` 的那六个函数。

三件必须同时成立的，少一条这层就白加：

1. **只放宽、不收紧**——部门内的人没有行也照旧能看能填（存量 905 条用例一条都不许红）；
2. **行只对那一页生效**，不是「这个人能看这个部门」的部门级判据（`can_access_org_unit` 不动）；
3. **祖先链向上走**：给公司根发一行就是「全公司」，给某部门发一行，它下面的小组也算在
   这条链上——用递归 CTE 一次查完，不在 Python 里爬树。

用例自带一套世界（公司根 + FIN/GA 两个部门 + FIN 下的一个小组 + 四个只靠岗位定范围的
人），不碰演示库、不依赖种子里的显示名或条数。
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import conftest as suite  # noqa: E402  pytest 会把 tests/ 放进 sys.path

from oams import security  # noqa: E402

pytestmark = pytest.mark.db

ADMIN = "ADMIN001"        # bootstrap-admin 建的公司级管理员
FIN_MEMBER = "FIN001"     # FIN 部的人：本部门默认能看能填，也是「部门管理员」的样子
GA_MEMBER = "GA001"       # GA 部的人：对 FIN 的页默认什么都看不到
TEAM_MEMBER = "TEAM001"   # FIN 下面的小组：祖先链要走到 FIN 与公司根
SEATLESS = "NOSEAT01"     # 没有任何岗位：可授权集合是空的

FIN_PAGE = "expense-log"
FIN_PAGE_TWO = "travel-claim"


@pytest.fixture(scope="module")
def world(db_ready):
    import psycopg
    from psycopg.rows import dict_row

    name, url = suite._create_database(db_ready)
    try:
        suite._cli(["db", "init"], url)
        suite._cli(
            ["bootstrap-admin", "--name", "管理员", "--employee-code", ADMIN,
             "--org-code", "COMPANY", "--org-name", "公司"],
            url,
        )
        with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
            company = conn.execute(
                "SELECT org_unit_id FROM oams.org_units WHERE org_code = 'COMPANY'"
            ).fetchone()["org_unit_id"]

            def org_unit(code, title, *, parent=company, unit_type="department"):
                return conn.execute(
                    """INSERT INTO oams.org_units (org_code, name, unit_type, parent_org_unit_id)
                       VALUES (%s, %s, %s, %s) RETURNING org_unit_id""",
                    (code, title, unit_type, parent)).fetchone()["org_unit_id"]

            def member(employee_code, display_name):
                return conn.execute(
                    """INSERT INTO oams.workforce_members
                           (employee_code, display_name, employment_status)
                       VALUES (%s, %s, 'active') RETURNING member_id""",
                    (employee_code, display_name)).fetchone()["member_id"]

            def seat(member_id, org_unit_id, position_code, title):
                position = conn.execute(
                    """INSERT INTO oams.positions (position_code, title, org_unit_id)
                       VALUES (%s, %s, %s) RETURNING position_id""",
                    (position_code, title, org_unit_id)).fetchone()["position_id"]
                conn.execute(
                    """INSERT INTO oams.position_assignments
                           (member_id, position_id, assignment_type, status, starts_at, reason)
                       VALUES (%s, %s, 'primary', 'active', now() - interval '1 day', '测试用任职')""",
                    (member_id, position))

            fin = org_unit("FIN", "财务部")
            ga = org_unit("GA", "行政部")
            team = org_unit("FIN_OPS", "财务运营组", parent=fin, unit_type="team")

            fin_id, ga_id, team_id = str(fin), str(ga), str(team)
            fin_member = member(FIN_MEMBER, "财务同事")
            ga_member = member(GA_MEMBER, "行政同事")
            team_member = member(TEAM_MEMBER, "财务运营同事")
            seatless = member(SEATLESS, "没有岗位的人")
            seat(fin_member, fin, "FIN_LEAD", "财务部负责人")
            seat(ga_member, ga, "GA_LEAD", "行政部负责人")
            seat(team_member, team, "FIN_OPS_LEAD", "财务运营组负责人")

            admin_id = conn.execute(
                "SELECT member_id FROM oams.workforce_members WHERE employee_code = %s",
                (ADMIN,)).fetchone()["member_id"]

            def page(owner, slug, title):
                conn.execute(
                    """INSERT INTO oams.interfaces (org_unit_id, slug, name, status,
                                                    schema_version, descriptor)
                       VALUES (%s, %s, %s, 'active', '0.2', '{}'::jsonb)""",
                    (owner, slug, title))

            page(fin, FIN_PAGE, "报销登记")
            page(fin, FIN_PAGE_TWO, "出差报销")

        yield {
            "url": url,
            "company": str(company),
            "fin": fin_id,
            "ga": ga_id,
            "team": team_id,
            "fin_member": str(fin_member),
            "ga_member": str(ga_member),
            "team_member": str(team_member),
            "seatless": str(seatless),
            "admin_member": str(admin_id),
        }
    finally:
        suite._drop_database(db_ready, name)


@pytest.fixture()
def conn(world):
    """一条自动提交的连接：函数本身不 commit，交给它自己决定什么时候落库。"""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(world["url"], autocommit=True, row_factory=dict_row) as connection:
        yield connection


def _grant(world, department, slug, *, can_read=True, can_write=False, can_base=False, note=None):
    """直接落一行（不经过 set_interface_audience），用来单独验解析。"""
    import psycopg

    with psycopg.connect(world["url"], autocommit=True) as connection:
        connection.execute(
            """INSERT INTO oams.interface_access
                   (owner_org_unit_id, slug, org_unit_id, can_read, can_write, can_base, note)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (owner_org_unit_id, slug, org_unit_id)
               DO UPDATE SET can_read = EXCLUDED.can_read, can_write = EXCLUDED.can_write,
                             can_base = EXCLUDED.can_base""",
            (world["fin"], slug, department, can_read, can_write, can_base, note))


def _clear(world, slug=FIN_PAGE):
    import psycopg

    with psycopg.connect(world["url"], autocommit=True) as connection:
        connection.execute(
            "DELETE FROM oams.interface_access WHERE owner_org_unit_id = %s AND slug = %s",
            (world["fin"], slug))


# ---------------------------------------------------------------------------
# 一、部门内：没有行也照旧能看能填（只放宽不收紧的保证）
# ---------------------------------------------------------------------------


def test_a_department_member_always_sees_and_fills_their_own_page(world, conn):
    assert security.interface_audience(conn, world["fin"], FIN_PAGE) == []
    assert security.can_view_interface(conn, world["fin_member"], world["fin"], FIN_PAGE) is True
    assert security.can_fill_interface(conn, world["fin_member"], world["fin"], FIN_PAGE) is True


def test_the_system_administrator_still_sees_every_page(world, conn):
    assert security.can_view_interface(conn, world["admin_member"], world["fin"], FIN_PAGE) is True
    assert security.can_fill_interface(conn, world["admin_member"], world["fin"], FIN_PAGE) is True


def test_another_department_sees_nothing_without_a_row(world, conn):
    assert security.can_view_interface(conn, world["ga_member"], world["fin"], FIN_PAGE) is False
    assert security.can_fill_interface(conn, world["ga_member"], world["fin"], FIN_PAGE) is False


# ---------------------------------------------------------------------------
# 二、跨部门：只读 / 可填 / 只对这一页生效
# ---------------------------------------------------------------------------


def test_read_only_row_lets_them_view_but_not_fill(world, conn):
    _clear(world)
    _grant(world, world["ga"], FIN_PAGE, can_read=True, can_write=False)
    assert security.can_view_interface(conn, world["ga_member"], world["fin"], FIN_PAGE) is True
    assert security.can_fill_interface(conn, world["ga_member"], world["fin"], FIN_PAGE) is False


def test_write_row_lets_them_fill(world, conn):
    _clear(world)
    _grant(world, world["ga"], FIN_PAGE, can_read=True, can_write=True)
    assert security.can_fill_interface(conn, world["ga_member"], world["fin"], FIN_PAGE) is True


def test_a_row_opens_only_the_page_it_names(world, conn):
    _clear(world)
    _grant(world, world["ga"], FIN_PAGE, can_read=True, can_write=True)
    assert security.can_view_interface(conn, world["ga_member"], world["fin"], FIN_PAGE_TWO) is False
    assert security.can_fill_interface(conn, world["ga_member"], world["fin"], FIN_PAGE_TWO) is False


def test_page_level_access_does_not_widen_department_access(world, conn):
    """页面权限不改部门级判据：GA 的人看得见那一页，也仍然进不了 FIN 部。"""
    _clear(world)
    _grant(world, world["ga"], FIN_PAGE, can_read=True, can_write=True)
    assert security.can_access_org_unit(conn, world["ga_member"], world["fin"]) is False


# ---------------------------------------------------------------------------
# 三、公司根 = 全公司（祖先链向上走）
# ---------------------------------------------------------------------------


def test_a_company_root_row_opens_the_page_to_everyone(world, conn):
    _clear(world)
    _grant(world, world["company"], FIN_PAGE, can_read=True)
    # GA 的座位部门不在这行上，但它的祖先链走到公司根
    assert security.can_view_interface(conn, world["ga_member"], world["fin"], FIN_PAGE) is True
    # 小组的链是 小组 → FIN → 公司根
    assert security.can_view_interface(conn, world["team_member"], world["fin"], FIN_PAGE) is True
    # 只给看，不给填
    assert security.can_fill_interface(conn, world["ga_member"], world["fin"], FIN_PAGE) is False


def test_a_department_row_also_reaches_the_team_below_it(world, conn):
    _clear(world)
    _grant(world, world["fin"], FIN_PAGE, can_read=True, can_write=True)
    assert security.can_fill_interface(conn, world["team_member"], world["fin"], FIN_PAGE) is True


# ---------------------------------------------------------------------------
# 三之二、「可作参考」第三格：三格独立，参考权不蕴含能看
# ---------------------------------------------------------------------------


def test_a_base_only_row_lets_them_copy_the_page_but_not_read_it(world, conn):
    """只勾参考是合法组合：能拿这一页当模板，但打不开它、也读不到条目。

    这条同时钉住两件事——`can_base_interface` 认得这一行，而
    `can_view_interface` / `can_fill_interface` **不会**因为它变成真。
    """
    _clear(world)
    _grant(world, world["ga"], FIN_PAGE, can_read=False, can_write=False, can_base=True)
    assert security.can_base_interface(conn, world["ga_member"], world["fin"], FIN_PAGE) is True
    assert security.can_view_interface(conn, world["ga_member"], world["fin"], FIN_PAGE) is False
    assert security.can_fill_interface(conn, world["ga_member"], world["fin"], FIN_PAGE) is False


def test_a_base_row_does_not_leak_into_the_read_column(world, conn):
    """`interface_audience` 要如实带出三格：只给参考时，读/写都是假。"""
    _clear(world)
    _grant(world, world["ga"], FIN_PAGE, can_read=False, can_write=False, can_base=True)
    row = security.interface_audience(conn, world["fin"], FIN_PAGE)[0]
    assert (row["can_read"], row["can_write"], row["can_base"]) == (False, False, True)


def test_the_owning_department_can_always_use_its_own_page_as_a_base(world, conn):
    """本部门恒真：没有行也能参考，跟能看能填同一个来源（dash 只放宽不收紧）。"""
    _clear(world)
    assert security.can_base_interface(conn, world["fin_member"], world["fin"], FIN_PAGE) is True
    assert security.can_base_interface(conn, world["admin_member"], world["fin"], FIN_PAGE) is True


def test_a_base_row_follows_the_ancestor_chain(world, conn):
    """给公司根发参考权，链上所有人都能参考；给兄弟部门发，另一个人拿不到。"""
    _clear(world)
    _grant(world, world["company"], FIN_PAGE, can_read=False, can_write=False, can_base=True)
    assert security.can_base_interface(conn, world["ga_member"], world["fin"], FIN_PAGE) is True
    assert security.can_base_interface(conn, world["team_member"], world["fin"], FIN_PAGE) is True

    _clear(world)
    _grant(world, world["ga"], FIN_PAGE, can_read=False, can_write=False, can_base=True)
    assert security.can_base_interface(conn, world["team_member"], world["fin"], FIN_PAGE) is False


def test_saving_three_flags_covers_them_all(world, conn):
    """一次保存是整笔覆盖：漏传 `can_base` 就是撤回参考权，不是「保持原样」。"""
    _clear(world)
    ga = uuid.UUID(world["ga"])
    security.set_interface_audience(
        conn, world["fin"], FIN_PAGE,
        [{"org_unit_id": ga, "can_read": True, "can_write": False, "can_base": True}],
        granted_by=uuid.UUID(world["admin_member"]))
    row = security.interface_audience(conn, world["fin"], FIN_PAGE)[0]
    assert (row["can_read"], row["can_write"], row["can_base"]) == (True, False, True)

    security.set_interface_audience(
        conn, world["fin"], FIN_PAGE,
        [{"org_unit_id": ga, "can_read": True, "can_write": False}],
        granted_by=uuid.UUID(world["admin_member"]))
    row = security.interface_audience(conn, world["fin"], FIN_PAGE)[0]
    assert row["can_base"] is False
    assert security.can_base_interface(conn, world["ga_member"], world["fin"], FIN_PAGE) is False


def test_a_row_with_only_the_base_flag_is_stored(world, conn):
    """三格全假才不落库。原来看 `can_read` 一个，会把「只给参考」的行悄悄丢掉。"""
    _clear(world)
    security.set_interface_audience(
        conn, world["fin"], FIN_PAGE,
        [{"org_unit_id": uuid.UUID(world["ga"]), "can_read": False, "can_write": False,
          "can_base": True}],
        granted_by=uuid.UUID(world["admin_member"]))
    rows = security.interface_audience(conn, world["fin"], FIN_PAGE)
    assert [(row["org_code"], row["can_read"], row["can_base"]) for row in rows] == [("GA", False, True)]


def test_a_page_that_does_not_exist_has_no_base_right_either(world, conn):
    assert security.can_base_interface(conn, world["fin_member"], world["fin"], "no-such-page") is False


def test_a_member_without_a_seat_reaches_nothing(world, conn):
    _clear(world)
    _grant(world, world["ga"], FIN_PAGE)
    assert security.can_view_interface(conn, world["seatless"], world["fin"], FIN_PAGE) is False


# ---------------------------------------------------------------------------
# 四、读回来 / 存下去
# ---------------------------------------------------------------------------


def test_audience_returns_codes_flags_and_units(world, conn):
    _clear(world)
    _grant(world, world["ga"], FIN_PAGE, can_read=True, can_write=True)
    audience = security.interface_audience(conn, world["fin"], FIN_PAGE)
    assert len(audience) == 1
    row = audience[0]
    assert set(row) == {"org_unit_id", "org_code", "name", "unit_type",
                        "can_read", "can_write", "can_base", "granted_at"}
    assert row["org_code"] == "GA"
    assert row["name"] == "行政部"
    assert row["unit_type"] == "department"
    assert row["can_read"] is True and row["can_write"] is True


def test_saving_replaces_the_whole_list(world, conn):
    _clear(world)
    security.set_interface_audience(
        conn, world["fin"], FIN_PAGE,
        [{"org_unit_id": uuid.UUID(world["ga"]), "can_read": True, "can_write": False}],
        granted_by=uuid.UUID(world["admin_member"]))
    assert [row["org_code"] for row in security.interface_audience(conn, world["fin"], FIN_PAGE)] == ["GA"]

    # 换成 FIN 自己的行：GA 的祖先链是 GA → 公司根，走不到 FIN，所以它该看不见了
    security.set_interface_audience(
        conn, world["fin"], FIN_PAGE,
        [{"org_unit_id": uuid.UUID(world["fin"]), "can_read": True, "can_write": True}],
        granted_by=uuid.UUID(world["admin_member"]))
    assert [row["org_code"] for row in security.interface_audience(conn, world["fin"], FIN_PAGE)] == ["FIN"]
    # 覆盖之后，原来那行真没了——GA 又回到什么都看不到
    assert security.can_view_interface(conn, world["ga_member"], world["fin"], FIN_PAGE) is False


def test_writing_implies_reading_when_saved(world, conn):
    """表上有 CHECK，保存时先把它掰正：给「可填」不给「可看」是把库逼红，不是本事。"""
    _clear(world)
    security.set_interface_audience(
        conn, world["fin"], FIN_PAGE,
        [{"org_unit_id": uuid.UUID(world["ga"]), "can_read": False, "can_write": True}],
        granted_by=uuid.UUID(world["admin_member"]))
    row = security.interface_audience(conn, world["fin"], FIN_PAGE)[0]
    assert (row["can_read"], row["can_write"]) == (True, True)
    assert security.can_fill_interface(conn, world["ga_member"], world["fin"], FIN_PAGE) is True


def test_a_row_with_neither_flag_is_not_stored(world, conn):
    _clear(world)
    security.set_interface_audience(
        conn, world["fin"], FIN_PAGE,
        [{"org_unit_id": uuid.UUID(world["ga"]), "can_read": False, "can_write": False}],
        granted_by=uuid.UUID(world["admin_member"]))
    assert security.interface_audience(conn, world["fin"], FIN_PAGE) == []


def test_saving_the_same_department_twice_overwrites_instead_of_appending(world, conn):
    _clear(world)
    rows = [{"org_unit_id": uuid.UUID(world["ga"]), "can_read": True, "can_write": False},
            {"org_unit_id": uuid.UUID(world["ga"]), "can_read": True, "can_write": True}]
    security.set_interface_audience(conn, world["fin"], FIN_PAGE, rows,
                                    granted_by=uuid.UUID(world["admin_member"]))
    audience = security.interface_audience(conn, world["fin"], FIN_PAGE)
    assert len(audience) == 1
    assert audience[0]["can_write"] is True


def test_a_page_that_does_not_exist_returns_false_instead_of_raising(world, conn):
    assert security.can_view_interface(conn, world["fin_member"], world["fin"], "no-such-page") is False
    assert security.can_fill_interface(conn, world["fin_member"], world["fin"], "no-such-page") is False


# ---------------------------------------------------------------------------
# 五、可授权集合：公司级 = None，部门管理员 = 自己的座位部门
# ---------------------------------------------------------------------------


def test_grantable_is_none_for_the_system_administrator(world, conn):
    assert security.grantable_org_unit_ids(conn, world["admin_member"]) is None


def test_grantable_is_the_seat_departments_for_a_department_admin(world, conn):
    granted = security.grantable_org_unit_ids(conn, world["fin_member"])
    assert granted == {uuid.UUID(world["fin"])}
    # 公司根不在里面：「开给全公司」是公司级才有的动作
    assert uuid.UUID(world["company"]) not in granted
    assert uuid.UUID(world["ga"]) not in granted


def test_grantable_is_empty_without_a_seat(world, conn):
    assert security.grantable_org_unit_ids(conn, world["seatless"]) == set()


# ---------------------------------------------------------------------------
# 六、页头那句「谁开的」：只挑该说哪一行，不改判据
# ---------------------------------------------------------------------------


def test_via_picks_the_named_department_row(world, conn):
    _clear(world)
    _grant(world, world["ga"], FIN_PAGE, can_read=True)
    row = security.interface_access_via(conn, world["ga_member"], world["fin"], FIN_PAGE)
    assert row is not None and row["org_code"] == "GA"


def test_via_prefers_the_own_seat_over_an_ancestor(world, conn):
    """小组的人：FIN 行（祖先点名部门）也打开这页，但「谁开的」要挑他座位那一级说。"""
    _clear(world)
    _grant(world, world["fin"], FIN_PAGE, can_read=True)
    _grant(world, world["team"], FIN_PAGE, can_read=True, can_write=True)
    row = security.interface_access_via(conn, world["team_member"], world["fin"], FIN_PAGE)
    assert row["org_code"] == "FIN_OPS" and row["can_write"] is True


def test_via_falls_back_to_the_company_root(world, conn):
    _clear(world)
    _grant(world, world["company"], FIN_PAGE, can_read=True)
    row = security.interface_access_via(conn, world["ga_member"], world["fin"], FIN_PAGE)
    assert row is not None and row["unit_type"] == "company"


def test_via_is_none_without_a_matching_row_or_a_seat(world, conn):
    _clear(world)
    assert security.interface_access_via(conn, world["ga_member"], world["fin"], FIN_PAGE) is None
    assert security.interface_access_via(conn, world["seatless"], world["fin"], FIN_PAGE) is None


# ---------------------------------------------------------------------------
# 六点五、左栏「开给你的页」：只列**真被开过**的页，公司级的人一条都不列
#
# 这里踩过一个坑：第一版拿 `can_view_interface` 当判据，而它对公司级的人恒为
# True，于是系统管理员的左栏列出了全公司所有部门的页、小标题还写着「开给你的
# 页」——等于替他编了一条并不存在的授权。判据必须是「这一页真的开给了我」。
# ---------------------------------------------------------------------------


def _shared_pages(world, member):
    from oams import api

    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(world["url"], autocommit=True, row_factory=dict_row) as connection:
        return api._shared_pages(connection, member)


def test_a_company_admin_gets_no_shared_pages(world):
    """系统管理员本来哪都能进：这一栏对他没有信息量，列出来等于编授权。"""
    _clear(world)
    assert _shared_pages(world, world["admin_member"]) == []


def test_a_page_opened_to_my_department_shows_up(world):
    _clear(world)
    _grant(world, world["ga"], FIN_PAGE, can_read=True)
    slugs = [row["slug"] for row in _shared_pages(world, world["ga_member"])]
    assert slugs == [FIN_PAGE]


def test_a_page_not_opened_to_me_does_not_show_up(world):
    _clear(world)
    _grant(world, world["ga"], FIN_PAGE, can_read=True)
    assert _shared_pages(world, world["seatless"]) == []


def test_a_base_only_row_does_not_show_up_as_an_opened_page(world):
    """「可作参考」不蕴含能查看：拿它当模板可以，但那一页不该出现在「开给你的页」里。"""
    _clear(world)
    _grant(world, world["ga"], FIN_PAGE, can_read=False, can_write=False, can_base=True)
    assert _shared_pages(world, world["ga_member"]) == []


# ---------------------------------------------------------------------------
# 七、建表是幂等的：`db init` 再跑一遍，表还在、行还在
# ---------------------------------------------------------------------------


def test_db_init_is_idempotent_with_the_new_table(world):
    security_ids = []
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(world["url"], autocommit=True, row_factory=dict_row) as conn:
        security_ids = [row["event_id"] for row in conn.execute(
            "SELECT event_id FROM oams.workforce_audit_events").fetchall()]

    suite._cli(["db", "init"], world["url"])

    with psycopg.connect(world["url"], autocommit=True, row_factory=dict_row) as conn:
        assert conn.execute(
            "SELECT to_regclass('oams.interface_access') AS name").fetchone()["name"] is not None
        assert [row["event_id"] for row in conn.execute(
            "SELECT event_id FROM oams.workforce_audit_events").fetchall()] == security_ids
