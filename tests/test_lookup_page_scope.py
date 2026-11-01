"""查件框的查询范围：整页（含更早那几版），但不跨页（`16f3e6b`、`f8ef72f`）。

改之前查件框按 `e.interface_id = <当前这一版>` 查，而表格、数字、状态统计用的都是整页
口径（`api.PAGE_ENTRY_SCOPE_SQL`）。重发一版之后同一屏上就自相矛盾：表格里列着这条
记录，上方的查件框说「本页没有这个单号」——读的人只会判断查询坏了，想不到是版本不同。

两条用例一并钉住「放大到整页」与「没有放大到别页」之间的那条线：

1. 条目只记在**旧版**上 → 在当前版页面上查得到，并且卡片上有一行小字说明这条记在
   更早那一版（字段是按当前这一版映射的，老条目没有的字段是空的，不说就等于丢数据）；
2. 一样的单号记在**另一页**上 → 本页查它仍然是「本页没有这个单号」。

第 2 条不是衬托：口径一旦写成「按 slug 模糊」「按部门全查」，它就会红。
"""

from __future__ import annotations

import pytest

import oams.api
from test_review_preview import _fill_entry
from test_review_stages import (  # noqa: F401  (fixture 由 pytest 收集)
    DESIGN_AUTHOR,
    DESIGN_REVIEWER,
    _approve,
    room,
)

pytestmark = pytest.mark.db

DESCRIPTOR = {
    "schema_version": "0.2",
    "interface_id": "lookup-scope-sample",
    "name": "查件范围样张",
    "org_unit_code": "DESIGN",
    "position_codes": ["DESIGN_LEAD"],
    "capabilities": ["entries:write", "entries:read:dept"],
    "modules": [{"key": "layout", "type": "rows", "children": [
        {"key": "parcel_lookup", "type": "lookup_box",
         "params": {"label": "单号", "key_field": "evidence"}},
        {"key": "detail", "type": "table", "params": {"rows": 1, "columns": [
            {"key": "title", "label": "收件人"},
            {"key": "parcel_no", "label": "单号"},
        ]}},
    ]}],
    "fields": [
        {"key": "title", "label": "收件人", "type": "text", "required": True,
         "map_to": "title", "aggregatable": True, "example": "3F-12 工位"},
        {"key": "parcel_no", "label": "单号", "type": "text", "required": True,
         "map_to": "evidence", "aggregatable": True, "example": "SF1234567890"},
        {"key": "state_word", "label": "状态", "type": "enum", "required": True,
         "map_to": "status", "aggregatable": True, "example": "待取",
         "options": ["待取", "已取"]},
    ],
    "status_map": {"待取": "planned", "已取": "done"},
    "rules": {"evidence_required": False},
    "meta": {"author_position_code": "DESIGN_LEAD", "version": 1,
             "visibility": "department", "review": {"status": "draft"}},
}


def _live(room, slug: str):
    """建一版、提审、部门初审通过——都走真实入口。"""

    interface_id = room["create"](room["design"], descriptor=DESCRIPTOR, slug=slug)
    room["submit"](room["design"], interface_id)
    room["as_"](DESIGN_REVIEWER)
    _approve(room, interface_id, note="初审过，这一版上线")
    room["as_"](DESIGN_AUTHOR)
    return interface_id


def _lookup(room, interface_id, query: str):
    response = room["client"]().get(
        f"/departments/{room['design']}/{interface_id}/lookup",
        params={"lookup": query}, headers={"HX-Request": "true"})
    assert response.status_code == 200, response.text
    return response.text


def test_an_entry_on_an_older_version_is_still_found_from_the_current_one(room):
    """重发一版之后，只记在旧版上的那条仍然查得到，卡片上说明它记在更早那一版。"""

    slug = "lookup-scope-carried"
    v1 = _live(room, slug)
    _fill_entry(room, v1, {
        "title": "3F-12 工位", "parcel_no": "SF-OLD-001", "state_word": "待取",
    })

    # 同一 slug 再建一版再上线：version_no 由产品自己算，v1 随后被归档。
    # 两版都走完提交+初审——「旧条目在整页口径里还看得见」说的就是重发之后这一刻。
    v2 = room["create"](room["design"], descriptor=DESCRIPTOR, slug=slug)
    room["submit"](room["design"], v2)
    room["as_"](DESIGN_REVIEWER)
    _approve(room, v2, note="重发这一版，旧条目应当跟着页面走")
    room["as_"](DESIGN_AUTHOR)
    rows = room["conn"].execute(
        """SELECT version_no, status FROM oams.interfaces
            WHERE org_unit_id = %s AND slug = %s ORDER BY version_no""",
        (room["design"], slug)).fetchall()
    assert [(r["version_no"], r["status"]) for r in rows] == [(1, "archived"), (2, "active")], rows

    fragment = _lookup(room, v2, "SF-OLD-001")

    assert "SF-OLD-001" in fragment
    assert "这条记在更早的那一版上" in fragment
    assert "本页没有这个单号" not in fragment


def test_the_lookup_does_not_reach_into_another_page(room):
    """同样的单号记在另一页上：本页查它仍然是「本页没有这个单号」。"""

    mine = _live(room, "lookup-scope-mine")
    _fill_entry(room, mine, {
        "title": "我自己这页", "parcel_no": "SF-MINE-001", "state_word": "待取",
    })
    other = _live(room, "lookup-scope-other")
    _fill_entry(room, other, {
        "title": "隔壁那页", "parcel_no": "SF-OTHER-999", "state_word": "待取",
    })

    own_hit = _lookup(room, mine, "SF-MINE-001")
    assert "SF-MINE-001" in own_hit  # 正向对照：口径不是全都不查

    fragment = _lookup(room, mine, "SF-OTHER-999")
    assert "本页没有这个单号" in fragment
    assert "SF-OTHER-999" not in fragment
