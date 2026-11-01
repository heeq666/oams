"""待审页预览：只吃 descriptor，不给真实条目、不写库、不许被缓存。

审核人签的是「这一版定义能不能上线」，所以预览画的是定义本身：字段、密度、空态。
这一层最容易出三种错，三种都写成用例：

1. **看错人**：预览是审核动作的延伸，权限必须与 `/reviews` 同一套口径——部门初审
   只看自己部门、公司终审归平台、谁都不能预览自己提交的那一版。预览要是宽一点，
   就等于给了一个绕过审核队列读别部门页面的口子。
2. **看到真数据**：`render_preview(sample=True)` 的样本值全部来自 descriptor
   （`example`、状态词、本页 emit 指标的中性 128），一个真实条目都不许出现。
   这条光断「页面里有样本值」不够——那在把真实条目也画上去时同样成立，所以还要
   正向对照：同一个页面在 `/interfaces/{id}` 那条「填真实数据」的入口上确实带着
   那条真实条目。没有这个对照，断言只是没看见泄漏，不是看见了「不泄漏」。
3. **顺手改库**：一个 GET 预览不是一次编辑。预览前后接口、条目、审计三张表的行数
   必须一模一样。

夹具复用 `test_review_stages` 的 `room`：同一套种子世界、同一笔事务、同一批真实
入口（建页 → 提交审核 → 初审）。两套世界会长出两套事实，预览的权限口径尤其经不
起这种漂移。
"""

from __future__ import annotations

import json
import logging
import re
import uuid

import pytest

import oams.api
from test_review_stages import (  # noqa: F401  (fixture 由 pytest 收集)
    DESIGN_AUTHOR,
    DESIGN_REVIEWER,
    HR_AUTHOR,
    PLATFORM,
    _approve,
    room,
)

pytestmark = pytest.mark.db

#: 种子里的活页面：作者 DESIGN001、终审由 ADMIN001 签、带一条真实条目 `AW24-001`。
LIVE_SLUG = "design-sampling"
#: descriptor 的 `example` 与真实条目同值（种子两处都写 AW24-001），所以「样本款号」
#: 冒充得了真实数据。canary 反过来挑一个**只存在于条目、descriptor 里没有**的值：
#: 真实条目的 evidence 是「样衣确认单 SY-2410」，descriptor 的 example 只有「SY-2410」。
LIVE_ENTRY_CANARY = "样衣确认单 SY-2410"
#: `_SAMPLE_BY_FIELD["title"]`，预览画出来的样本款号。两个值必须长得不一样，
#: 否则「样本值在场」和「真实值不在场」会互相冒充。
SAMPLE_TITLE = "AW26-001"

# 一个页面声明的指标：预览要画出 128 —— 这个数字既不来自数据库，也不来自条目，
# 而是 `_sample_aggregates` 给本页自产指标的中性样本值。
METRIC_DESCRIPTOR = json.loads(json.dumps({
    "schema_version": "0.2",
    "interface_id": "preview-metric-sample",
    "name": "预览指标样张",
    "org_unit_code": "DESIGN",
    "position_codes": ["DESIGN_LEAD"],
    "capabilities": ["entries:write", "entries:read:dept"],
    "modules": [{"key": "card", "type": "metric_card",
                 "params": {"metric_key": "dept.DESIGN.done_count"}}],
    "fields": [
        {"key": "title", "label": "款号", "type": "text", "required": True,
         "map_to": "title", "aggregatable": True, "example": "AW24-001"},
        {"key": "state_word", "label": "状态", "type": "enum", "required": True,
         "map_to": "status", "aggregatable": True, "example": "进行中",
         "options": ["进行中", "已完成"]},
        {"key": "done_count", "label": "完成数", "type": "number", "required": False,
         "map_to": "progress_value", "aggregatable": True, "example": "12"},
        # 有 aggregate 能力的页必须留一个 evidence 字段，否则校验直接 FIELD_UNMAPPED。
        {"key": "evidence", "label": "凭据", "type": "text", "required": False,
         "map_to": "evidence", "aggregatable": True, "example": "SY-2410"},
    ],
    "status_map": {"进行中": "in_progress", "已完成": "done"},
    "rules": {"evidence_required": False},
    "metrics": [{"metric_key": "dept.DESIGN.done_count", "mode": "emit",
                 "agg": "sum", "from_field": "done_count"}],
    "meta": {"author_position_code": "DESIGN_LEAD", "version": 1,
             "visibility": "department", "review": {"status": "draft"}},
}))

