"""别人共享给我的页，页头那句「谁开的」：说部门不说人，主人自己看不到。

口径（负责人 2026-11-04 定）：权限是发给部门的，人只是当时点的那只手，所以这句里
只出现部门名，不出现姓名；只给查看就说「查看」，也给了编辑就说「查看和编辑」；公司根
那一行说「全公司可见」；点名部门与公司根同时命中时只提更具体的那个部门；本部门的人
（这一页的主人）即使另外还拿到别的部门授权，也不显示这句。

句子在服务端拼（`web._access_note`），模板只做纯文本输出；判据来自
`security.interface_access_via`——它读的就是 `_page_access` 判可见性的那张表，不另写
SQL。所以这里的用例既钉文案，也钉「本部门不出现」这条边界。

数据一律自己造、整笔事务结束时回滚。
"""

from __future__ import annotations

import contextlib

import psycopg
import pytest
from psycopg.rows import dict_row
from starlette.testclient import TestClient

import oams.api
from oams import web
from oams.config import settings

pytestmark = pytest.mark.db

ADMIN = "ADMIN001"         # 系统管理员：不需要授权就能进任何部门的页
OWNER = "DESIGN001"        # 设计负责人：这张页的主人
GUEST = "MVP1001"          # 人事经理：种子里没有这张页的任何授权，靠下面造的行走进来

SLUG = "design-sampling"   # 种子里设计部那张已经上线的页
OPENED_VIEW = "人事部开给你查看"
OPENED_EDIT = "人事部开给你查看和编辑"
EVERYONE = "全公司可见"


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

    state = {"me": GUEST, "cookie": "access-note-strip-test"}

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

    path = f"/departments/{page['org_unit_id']}/{page['interface_id']}"

    def open_page():
        return client().get(path, follow_redirects=False)

    def grant(code, can_write=False):
        """把这一页开给某个部门——这就是页面设置弹窗保存时写的那一行。"""
        conn.execute(
            """INSERT INTO oams.interface_access
                   (owner_org_unit_id, slug, org_unit_id, can_read, can_write,
                    granted_by, note)
               VALUES (%s, %s, %s, true, %s, %s, 'access-note-strip')
               ON CONFLICT (owner_org_unit_id, slug, org_unit_id)
               DO UPDATE SET can_read = true, can_write = EXCLUDED.can_write""",
            (page["org_unit_id"], SLUG, unit(code), can_write, people[ADMIN]))

    def clear():
        conn.execute("""DELETE FROM oams.interface_access
                         WHERE owner_org_unit_id = %s AND slug = %s""",
                     (page["org_unit_id"], SLUG))

    made = {"conn": conn, "as_": as_, "open_page": open_page, "grant": grant,
            "clear": clear, "unit": unit, "owner": page["org_unit_id"],
            "interface_id": page["interface_id"]}
    try:
        yield made
    finally:
        conn.rollback()
        conn.close()


def test_the_strip_names_the_department_that_opened_the_page(shared):
    """只给查看：说「<部门>开给你查看」，不提姓名。"""
    shared["clear"]()
    shared["grant"]("HR")
    response = shared["open_page"]()
    assert response.status_code == 200, response.text
    assert OPENED_VIEW in response.text
    assert OPENED_EDIT not in response.text
    assert EVERYONE not in response.text
    # 说部门不说人：授权人（ADMIN001）的名字不该出现在这句里。
    assert "系统管理员开给你" not in response.text


def test_the_strip_says_editing_when_that_was_granted(shared):
    """也给了编辑：一句说完，不拆成两句。"""
    shared["clear"]()
    shared["grant"]("HR", can_write=True)
    response = shared["open_page"]()
    assert response.status_code == 200, response.text
    assert OPENED_EDIT in response.text


def test_the_company_root_reads_as_everyone(shared):
    """公司根那一行是「全公司可见」，不点某个部门的名。"""
    shared["clear"]()
    shared["grant"]("COMPANY")
    response = shared["open_page"]()
    assert response.status_code == 200, response.text
    assert EVERYONE in response.text
    assert "开给你查看" not in response.text


def test_the_named_department_wins_over_the_company_root(shared):
    """两行同时命中时说更具体的那个部门，别上下叠两句。"""
    shared["clear"]()
    shared["grant"]("HR")
    shared["grant"]("COMPANY")
    response = shared["open_page"]()
    assert response.status_code == 200, response.text
    assert OPENED_VIEW in response.text
    assert EVERYONE not in response.text


def test_the_owning_department_sees_no_strip(shared):
    """主人不是客人：本部门的人打开自己那页，看不到这句。"""
    shared["clear"]()
    shared["grant"]("HR")
    shared["as_"](OWNER)
    response = shared["open_page"]()
    assert response.status_code == 200, response.text
    assert "开给你查看" not in response.text
    assert EVERYONE not in response.text


def test_a_system_admin_without_a_grant_sees_no_strip(shared):
    """系统管理员是靠身份进来的，不是靠某一行授权——没有行就没有这句。"""
    shared["clear"]()
    shared["as_"](ADMIN)
    response = shared["open_page"]()
    assert response.status_code == 200, response.text
    assert "开给你查看" not in response.text
    assert EVERYONE not in response.text
