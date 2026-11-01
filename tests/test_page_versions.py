"""一页一个地址：重发换的是版本行，不是页面身份。

`interface_id` 只是某一版的行号，页面的身份是 `(org_code, slug)`——每重发一次就多一行，
把链接建在 id 上，别人一改页链接就烂。所以三条约定要同时成立：

* `/pages/{org_code}/{slug}` 永远指向**当前**版本（不随重发变化的那条地址）；
* 拿着旧版本的 id 来**读**，跳到现在那一版，而不是给一个死胡同；
* 拿着旧版本的 id 来**写**（填报 / 提交审核 / 撤回 / 关联工作项），409 并说清现在那版在哪；
  而当前版本照旧 303 `?saved=1`——闸门不能做成「一律拒写」。

造态全部走真实入口：建页 → 提交审核 → 初审通过上线；同 slug 再发第二版让它归档。
"""

from __future__ import annotations

import hashlib
import hmac
import re

import pytest

from test_review_preview import METRIC_DESCRIPTOR, _a_live_page, _fill_entry
from test_review_stages import DESIGN_AUTHOR, DESIGN_REVIEWER, _approve, room

pytestmark = pytest.mark.db

# room fixture 里 TestClient 带的会话串，表单令牌就是拿它算的（web._form_token）。
ROOM_COOKIE = "review-stage-test"

ARCHIVED_REASON = "这一版已归档，不能再往里写"


def _form_csrf(purpose: str) -> str:
    return hmac.new(ROOM_COOKIE.encode(), purpose.encode(), hashlib.sha256).hexdigest()


def _slug_of(room, interface_id) -> str:
    return room["conn"].execute(
        "SELECT slug FROM oams.interfaces WHERE interface_id = %s",
        (interface_id,)).fetchone()["slug"]


def _status_of(room, interface_id) -> str:
    return room["conn"].execute(
        "SELECT status FROM oams.interfaces WHERE interface_id = %s",
        (interface_id,)).fetchone()["status"]


def _entry_total(room) -> int:
    return room["conn"].execute(
        "SELECT count(*) AS n FROM oams.interface_entries").fetchone()["n"]


def _republish(room, *, descriptor=None, fill=None) -> tuple:
    """v1 上线 →（可选）在 v1 上填一条 → 同一 slug 再发 v2 并过审 → v1 归档。

    返回 (v1, v2, slug, entry_id)。条目必须在 v1 还是当前版本时填——归档之后再写
    是另一条用例要钉的拒绝路径，不能拿它当造态手段。
    """
    first = _a_live_page(room, descriptor=descriptor)
    slug = _slug_of(room, first)
    entry_id = None
    if fill is not None:
        room["as_"](DESIGN_AUTHOR)
        _fill_entry(room, first, fill)
        entry_id = room["conn"].execute(
            "SELECT entry_id FROM oams.interface_entries WHERE interface_id = %s",
            (first,)).fetchone()["entry_id"]
    room["as_"](DESIGN_AUTHOR)
    second = room["create"](room["design"], slug=slug, name="审核分级样张（第二版）",
                            descriptor=descriptor)
    room["submit"](room["design"], second)
    room["as_"](DESIGN_REVIEWER)
    passed = _approve(room, second, note="第二版上线")
    assert passed.status_code == 303, passed.text
    # 真出现两行：一版一行，旧的那行归档——不是把同一行改来改去。
    assert _status_of(room, first) == "archived"
    assert _status_of(room, second) == "active"
    return first, second, slug, entry_id


# ---------------------------------------------------------------- 稳定地址


def test_the_address_by_slug_lands_on_the_current_version(room):
    first, second, slug, _ = _republish(room)
    room["as_"](DESIGN_AUTHOR)

    opened = room["client"]().get(f"/pages/DESIGN/{slug}", follow_redirects=False)

    assert opened.status_code == 303, opened.text
    assert opened.headers["location"] == f"/departments/{room['design']}/{second}"


