"""状态下拉回填：本部门自己的词要原样回来，退回标准态时绝不反推（`4141626`）。

口径是「纯 A」：`status` 列只存映射后的标准态（`done`），部门词（「已完成」「待取」）
另存一份在 `extra[字段 key]`。回填时优先取它；没有（本次改动之前记的条目）就退回
标准态——而标准态对不上任何选项，于是空着。

为什么不许从标准态反查部门词：一个标准态可能对上好几个部门词（`planned` 对应
「待取」「已取」），反推就会把「待取」显示成「已取」，而且错得很安静——页面看起来
填好了。所以第 2、3 条是**护栏**：它们不钉某次行为，钉的是「一年后有人把反查加回来」
这件事（那种改动会让这两条红，而第 1 条照样绿）。

夹具复用 `test_review_stages` 的 `room`：同一套种子世界、同一笔事务、同一条真实链路
（建页 → 提交 → 初审上线 → `/interfaces/{id}` 填一条）。
"""

from __future__ import annotations

import pytest

import oams.api
from oams import web  # 必须在 oams.api 之后导入
from test_review_preview import _a_live_page, _fill_entry
from test_review_stages import (  # noqa: F401  (fixture 由 pytest 收集)
    DESIGN_AUTHOR,
    DESIGN_REVIEWER,
    room,
)

pytestmark = pytest.mark.db

# DESCRIPTOR 里那格：字段 key `state_word`、选项是本部门的两个词、映射到标准态。
WORD_FIELDS = [{"key": "state_word", "map_to": "status", "options": ["进行中", "已完成"]}]
# 第 3 条要的形状：两个部门词映射同一个标准态——反查在这里必然猜错。
TWO_WORDS = [{"key": "state_word", "map_to": "status", "options": ["待取", "已取"]}]


def test_a_saved_word_comes_back_selected(room):
    """记一条「已完成」→ 再打开这一页，下拉里选中的是那个词本身。"""

    interface_id = _a_live_page(room)
    room["as_"](DESIGN_AUTHOR)
    _fill_entry(room, interface_id, {
        "title": "月度报销", "state_word": "已完成", "evidence": "FP-2026-11",
    })

    page = room["client"]().get(f"/interfaces/{interface_id}")
    assert page.status_code == 200, page.text
    assert '<option value="已完成" selected>' in page.text
    # 反面：不能两个词都标成选中，也不能选到另一个词
    assert '<option value="进行中" selected>' not in page.text

    # 库里两处同时成立：标准态仍是 done，部门词落在 extra 里——这条分支只加"另存一份"，
    # 没有把 status 列改成部门词（改了就破坏全局状态口径）。
    rows = room["conn"].execute(
        "SELECT status, extra FROM oams.interface_entries WHERE interface_id = %s",
        (interface_id,)).fetchall()
    assert len(rows) == 1, rows
    assert rows[0]["status"] == "done", rows[0]
    assert (rows[0]["extra"] or {}).get("state_word") == "已完成", rows[0]


def test_a_history_entry_without_the_word_selects_nothing():
    """本次改动之前记的条目（`extra` 里没有那个词）：空着，不猜。"""

    values = web._interface_values(WORD_FIELDS, {"status": "done", "extra": {}})

    # 退回标准态是原有行为（改前就这样，所以这条在 `4141626^` 上也是绿的）
    assert values["state_word"] == "done"
    # 关键的一半：标准态不许被翻成任何一个部门词。渲染只在完全相等时标 selected
    # （render.py 的 select 分支），所以这条成立即"没有错词被选中"。
    assert values["state_word"] not in ("进行中", "已完成")


def test_two_words_for_one_standard_state_are_not_guessed():
    """两个部门词映射同一个标准态：老条目上是空的，不许挑一个显示。

    这是防"加回反查"的护栏：反查实现一定会在这里挑中「待取」或「已取」中的一个。
    """

    values = web._interface_values(TWO_WORDS, {"status": "planned", "extra": {}})

    assert values["state_word"] == "planned"
    assert values["state_word"] not in ("待取", "已取")
