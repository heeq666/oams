"""查件结果卡的状态词：与表格那一列同一个词（`55a2d9b`）。

同一条记录在同一屏上被叫两个名字——表格那列写部门词「待取」，查件卡写标准态的显示名
「未开始」，读的人会以为状态被人改过。修法不是给卡片另写一套规则，而是复用同一条
优先级：先取 `extra[<字段 key>]` 里留着的部门词，取不到才退回标准态显示名。

所以判据有两层：
* 端到端：真填一条「待取」，再用查件框查它 → 片段里是「待取」，整块里不许出现「未开始」；
* 同一口径：卡片给的词，必须等于填表页回填下拉框时算出来的那个词（`_interface_values`），
  两处各写一套的话这条就红。
* 老条目（`extra` 里没留词）：退回标准态显示名，不猜——这条防的是把反查加回来。
"""

from __future__ import annotations

import pytest

import oams.api
from oams import web  # 必须在 oams.api 之后导入
from test_review_preview import _a_live_page, _fill_entry
from test_review_stages import (  # noqa: F401  (fixture 由 pytest 收集)
    DESIGN_AUTHOR,
    room,
)

pytestmark = pytest.mark.db

DESCRIPTOR = {
    "schema_version": "0.2",
    "interface_id": "lookup-word-sample",
    "name": "查件样张",
    "org_unit_code": "DESIGN",
    "position_codes": ["DESIGN_LEAD"],
    "capabilities": ["entries:write", "entries:read:dept"],
    "modules": [{"key": "layout", "type": "rows", "children": [
        {"key": "parcel_lookup", "type": "lookup_box",
         "params": {"label": "单号", "key_field": "evidence"}},
        {"key": "detail", "type": "table", "params": {"rows": 1, "columns": [
            {"key": "title", "label": "收件人"},
            {"key": "state_word", "label": "状态"},
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


def _the_entry(room, interface_id) -> dict:
    rows = room["conn"].execute(
        "SELECT * FROM oams.interface_entries WHERE interface_id = %s", (interface_id,)
    ).fetchall()
    assert len(rows) == 1, rows
    return rows[0]


def test_the_card_and_the_table_call_the_same_record_by_the_same_word(room):
    """一条「待取」的记录：查件卡里也是「待取」，不是标准态的「未开始」。"""

    interface_id = _a_live_page(room, descriptor=DESCRIPTOR)
    room["as_"](DESIGN_AUTHOR)
    _fill_entry(room, interface_id, {
        "title": "3F-12 工位", "parcel_no": "SF1234567890", "state_word": "待取",
    })
    entry = _the_entry(room, interface_id)

    # 前提：部门词真的落在 extra 里（落不上，下面两条就都成了空跑）
    assert (entry["extra"] or {}).get("state_word") == "待取", entry["extra"]
    assert entry["status"] == "planned", entry["status"]

    fragment = room["client"]().get(
        f"/departments/{room['design']}/{interface_id}/lookup",
        params={"lookup": "SF1234567890"},
        headers={"HX-Request": "true"})
    assert fragment.status_code == 200, fragment.text

    assert "待取" in fragment.text
    assert "未开始" not in fragment.text  # 标准态显示名不该再出现

    # 同一口径：卡片这个词，必须就是填表页回填下拉框时算出来的那个词
    assert web._interface_values(DESCRIPTOR["fields"], entry)["state_word"] == "待取"
    assert web._entry_status_word(DESCRIPTOR, entry) == "待取"


def test_an_old_entry_without_the_word_falls_back_to_the_standard_label():
    """本次改动之前记的条目（`extra` 里没有部门词）：退回标准态显示名，不猜。"""

    labels = {"status": "planned", "extra": {}}
    assert web._entry_status_word(DESCRIPTOR, labels) == "未开始"
    # 护栏：两个部门词都可能对应 planned，反查一定会挑错一个
    assert web._entry_status_word(DESCRIPTOR, labels) not in ("待取", "已取")