def test_an_unknown_slug_is_sent_away_rather_than_drawn(room):
    """不存在的 slug 走既有的 `_denied`（GET 时带人回第一个能看的屏），不是 200。"""
    room["as_"](DESIGN_AUTHOR)

    opened = room["client"]().get("/pages/DESIGN/no-such-page", follow_redirects=False)

    assert opened.status_code == 303, opened.text
    assert str(room["design"]) not in opened.headers["location"]
    assert opened.headers["location"].startswith("/")


def test_reading_an_archived_version_lands_on_the_current_one(room):
    """旧链接不是死胡同，但也不许「静默换个版本给你看」——跳转带 `?archived=1` 说明原因。

    注意：现在只到「带标记跳走」这一步。`archived_notice` 已经传进模板、
    模板还没渲染那句提示（web.py:948 有值、模板无引用），所以这里不断言提示句；
    模板接上后要在这里补一条断言，否则这个功能在页面上是看不见的。
    """
    first, second, _, _ = _republish(room)
    room["as_"](DESIGN_AUTHOR)

    opened = room["client"]().get(f"/departments/{room['design']}/{first}",
                                 follow_redirects=False)

    assert opened.status_code == 303, opened.text
    assert opened.headers["location"] == f"/departments/{room['design']}/{second}?archived=1"
    # 跟过去是当前版本、照常能看。
    landed = room["client"]().get(opened.headers["location"])
    assert landed.status_code == 200, landed.text[:300]
    assert f"/departments/{room['design']}/{second}" in landed.text


# ---------------------------------------------------------------- 归档闸


def _write_routes(room, version, entry_id) -> dict:
    """四条写入口，键是报告里的称呼。每条都带自己的令牌（purpose 各不相同）。"""
    org = room["design"]
    base = f"/departments/{org}/{version}"
    return {
        "填报": (base, {"csrf": _form_csrf("oams:interface-form:v1")}),
        "提交审核": (base + "/submit-review",
                 {"review_csrf": _form_csrf("oams:page-form:v1")}),
        "撤回审核": (base + "/withdraw-review",
                 {"review_csrf": _form_csrf("oams:page-form:v1")}),
        "关联工作项": (base + f"/entries/{entry_id}/link",
                  {"csrf": _form_csrf("oams:entry-link:v1"), "work_item_id": ""}),
    }


def test_writing_to_an_archived_version_is_refused(room):
    """旧版本的 id 还在别人书签里，所以四条写入口都得挡住，并且不留半笔写入。"""
    first, second, _, entry = _republish(
        room, fill={"title": "AW26-777 旧版那一条", "state_word": "进行中",
                    "evidence": "SY-2777"})
    room["as_"](DESIGN_AUTHOR)
    assert entry is not None
    before = _entry_total(room)

    for label, (path, data) in _write_routes(room, first, entry).items():
        refused = room["client"]().post(path, data=data, follow_redirects=False)
        assert refused.status_code == 409, (label, refused.status_code, refused.text[:300])
        assert ARCHIVED_REASON in refused.text, label
        # 告诉人「现在那版在哪」，否则 409 只是关上一扇门。
        assert f"/departments/{room['design']}/{second}" in refused.text, label
        assert _entry_total(room) == before, label
        assert _status_of(room, first) == "archived", label
        assert _status_of(room, second) == "active", label


def _design_work_item(room):
    """DESIGN 部的一条工作项，供关联用。

    产品入口 `/admin/work-items` 要 `workforce.work_items/manage`，而种子里 DESIGN 的
    两个岗位都只有 read（人事经理才有 manage，跨部门建不了）。这里按夹具手段插一条，
    行形与那个入口建出来的完全一致；造的是「事项」，不是被测的页面状态。
    """
    return room["conn"].execute(
        """INSERT INTO oams.work_items (org_unit_id, position_id, title, kind, status)
           SELECT p.org_unit_id, p.position_id, '报工关联用的事项（夹具）', 'project', 'in_progress'
             FROM oams.positions p WHERE p.position_code = 'DESIGNER'
           RETURNING work_item_id""").fetchone()["work_item_id"]


