"""「别的部门开给你的页」，读者一看就知道自己拿到的是哪一档。

权限三格（查看 / 编辑 / 可作参考）是分开勾的，之前界面上分不出来：被共享进来的页只在
名字后面挂一枚「别的部门开给你的」，可见范围那一列写的还是**页面主人的**范围（「仅本部
门」），读者据此会以为这页不给自己看；只拿到「可作参考」的部门更糟——那一行挂着「打开
填写」，点下去才知道不行。

这组用例钉三件事：

1. 那一列说的是**我拿到哪一档**，不是这一页本身的可见范围；
2. 只给参考的那一行**不给**「打开填写」，而给「照着做一版」这个真能走通的下一步；
3. 只拿到参考权的部门**不会**因为「有页面开给它」就出现在左栏——左栏那条是「能看」的
   判据（`shared_org_unit_ids`），拿参考权的人点进去会被拒，不该给他这扇门。

判据来自 `security.interface_rights`：三个 `can_*_interface` 与列表页取同一份结果，
这里不另写一套断言口径。
"""

from __future__ import annotations

import contextlib

import psycopg
import pytest
from psycopg.rows import dict_row
from starlette.testclient import TestClient

import oams.api
from oams import security, web
from oams.config import settings

pytestmark = pytest.mark.db

ADMIN = "ADMIN001"     # 系统管理员：本来就能进所有部门，不该被当成客人
OWNER = "DESIGN001"    # 设计部：这张页的主人
GUEST = "MVP1001"      # 周岚（人事经理）：靠下面造的那一行走进来
GUEST_DEPT = "HR"      # 授权发给部门，不发给个人——工号不是部门代码

SLUG = "design-sampling"   # 种子里设计部那张已经上线的页
OWNER_NAME = "设计部"
GUEST_NAME = "人事部"


@contextlib.contextmanager
def _one_transaction(conn):
    yield conn


@pytest.fixture()
def shared(seeded_database, monkeypatch):
    """一笔事务里的种子库 + 一个能按身份切人的 TestClient。"""
    conn = psycopg.connect(seeded_database.url, row_factory=dict_row)
    monkeypatch.setattr(web, "connection", lambda: _one_transaction(conn))
    monkeypatch.setattr(oams.api, "connection", lambda: _one_transaction(conn))

    def unit(code):
        row = conn.execute("SELECT org_unit_id FROM oams.org_units WHERE org_code = %s",
                           (code,)).fetchone()
        assert row is not None, f"{code} 不在种子库里"
        return row["org_unit_id"]

    page = conn.execute(
        """SELECT i.interface_id, i.org_unit_id FROM oams.interfaces i
             JOIN oams.org_units o ON o.org_unit_id = i.org_unit_id
            WHERE o.org_code = 'DESIGN' AND i.slug = %s AND i.status = 'active'
            LIMIT 1""", (SLUG,)).fetchone()
    assert page is not None, "种子里应该有设计部那张已上线的页"

    people = {code: conn.execute(
        "SELECT member_id FROM oams.workforce_members WHERE employee_code = %s",
        (code,)).fetchone()["member_id"]
        for code in (ADMIN, OWNER, GUEST)}

    state = {"me": GUEST, "cookie": "shared-page-rights-test"}

    def _principal(request):
        return {"member_id": people[state["me"]],
                "employee_code": state["me"],
                "display_name": state["me"],
                "employment_status": "active"}

    monkeypatch.setattr(oams.api, "principal_from_request", _principal)

    def client() -> TestClient:
        made = TestClient(oams.api.app)
        made.cookies.set(settings.web_session_cookie, state["cookie"])
        return made

    def as_(code):
        state["me"] = code

    def grant(*, can_read=True, can_write=False, can_base=False, code=GUEST_DEPT):
        """页面设置弹窗保存时写的那一行（三格独立）。"""
        conn.execute(
            """INSERT INTO oams.interface_access
                   (owner_org_unit_id, slug, org_unit_id, can_read, can_write, can_base,
                    granted_by, note)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 'shared-page-rights')
               ON CONFLICT (owner_org_unit_id, slug, org_unit_id)
               DO UPDATE SET can_read = EXCLUDED.can_read, can_write = EXCLUDED.can_write,
                             can_base = EXCLUDED.can_base""",
            (page["org_unit_id"], SLUG, unit(code), can_read, can_write, can_base,
             people[ADMIN]))

    def clear():
        conn.execute("""DELETE FROM oams.interface_access
                         WHERE owner_org_unit_id = %s AND slug = %s""",
                     (page["org_unit_id"], SLUG))

    def listing():
        return client().get("/interfaces", follow_redirects=False)

    def row_html():
        """那一行本身（含页面名的那一串），避免整张表混进来。"""
        body = listing().text
        start = body.find("设计部打样进度")
        assert start >= 0, "设计部那张页应该出现在「部门页面」列表里"
        end = body.find("</tr>", start)
        return body[start:end if end > start else start + 2000]

    made = {"conn": conn, "as_": as_, "grant": grant, "clear": clear,
            "listing": listing, "row_html": row_html, "unit": unit,
            "owner": page["org_unit_id"], "interface_id": page["interface_id"],
            "member": people[GUEST], "person": people, "code": GUEST,
            "cookie": state["cookie"]}
    try:
        yield made
    finally:
        conn.rollback()
        conn.close()


