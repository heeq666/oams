"""明细表只预填第 1 行（`171ced5`）。

表格模块按 `rows` 连画 1–5 行「填新记录」的空行，它们共用同一份 values。改之前每次
打开页面 5 行都带着同一条记录（`#wh-f-title` 到 `#wh-f-title-5` 一字不差），读的人
以为已经存了 5 条，再点提交又多一条。

判据只用渲染出来的 HTML：一行里 `<input>` 的 `value` 属性就是「这格有值」，`<option
… selected>` 就是「这格选中的词」。不跑浏览器——这是服务端渲染的事，DOM 字符串就是
用户拿到的东西。
"""

from __future__ import annotations

import re

import pytest

import oams.api
from test_review_preview import _a_live_page, _fill_entry
from test_review_stages import (  # noqa: F401  (fixture 由 pytest 收集)
    DESIGN_AUTHOR,
    room,
)

pytestmark = pytest.mark.db

DESCRIPTOR = {
    "schema_version": "0.2",
    "interface_id": "table-prefill-sample",
    "name": "明细表预填样张",
    "org_unit_code": "DESIGN",
    "position_codes": ["DESIGN_LEAD"],
    "capabilities": ["entries:write", "entries:read:dept"],
    "modules": [{"key": "layout", "type": "rows",
                 "children": [{"key": "detail", "type": "table",
                               "params": {"rows": 5, "columns": [
                                   {"key": "title", "label": "科目"},
                                   {"key": "state_word", "label": "状态"},
                                   {"key": "voucher", "label": "凭证号"},
                               ]}}]}],
    "fields": [
        {"key": "title", "label": "科目", "type": "text", "required": True,
         "map_to": "title", "aggregatable": True, "example": "差旅报销"},
        {"key": "state_word", "label": "状态", "type": "enum", "required": True,
         "map_to": "status", "aggregatable": True, "example": "进行中",
         "options": ["进行中", "已完成"]},
        {"key": "voucher", "label": "凭证号", "type": "text", "required": True,
         "map_to": "evidence", "aggregatable": True, "example": "FP-20261102-014"},
    ],
    "status_map": {"进行中": "in_progress", "已完成": "done"},
    "rules": {"evidence_required": False},
    "meta": {"author_position_code": "DESIGN_LEAD", "version": 1,
             "visibility": "department", "review": {"status": "draft"}},
}


def _rows(html: str) -> list[str]:
    """把表格模块里的 5 个 <tr> 切出来（表头那一行是 <th>，不算数据行）。"""

    section = re.search(r"<section[^>]*wh-table-controls.*?</section>", html, re.S)
    assert section, html[:600]
    return [row for row in re.findall(r"<tr[^>]*>(.*?)</tr>", section.group(0), re.S)
            if "<td" in row]


def _filled_values(row: str) -> list[str]:
    """这一行的控件里真正带值的有几个（`value` 属性、非空）。"""

    return [m.group(1) for m in
            (re.search(r'value="([^"]*)"', tag) for tag in re.findall(r"<input[^>]*>", row))
            if m and m.group(1)]


def _selected_words(row: str) -> list[str]:
    return re.findall(r'<option value="([^"]*)" selected>', row)


def test_only_the_first_row_carries_the_last_entry(room):
    """记住的只是「上一行」：第 1 行有值，第 2–5 行整格空着（占位符不算值）。"""

    interface_id = _a_live_page(room, descriptor=DESCRIPTOR)
    room["as_"](DESIGN_AUTHOR)
    _fill_entry(room, interface_id, {
        "title": "差旅报销", "state_word": "已完成", "voucher": "FP-20261102-014",
    })

    page = room["client"]().get(f"/interfaces/{interface_id}")
    assert page.status_code == 200, page.text
    rows = _rows(page.text)
    assert len(rows) == 5, len(rows)

    first, rest = rows[0], rows[1:]
    assert "差旅报销" in _filled_values(first)
    assert "FP-20261102-014" in _filled_values(first)
    assert _selected_words(first) == ["已完成"]

    for index, row in enumerate(rest, start=2):
        assert _filled_values(row) == [], f"第 {index} 行还被预填了：{_filled_values(row)}"
        # 下拉也一样：除第 1 行外都停在「请选择」（value="" 的那个默认项）
        assert _selected_words(row) == [], f"第 {index} 行的下拉被选中了：{_selected_words(row)}"
        # 空白对照：占位符在场，但那是 placeholder 不是 value
        assert 'placeholder="FP-20261102-014"' in row

    # 一行钉死整件事：这条记录的值在整页只出现一次（改之前是 5 次，一行一份）
    assert page.text.count('value="差旅报销"') == 1