#: 历史上真实存在过、现在创建入口会拒的写法：引用已经下线的模块。
#: 预览遇到它不是「服务器坏了」，是这一版画不出来——要给人话，不是 500。
BROKEN_DESCRIPTOR = json.loads(json.dumps(METRIC_DESCRIPTOR))
BROKEN_DESCRIPTOR["modules"] = [{"key": "gone", "type": "status_list", "params": {}}]


def _preview(room, interface_id, *, htmx: bool = True):
    """看一眼某个版本。默认按审核卡里那个 htmx 片段请求来。"""
    headers = {"HX-Request": "true"} if htmx else {}
    # 不跟随重定向：未登录那一条要断的是 302 本身，跟过去就只剩登录页的 200。
    return room["client"]().get(f"/reviews/{interface_id}/preview",
                                headers=headers, follow_redirects=False)


def _counts(room) -> dict[str, int]:
    return {table: room["conn"].execute(f"SELECT count(*) AS n FROM oams.{table}").fetchone()["n"]
            for table in ("interfaces", "interface_entries", "workforce_audit_events")}


def _a_live_page(room) -> uuid.UUID:
    """走完整真实链路造一版上线页：建 → 提交审核 → 部门初审通过。

    不拿种子里那条 `design-sampling` 当「活页面」：种子库是 session 级的，前序用例
    会把它归档、换作者，于是这条用例单跑绿、全量红——最难查的那一类红。自己造一版，
    每一步都走产品的真入口，`updated_by` 就是作者本人。
    """
    waiting = room["page"](room["design"])          # 建 → 提交，作者是 DESIGN_AUTHOR
    room["as_"](DESIGN_REVIEWER)
    passed = _approve(room, waiting, note="初审过，这一版上线")
    assert passed.status_code == 303, passed.text
    row = room["conn"].execute(
        "SELECT status FROM oams.interfaces WHERE interface_id = %s", (waiting,)).fetchone()
    assert row["status"] == "active", row
    return waiting


def _fill_entry(room, interface_id, values: dict[str, str]):
    """按填写页自己的表单交一条条目：csrf 从页面里取，不手搓。

    走 `/interfaces/{id}`——人真正填数的地方。这样下面那条「真实条目不出现在预览里」
    的对照才是真的：两边比的是同一批数据，不是我另造一份假数据。
    """
    page = room["client"]().get(f"/interfaces/{interface_id}")
    assert page.status_code == 200, page.text
    found = re.search(r'name="csrf" value="([^"]+)"', page.text)
    assert found, page.text[:400]
    sent = room["client"]().post(
        f"/interfaces/{interface_id}",
        data={"csrf": found.group(1), **values}, follow_redirects=False)
    assert sent.status_code == 303, sent.text
    return sent


# --------------------------------------------------------------------------- #
# 谁能看：与 /reviews 同一套口径，预览不是后门
# --------------------------------------------------------------------------- #


def test_a_department_reviewer_previews_a_page_waiting_for_them(room):
    waiting = room["page"](room["design"])
    room["as_"](DESIGN_REVIEWER)
    seen = _preview(room, waiting)
    assert seen.status_code == 200, seen.text
    # 画的是「审核视角」：一个 data-mode 就说明走了 render_preview 而不是编辑态。
    assert 'data-mode="preview"' in seen.text
    assert SAMPLE_TITLE in seen.text


def test_the_author_is_not_the_person_who_gets_to_preview_it(room):
    """作者看自己交的版本同样被拒——预览不能变成「自己先审一遍」。"""
    waiting = room["page"](room["design"])  # author_for(DESIGN) = DESIGN_AUTHOR
    room["as_"](DESIGN_AUTHOR)
    refused = _preview(room, waiting)
    assert refused.status_code == 403
    assert "自己提交的页面要由别人审" in refused.text


def test_someone_without_review_power_is_told_which_permission_is_missing(room):
    waiting = room["page"](room["design"])
    room["as_"](HR_AUTHOR)  # 人事经理：能填页，但没有 review 权
    refused = _preview(room, waiting)
    assert refused.status_code == 403
    assert "初审需要 workforce.interfaces/review 权限" in refused.text