def test_the_current_version_still_links_an_old_entry(room):
    """契约 6.4：旧版本上记的条目在当前版本里照旧算数，包括「关联工作项」这条写。

    闸门修好时曾顺手把条目查询也收成「本版才有」，结果旧条目在当前页面上一点就报
    「找不到这条汇报记录」——聚合对了、关联断了。这条钉住两边都要成立。
    """
    first, second, _, entry = _republish(
        room, fill={"title": "AW26-779 旧版那一条", "state_word": "进行中",
                    "evidence": "SY-2779"})
    room["as_"](DESIGN_AUTHOR)
    assert entry is not None
    item = _design_work_item(room)

    linked = room["client"]().post(
        f"/departments/{room['design']}/{second}/entries/{entry}/link",
        data={"csrf": _form_csrf("oams:entry-link:v1"), "work_item_id": str(item)},
        follow_redirects=False)

    assert linked.status_code == 303, linked.text
    assert linked.headers["location"].endswith("?saved=linked"), linked.headers["location"]
    stored = room["conn"].execute(
        "SELECT work_item_id, interface_id FROM oams.interface_entries WHERE entry_id = %s",
        (entry,)).fetchone()
    assert stored["work_item_id"] == item
    # 关联不搬家：条目仍记在它当初提交的那一版上，不是被挪到新版本。
    assert stored["interface_id"] == first


def test_the_current_version_still_takes_a_write(room):
    """闸门只挡旧版本：当前版本照旧 303 `?saved=1`。"""
    _, second, _, _ = _republish(room)
    room["as_"](DESIGN_AUTHOR)
    before = _entry_total(room)

    saved = room["client"]().post(
        f"/departments/{room['design']}/{second}",
        data={"csrf": _form_csrf("oams:interface-form:v1"),
              "title": "AW26-778 新版这一条", "state_word": "进行中",
              "evidence": "SY-2778"},
        follow_redirects=False)

    assert saved.status_code == 303, saved.text
    assert saved.headers["location"] == f"/departments/{room['design']}/{second}?saved=1"
    assert _entry_total(room) == before + 1


# ---------------------------------------------------------------- 旧版本的条目在页面上


def test_the_old_versions_entry_still_shows_up_on_the_workbench(room):
    """契约 6.4 的界面侧：旧版本上记的那条，在当前版本的工作台列表里，也算进页面汇总。

    聚合对了不等于人看得见——工作台是新版本在渲染、列表却要列出旧版本的条目，
    汇总也要把两版加在一起。这条钉住「列表 + 汇总」两处都按页面算，不是按版本行算。
    """
    first, second, _, _ = _republish(
        room, descriptor=METRIC_DESCRIPTOR,
        fill={"title": "AW26-781 旧版那一条", "state_word": "进行中",
              "done_count": "5", "evidence": "SY-2781"})
    room["as_"](DESIGN_AUTHOR)
    # 当前版本再记一条：汇总必须是两版之和，只算当前版本会得到 7，只算旧版本会得到 5。
    _fill_entry(room, second, {"title": "AW26-782 新版这一条", "state_word": "进行中",
                               "done_count": "7", "evidence": "SY-2782"})

    shown = room["client"]().get(f"/departments/{room['design']}/{second}")
    assert shown.status_code == 200, shown.text[:300]
    html = shown.text

    listing = html.split('<ul class="items">', 1)[1].split("</ul>", 1)[0]
    assert "AW26-781 旧版那一条" in listing, "旧版本的条目没出现在当前版本的工作台列表里"
    assert "AW26-782 新版这一条" in listing

    # 页面汇总读页面级聚合（dept.DESIGN.done_count，sum）：5 + 7 = 12。
    assert re.search(r'<span class="wh-metric-value">12</span>', html), "汇总只算了当前版本"
    # 部门汇总那一栏同样要把它算进去：两条都计入本部门。
    assert "本部门计入 2 条" in html
