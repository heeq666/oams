"""坏掉的页面必须说自己是坏的，不能伪装成「你没有权限」。

`_denied` 对 GET 一律 303 跳落地页、且不带原因——这对**权限**是有意的（菜单里本来就没有那些链接，
能摸到这里只有旧书签或手输 URL，多一句解释只是噪音）。但它不该接渲染失败：
0.1 的描述被 0.2 渲染器拒掉时，人看到的是「被送回进度中心」，于是去申请一个帮不上忙的授权，
而真正该修的是这一页的描述。R1 查了一整天没人往这个方向想，就是因为它长得和没权限一模一样。

所以这组用例把两件事钉死：
* 渲染失败 → 500 + 原文 + schema 版本 + 页面是谁的，并且记一条服务端日志；
* 权限不够 → 仍然静默跳走，一个字都不多说；
* 能渲染的页面 → 照旧 200，别被顺手改坏。
"""

from __future__ import annotations

import contextlib
import json
import logging

import pytest
from starlette.testclient import TestClient

import oams.api
from oams import web

pytestmark = pytest.mark.db

# 设计部里那个有 reports/read + submit 的人（见 tests/conftest.py 的 SEED_SQL）。
DESIGN_LEAD = "DESIGN001"
GOOD_PAGE = "design-sampling"

# 一份 0.1 的描述：item_card 在 0.2 的白名单里没有对应，渲染器会直接拒绝。
LEGACY_DESCRIPTOR = {
    "schema_version": "0.1",
    "interface_id": "design-legacy-broken",
    "name": "设计部旧版打样页",
    "org_unit_code": "DESIGN",
    "position_codes": ["DESIGN_LEAD"],
    "modules": [
        {"key": "item", "type": "item_card", "title": "事项", "order": 1,
         "source_fields": ["title"], "aggregatable": True},
    ],
    "fields": [
        {"key": "title", "label": "款号", "type": "text", "required": True,
         "map_to": "title", "aggregatable": True},
    ],
    "status_map": {},
}


@contextlib.contextmanager
def _one_transaction(conn):
    """把路由里的 `with connection() as c` 换成「用用例这一笔事务」。"""
    yield conn


@pytest.fixture()
def served_as(seeded_database, monkeypatch):
    """一个连到种子库、有设计部权限的人；用例结束整笔回滚，库里不留行。"""

    import psycopg
    from psycopg.rows import dict_row

    conn = psycopg.connect(seeded_database.url, row_factory=dict_row)
    member_id = seeded_database.member_id(DESIGN_LEAD)
    principal = {"member_id": member_id, "employee_code": DESIGN_LEAD,
                 "display_name": "设计负责人", "employment_status": "active"}
    # 路由与中间件都走 `connection()`；换成这笔事务，权限与页面才看的是同一个库。
    monkeypatch.setattr(web, "connection", lambda: _one_transaction(conn))
    monkeypatch.setattr(oams.api, "connection", lambda: _one_transaction(conn))
    monkeypatch.setattr(oams.api, "principal_from_request", lambda request: principal)
    try:
        yield {"conn": conn, "member_id": member_id,
               "org_unit_id": seeded_database.org_unit_id("DESIGN"),
               "good": seeded_database.scalar(
                   "SELECT interface_id FROM oams.interfaces WHERE slug = %s AND status = 'active'",
                   (GOOD_PAGE,))}
    finally:
        conn.rollback()
        conn.close()


def _broken_page(served_as) -> dict:
    """种一份渲染不出来的旧页面（0.1 + item_card），不提交。"""

    conn = served_as["conn"]
    row = conn.execute(
        """INSERT INTO oams.interfaces
               (org_unit_id, slug, name, status, visibility, schema_version, descriptor,
                position_id, version_no, created_by, updated_by)
           VALUES (%s, %s, %s, 'active', 'department', '0.1', %s,
                   NULL, 1, %s, %s)
           RETURNING interface_id""",
        (served_as["org_unit_id"], LEGACY_DESCRIPTOR["interface_id"], LEGACY_DESCRIPTOR["name"],
         json.dumps(LEGACY_DESCRIPTOR), served_as["member_id"], served_as["member_id"]),
    ).fetchone()
    return {"interface_id": row["interface_id"], "org_unit_id": served_as["org_unit_id"]}


def test_a_page_the_renderer_cannot_draw_says_so_instead_of_redirecting(served_as, caplog):
    page = _broken_page(served_as)
    with caplog.at_level(logging.ERROR, logger="oams.render"):
        response = TestClient(oams.api.app).get(f"/interfaces/{page['interface_id']}")

    # 500 而不是一张「被踢回去」的 303：这一页是坏的，不是这个人不够格
    assert response.status_code == 500
    # 原文在场，能直接拿去修：哪个模块、哪个 0.1 的类型被拒
    assert "MODULE_NOT_WHITELISTED" in response.text and "item_card" in response.text
    # 这页是哪个 schema 版本的，以及它叫什么
    assert "0.1" in response.text and LEGACY_DESCRIPTOR["name"] in response.text
    # 一句话把「坏」和「没权限」分开，别再让人去申请授权
    assert "不是你没有权限" in response.text
    # 同一段事实进了服务端日志，不用复述
    assert any("render failed" in record.getMessage() for record in caplog.records)


def test_the_department_workbench_reports_the_same_break(served_as):
    page = _broken_page(served_as)
    response = TestClient(oams.api.app).get(
        f"/departments/{page['org_unit_id']}/{page['interface_id']}")

    # 两个入口同一件事：都是 500 + 原因，没有一个是静默 303
    assert response.status_code == 500
    assert "MODULE_NOT_WHITELISTED" in response.text


def test_a_page_that_renders_is_untouched(served_as):
    if served_as["good"] is None:  # pragma: no cover - 种子不对时给一句人话
        pytest.skip(f"这个库没有 {GOOD_PAGE}")

    response = TestClient(oams.api.app).get(f"/interfaces/{served_as['good']}")

    assert response.status_code == 200
    assert "MODULE_NOT_WHITELISTED" not in response.text


def test_a_refused_permission_still_goes_quietly_to_the_landing_page(served_as, monkeypatch):
    """权限失败保持原样：GET 静默跳走。改渲染失败的时候别顺手把这个也改了。"""

    if served_as["good"] is None:  # pragma: no cover
        pytest.skip(f"这个库没有 {GOOD_PAGE}")

    def refuse(request, resource, action):
        raise web._NotPermitted()

    monkeypatch.setattr(web, "_require", refuse)
    response = TestClient(oams.api.app).get(f"/interfaces/{served_as['good']}",
                                               follow_redirects=False)

    assert response.status_code == 303