def test_a_reviewer_cannot_preview_another_departments_page(room):
    """人事部的待审页不该被设计部负责人看见——预览要是宽一格，审核队列就是筛子。"""
    elsewhere = room["page"](room["hr"])
    room["as_"](DESIGN_REVIEWER)
    refused = _preview(room, elsewhere)
    assert refused.status_code == 403
    assert "这个页面不在你所在部门的范围内" in refused.text


def test_a_company_level_page_is_the_platforms_to_preview(room):
    waited_up = room["page"](room["design"], stage="company")
    room["as_"](DESIGN_REVIEWER)
    refused = _preview(room, waited_up)
    assert refused.status_code == 403
    assert "公司终审需要" in refused.text
    room["as_"](PLATFORM)
    seen = _preview(room, waited_up)
    assert seen.status_code == 200, seen.text


def test_a_live_page_can_still_be_previewed(room):
    """上线之后点「看一眼」不该 403：审核人复核线上那一版是正当动作。

    只有「状态」这一条被放宽，作者规则不放宽——所以这里用平台身份看别人写的活页面。
    """
    live = _a_live_page(room)
    room["as_"](DESIGN_REVIEWER)   # 同部门审核人复核线上那一版
    seen = _preview(room, live)
    assert seen.status_code == 200, seen.text
    assert 'data-mode="preview"' in seen.text


def test_the_author_still_cannot_preview_their_own_live_page(room):
    """状态放宽不等于身份放宽：活页面的作者仍然是「自己交的那一版」。"""
    live = _a_live_page(room)
    room["as_"](DESIGN_AUTHOR)  # 这一版就是他交的
    refused = _preview(room, live)
    assert refused.status_code == 403
    assert "自己提交的页面要由别人审" in refused.text


def test_a_draft_has_no_version_to_show_yet(room):
    created = room["create"](room["design"])  # 建了但不提交：还是 draft
    room["as_"](DESIGN_REVIEWER)
    refused = _preview(room, created)
    assert refused.status_code == 403
    assert "既不在待审核、也没有上线" in refused.text


def test_an_archived_version_has_nothing_to_show_either(room):
    """归档页不许预览：它已经不是任何人心里的「这一版」，画出来只会让人误批。"""
    old = room["conn"].execute(
        "SELECT interface_id FROM oams.interfaces WHERE slug = %s AND status = 'active'",
        (LIVE_SLUG,)).fetchone()["interface_id"]
    # 真实入口：同一 slug 再写一版、提交、部门初审通过 —— 活的那版被归档。
    successor = room["create"](room["design"], slug=LIVE_SLUG, name="设计部打样进度（第二版）")
    room["submit"](room["design"], successor)
    room["as_"](DESIGN_REVIEWER)
    passed = _approve(room, successor, note="第二版上线")
    assert passed.status_code == 303, passed.text
    assert room["conn"].execute(
        "SELECT status FROM oams.interfaces WHERE interface_id = %s", (old,)
    ).fetchone()["status"] == "archived"

    refused = _preview(room, old)
    assert refused.status_code == 403
    assert "既不在待审核、也没有上线" in refused.text


def test_a_page_that_does_not_exist_is_a_404_not_a_403(room):
    """「没有这页」和「你不能看」必须分开：混成 403 会让人以为权限没开。"""
    room["as_"](PLATFORM)
    missing = _preview(room, uuid.uuid4())
    assert missing.status_code == 404
    assert "找不到这个页面" in missing.text


def test_signed_out_visitors_are_sent_to_the_review_queue_login(room, monkeypatch):
    waiting = room["page"](room["design"])
    monkeypatch.setattr(oams.api, "principal_from_request", lambda request: None)
    sent = _preview(room, waiting)
    assert sent.status_code == 302
    assert sent.headers["location"] == "/login?next=/reviews"


# --------------------------------------------------------------------------- #
# 看什么：样本值，不是谁填过的真数据
# --------------------------------------------------------------------------- #