def test_only_read_shows_the_rights_i_actually_got(shared):
    """只给查看：这一格说「开给你的：可查看」，不再是主人那句「仅本部门」。"""
    shared["clear"]()
    shared["grant"](can_read=True)
    row = shared["row_html"]()
    assert "开给你的：可查看" in row, row
    assert "仅本部门" not in row, "别部门的页，可见范围一列不该再写主人的范围"
    assert "打开填写" in row


def test_edit_and_reference_are_listed_as_they_are(shared):
    """给查看 + 参考：两个都列出来，不合并成一句含糊的话。"""
    shared["clear"]()
    shared["grant"](can_read=True, can_base=True)
    row = shared["row_html"]()
    assert "开给你的：可查看、可当模板" in row, row


def test_reference_only_does_not_offer_a_page_it_would_refuse(shared):
    """只给参考：不给「打开填写」（点了会 403），改给「照着做一版」。"""
    shared["clear"]()
    shared["grant"](can_read=False, can_base=True)
    row = shared["row_html"]()
    assert "只开给你当模板，看不到里面的数据" in row, row
    assert "打开填写" not in row, "拿不到查看权就别画那个会把人挡回来的链接"
    assert "照着做一版" in row, row
    assert f"based_on={shared['interface_id']}" in row, "建页页要把这一页预选成基础页"


def test_reference_only_still_cannot_open_the_page(shared):
    """这条是判据本身的边界：参考权不许顺手变成「能看」。"""
    shared["clear"]()
    shared["grant"](can_read=False, can_base=True)
    rights = security.interface_rights(shared["conn"], shared["member"],
                                       shared["owner"], SLUG)
    assert rights == {"can_read": False, "can_write": False, "can_base": True}, rights
    assert security.can_view_interface(shared["conn"], shared["member"],
                                       shared["owner"], SLUG) is False


def test_reference_only_does_not_add_the_department_to_the_rail(shared):
    """只有参考权：左栏不出现这个部门——点进去会被拒，那不是一条能走的路。"""
    shared["clear"]()
    shared["grant"](can_read=False, can_base=True)
    assert security.shared_org_unit_ids(shared["conn"], shared["member"]) == set()


def test_read_does_add_the_department_to_the_rail(shared):
    """给了查看：左栏才出现这个部门（这是上一版就有的行为，钉住别退化）。"""
    shared["clear"]()
    shared["grant"](can_read=True)
    assert shared["owner"] in security.shared_org_unit_ids(shared["conn"], shared["member"])


def test_the_page_owner_sees_their_own_range_not_somebody_elses(shared):
    """本部门的页照旧写自己的可见范围——那句话没说错，别被这次改动牵连。"""
    shared["clear"]()
    shared["grant"](can_read=True)
    shared["as_"](OWNER)
    row = shared["row_html"]()
    assert "开给你的" not in row, "自己的页不该有「开给你的」"
    assert "仅本部门" in row, row


def test_a_page_nobody_opened_to_me_is_not_listed(shared):
    """一行授权都没有：这页根本不该出现在列表里（别把全公司的页都列一遍）。"""
    shared["clear"]()
    assert "设计部打样进度" not in shared["listing"]().text


def test_the_three_rights_share_one_query_per_page(shared):
    """三个 can_* 与界面取同一份结果：三格必须一致，不许各算各的。"""
    shared["clear"]()
    shared["grant"](can_read=True, can_write=False, can_base=True)
    conn, member, owner = shared["conn"], shared["member"], shared["owner"]
    rights = security.interface_rights(conn, member, owner, SLUG)
    assert rights["can_read"] == security.can_view_interface(conn, member, owner, SLUG)
    assert rights["can_write"] == security.can_fill_interface(conn, member, owner, SLUG)
    assert rights["can_base"] == security.can_base_interface(conn, member, owner, SLUG)


def test_a_page_that_does_not_exist_has_no_rights(shared):
    """页不存在：`interface_rights` 返回 None，三个 can_* 一律 False，不抛异常。"""
    conn, member, owner = shared["conn"], shared["member"], shared["owner"]
    assert security.interface_rights(conn, member, owner, "no-such-page") is None
    assert security.can_view_interface(conn, member, owner, "no-such-page") is False
    assert security.can_base_interface(conn, member, owner, "no-such-page") is False