def test_the_preview_shows_the_descriptors_sample_not_what_people_typed(room):
    """预览画样本，不画谁填过的字——而且对照页上那些字真的存在。

    自己造一版上线页、自己走填写页交一条带 canary 的条目：两边是人、数据和入口都
    真实的一条链，不借种子里那条会被别的用例改写的页。
    """
    live = _a_live_page(room)
    room["as_"](DESIGN_AUTHOR)
    _fill_entry(room, live, {"title": "AW26-901 羊绒围巾",
                             "state_word": "进行中",
                             "evidence": LIVE_ENTRY_CANARY})

    room["as_"](DESIGN_REVIEWER)
    drawn = _preview(room, live)
    assert drawn.status_code == 200, drawn.text
    assert SAMPLE_TITLE in drawn.text            # 样本款号在
    assert "AW26-901" not in drawn.text          # 条目里的款号不在
    assert LIVE_ENTRY_CANARY not in drawn.text   # 真实条目独有的值也不在

    # 正向对照：同一个人到填写页看，canary 确实在——否则上面的「不在」只证明
    # 「我没造出那条数据」。
    room["as_"](DESIGN_AUTHOR)
    filled = room["client"]().get(f"/interfaces/{live}")
    assert filled.status_code == 200, filled.text
    assert LIVE_ENTRY_CANARY in filled.text


#: 只存在于「这一版 descriptor」里的标记：预览画样本时该出现，审核列表页不该出现。
LEAK_PROBE = "PREVIEW-LEAK-PROBE"


def _probe_descriptor() -> dict:
    """同一份合法描述符，只把「款号」的样例值换成一个独一无二的标记。

    标在 `example` 上：预览渲样本值时它一定出现（`progress_row` 会把 title 印出来），
    而列表页只该给「改了什么」的差异摘要，不该把描述符里的样例值摊开。
    """
    descriptor = json.loads(json.dumps(METRIC_DESCRIPTOR))
    for field in descriptor["fields"]:
        if field["key"] == "title":
            field["example"] = LEAK_PROBE
    descriptor["modules"] = [{"key": "row", "type": "progress_row"}]
    return descriptor


def test_the_review_list_does_not_render_the_inside_of_a_descriptor(room):
    """列表页读的是「谁在等、差在哪」，不是把 descriptor 摊开渲一遍。

    标记放在 `fields[].example` 上：它 100% 会出现在预览（样本值就是它），所以列表页
    只要顺手渲了描述符明细，这条就会红。
    """
    # 非首版：先有一版在线的，第二版才走「对比上一版」那个差异块——首版只显示一句
    # 「首版」，那时列表页本来就不列明细，断言等于没测。
    first = _a_live_page(room)
    slug = room["conn"].execute(
        "SELECT slug FROM oams.interfaces WHERE interface_id = %s", (first,)).fetchone()["slug"]
    waiting = room["create"](room["design"], slug=slug, name="审核分级样张（第二版）",
                             descriptor=_probe_descriptor())
    room["submit"](room["design"], waiting)
    room["as_"](DESIGN_REVIEWER)
    listing = room["client"]().get("/reviews")
    assert listing.status_code == 200, listing.text
    assert LEAK_PROBE not in listing.text
    drawn = _preview(room, waiting)
    assert LEAK_PROBE in drawn.text, "标记得真在那一版里，否则上面那条断言是空的"


def test_the_preview_body_is_identical_whether_the_page_has_entries(room):
    """预览一个字都不吃条目：同一条 URL，条目从无到有，正文逐字相同。

    这是「不查库」在路由层的可测形态。整条路由当然要读 `interfaces` 行才知道渲哪一版
    （指一个不存在的库连 200 都拿不到），但「渲出来的正文」与库里的填报数据完全无关——
    那正是它和 `/departments/...` 的区别。
    """
    live = _a_live_page(room)
    room["as_"](DESIGN_REVIEWER)
    empty = _preview(room, live)
    assert empty.status_code == 200, empty.text

    room["as_"](DESIGN_AUTHOR)
    _fill_entry(room, live, {"title": "AW26-902 羊毛围巾",
                             "state_word": "进行中",
                             "evidence": "PREVIEW-NOT-FROM-ENTRY"})

    room["as_"](DESIGN_REVIEWER)
    filled = _preview(room, live)
    assert filled.status_code == 200, filled.text
    assert filled.text == empty.text
    assert "PREVIEW-NOT-FROM-ENTRY" not in filled.text


def test_the_preview_draws_the_metrics_the_page_declares(room):
    """自产指标的样本数由 render_preview 给，不查库也算得出来 —— 预览断的是定义。"""
    waiting = room["page"](room["design"], descriptor=METRIC_DESCRIPTOR)
    room["as_"](DESIGN_REVIEWER)
    drawn = _preview(room, waiting)
    assert drawn.status_code == 200, drawn.text
    assert "128" in drawn.text


def test_previewing_writes_nothing(room):
    """GET 预览不是一次编辑：接口、条目、审计三张表的行数一行都不许动。"""
    waiting = room["page"](room["design"])
    room["as_"](DESIGN_REVIEWER)
    before = _counts(room)
    drawn = _preview(room, waiting)
    assert drawn.status_code == 200
    assert _counts(room) == before


# --------------------------------------------------------------------------- #
# 怎么答：htmx 片段 vs 整页、以及不能被缓存
# --------------------------------------------------------------------------- #


def test_the_preview_is_never_cached(room):
    """版本在队列里会变；缓存住的预览就是把上一版画给审核人。"""
    waiting = room["page"](room["design"])
    room["as_"](DESIGN_REVIEWER)
    drawn = _preview(room, waiting)
    assert drawn.status_code == 200
    assert drawn.headers["cache-control"] == "no-store"


def test_a_refusal_answered_to_htmx_is_a_flash_block_not_a_page(room):
    """审核卡只换一个片段：把整页 denied.html 塞进卡片里等于把卡片撑爆。"""
    waiting = room["page"](room["design"])
    room["as_"](DESIGN_AUTHOR)  # 作者本人：必然被拒
    fragment = _preview(room, waiting, htmx=True)
    assert fragment.status_code == 403
    assert fragment.text.startswith('<div class="flash error" role="alert">')
    assert "<html" not in fragment.text.lower()

    whole = _preview(room, waiting, htmx=False)
    assert whole.status_code == 403
    # 同一个人、同一页，换成浏览器直接打开就是一张完整的「不许看」页面。
    assert "<html" in whole.text.lower()
    assert "自己提交的页面要由别人审" in whole.text


# --------------------------------------------------------------------------- #
# 画不出来怎么办：一句人话，不是 500
# --------------------------------------------------------------------------- #


def test_a_version_that_cannot_be_drawn_answers_with_a_sentence(room, caplog):
    """引用已下线模块的那一版：200 + 一句人话，异常文本不整段进页面。

    这种版本在审核队列里是现实（老页面的模块随产品中心一起下线了），但**现在建不
    出来**——创建入口会以 MODULE_NOT_WHITELISTED 拒掉。所以这里先用真入口建一版
    正常页并提交，再在这一笔事务里把它存的那份 descriptor 换成历史写法，模拟库里
    已经躺着的老版本；改的是我自己刚造的那一行，且带 `WHERE interface_id`。
    """
    waiting = room["page"](room["design"])          # 真入口：建 → 提交
    room["conn"].execute(
        "UPDATE oams.interfaces SET descriptor = %s::jsonb WHERE interface_id = %s",
        (json.dumps(BROKEN_DESCRIPTOR), waiting))

    room["as_"](DESIGN_REVIEWER)
    with caplog.at_level(logging.WARNING, logger="oams.render"):
        drawn = _preview(room, waiting)

    assert drawn.status_code == 200, drawn.text      # 不是 500：htmx 不换非 2xx
    assert 'data-degraded="1"' in drawn.text
    assert 'class="preview-degraded"' in drawn.text
    assert "预览不可用" in drawn.text
    assert "status_list 已下线" in drawn.text
    # 只引模块名，不把整段异常（错误码、位置、堆栈）摊到审核人脸上。
    assert "MODULE_NOT_WHITELISTED" not in drawn.text

    logged = [r.getMessage() for r in caplog.records if r.name == "oams.render"]
    assert any("preview degraded:" in m and f"interface={waiting}" in m
               and "未注册的模块类型" in m for m in logged), logged


def test_a_drawable_version_still_draws_the_preview(room):
    """降级只落在画不出来的版本上：同一批人看正常版，仍是样本值而不是提示条。"""
    waiting = room["page"](room["design"])
    room["as_"](DESIGN_REVIEWER)
    drawn = _preview(room, waiting)
    assert drawn.status_code == 200, drawn.text
    assert "data-degraded" not in drawn.text
    assert SAMPLE_TITLE in drawn.text
