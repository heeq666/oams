"""Module-level acceptance tests for the v0.2 renderer (contract §6, §1).

tests/test_render_interface.py checks the three entry points against one realistic
page. This file checks the *registry contract* itself: every one of the twenty
frozen modules must actually draw, in all three viewpoints, with the data rules the
contract states -- containers carry no data, input modules carry widgets only in the
edit viewpoint, display modules read aggregates and never the descriptor, and every
module says in preview whether it feeds the company aggregate.

Everything here is pure: no database, no HTTP, no clock.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, meta

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import oams.render as render  # noqa: E402
from oams.modules import MODULE_REGISTRY, get_module  # noqa: E402
from oams.render import (  # noqa: E402
    MAX_RENDER_DEPTH,
    RenderError,
    known_module_keys,
    render_interface,
    render_preview,
    render_read_only,
    template_name,
)

TEMPLATE_DIR = Path(render.__file__).resolve().parent / "templates" / "modules"

CONTAINER_KEYS = {"columns", "rows", "grid", "card", "tabs", "divider", "spacer"}
INPUT_KEYS = {
    "progress_row", "table", "timeline", "blocker_list", "metric_input", "evidence_gallery",
}
DISPLAY_KEYS = {
    "metric_card", "metric_grid", "flow_strip", "status_chart", "rank_list",
    "activity_feed", "note_block",
}

FIELDS = [
    {"key": "f_title", "label": "款号", "type": "text", "map_to": "title",
     "required": True, "example": "AW26-001"},
    {"key": "f_status", "label": "状态", "type": "enum", "map_to": "status",
     "required": True, "options": ["待打样", "打样中", "等面料", "已完成"]},
    {"key": "f_progress", "label": "完成度", "type": "percent", "map_to": "progress_value",
     "required": True, "example": "65"},
    {"key": "f_note", "label": "备注", "type": "text", "map_to": "note", "example": "面料已到"},
    {"key": "f_evidence", "label": "依据", "type": "text", "map_to": "evidence",
     "example": "SY-2610"},
    {"key": "f_url", "label": "证据链接", "type": "link", "map_to": "evidence_url",
     "example": "https://example.com/sheet.pdf"},
    {"key": "f_due", "label": "到期", "type": "date", "map_to": "due_at", "example": "2026-11-05"},
    {"key": "f_blocked", "label": "阻塞原因", "type": "text", "map_to": "blocked_reason",
     "example": "等检测报告"},
    {"key": "f_decision", "label": "需要谁决策", "type": "text", "map_to": "needs_decision",
     "example": "设计总监"},
    {"key": "f_metric", "label": "件数", "type": "number", "map_to": "metric_value",
     "required": True, "example": "128"},
]

STATUS_MAP = {"待打样": "planned", "打样中": "in_progress", "等面料": "blocked", "已完成": "done"}

VALUES = {
    "f_title": "AW26-001", "f_status": "等面料", "f_progress": "65", "f_note": "面料已到仓",
    "f_evidence": "SY-2610", "f_url": "https://example.com/sheet.pdf",
    "f_due": "2026-11-05", "f_blocked": "等检测报告", "f_decision": "设计总监",
    "f_metric": "128",
}

AGGREGATES = {
    "dept.DESIGN.designed": 128, "dept.DESIGN.sample_done": 59,
    "dept.DESIGN.sample_wip": 24, "dept.DESIGN.sample_pending": 45,
    "dept.DESIGN.sample_open": 69, "dept.DESIGN.priced": 87,
    "dept.DESIGN.ordered": 63, "dept.DESIGN.not_ordered": 65, "dept.DESIGN.qty": 63289,
    "dept.DESIGN.gmv": 17458734.0, "dept.DESIGN.roi": 5.1751,
    "rank.dept.DESIGN.qty.抖音": 820, "rank.dept.DESIGN.qty.天猫": 640,
    "rank.dept.DESIGN.qty.拼多多": 310,
    "activity": [
        {"when": "今天 09:12", "what": "补充了打样单号 SY-2610", "who": "设计部"},
        {"when": "昨天 16:40", "what": "把 3 款推进到打样中", "who": "设计部"},
    ],
}
for _index, (_word, _standard) in enumerate(STATUS_MAP.items()):
    AGGREGATES[f"status.{_word}"] = 10 + _index * 5
    AGGREGATES[f"status.{_standard}"] = 10 + _index * 5

#: 展示件引的指标必须先在本页 emit（契约 §2：自产才能自用）。
#: 平台目录现在是空表——商品中心下线时那 11 个 funnel.* 一起删了——所以这里
#: 全部走部门命名空间，聚合口径也写在本页，读者能自己核对数字从哪来。
EMITTED_METRICS = [
    {"metric_key": f"dept.DESIGN.{slug}", "mode": "emit",
     "agg": "avg" if slug == "roi" else ("sum" if slug in {"qty", "gmv"} else "count"),
     "from_field": "f_metric" if slug in {"qty", "gmv", "roi"} else "f_status"}
    for slug in ("designed", "sample_done", "sample_wip", "sample_pending", "sample_open",
                 "priced", "ordered", "not_ordered", "qty", "gmv", "roi")
]

LEAF = {"type": "note_block", "params": {"text": "说明"}}

#: One legal, fully-configured node per registered module.
MODULES: dict[str, dict] = {
    "columns": {"type": "columns", "params": {"columns": 2}, "children": [LEAF, LEAF]},
    "rows": {"type": "rows", "children": [LEAF]},
    "grid": {"type": "grid", "params": {"columns": 3}, "children": [LEAF]},
    "card": {"type": "card", "params": {"title": "本周打样"}, "children": [LEAF]},
    "tabs": {"type": "tabs", "params": {"labels": ["概览", "明细"]}, "children": [LEAF, LEAF]},
    "divider": {"type": "divider", "params": {"label": "分隔"}},
    "spacer": {"type": "spacer", "params": {"size": "lg"}},
    "progress_row": {"type": "progress_row",
                     "source_fields": ["title", "status", "progress_value", "note", "evidence"]},
    "table": {"type": "table",
              "params": {"columns": [{"key": "f_title", "label": "款号"},
                                     {"key": "f_status", "label": "状态"}], "rows": 2},
              "source_fields": ["title", "status"]},
    "timeline": {"type": "timeline",
                 "params": {"items": [{"text": "出样", "status": "已完成", "due": "11-05"}]},
                 "source_fields": ["title", "due_at", "status"]},
    "blocker_list": {"type": "blocker_list",
                     "source_fields": ["title", "blocked_reason", "needs_decision", "status"]},
    "metric_input": {"type": "metric_input", "params": {"unit": "件"},
                     "source_fields": ["title", "metric_value"]},
    "evidence_gallery": {"type": "evidence_gallery",
                         "source_fields": ["title", "evidence_url", "note"]},
    "metric_card": {"type": "metric_card",
                    "params": {"metric_key": "dept.DESIGN.sample_done", "span": 2}},
    "metric_grid": {"type": "metric_grid",
                    "params": {"metric_keys": ["dept.DESIGN.sample_done", "dept.DESIGN.qty"],
                               "columns": 2}},
    "flow_strip": {"type": "flow_strip",
                   "params": {"metrics": ["dept.DESIGN.designed", "dept.DESIGN.sample_done"],
                              "labels": ["设计", "出样"]}},
    "status_chart": {"type": "status_chart", "params": {"dimension": "status"}},
    "rank_list": {"type": "rank_list", "params": {"metric_key": "dept.DESIGN.qty", "limit": 2}},
    "activity_feed": {"type": "activity_feed", "params": {"limit": 1}},
    "note_block": {"type": "note_block", "params": {"text": "这一页说明打样口径。"}},
}


def page(*modules: dict, **overrides) -> dict:
    """A descriptor that can host any module tree."""
    descriptor = {
        "schema_version": "0.2",
        "interface_id": "b2f3a1c0-0000-4000-8000-000000000002",
        "name": "渲染模块契约",
        "org_unit_code": "DESIGN",
        "modules": list(modules) or [LEAF],
        "fields": [dict(field) for field in FIELDS],
        "status_map": dict(STATUS_MAP),
        "metrics": [dict(binding) for binding in EMITTED_METRICS],
    }
    descriptor.update(overrides)
    return descriptor


def one(key: str) -> dict:
    return page(MODULES[key])


ALL_KEYS = sorted(MODULE_REGISTRY)
BY_LAYER = [(key, get_module(key).layer) for key in ALL_KEYS]


# --------------------------------------------------------------------------- #
# Templates and the registry agree (contract §6: one template per module)
# --------------------------------------------------------------------------- #


def test_registry_is_the_frozen_twenty():
    assert len(MODULE_REGISTRY) == 20
    assert set(MODULE_REGISTRY) == CONTAINER_KEYS | INPUT_KEYS | DISPLAY_KEYS
    assert set(known_module_keys()) == set(MODULE_REGISTRY)


def test_no_registered_module_is_missing_a_template():
    missing = [key for key in ALL_KEYS if not render.has_template(key)]
    assert missing == []


def test_no_template_exists_for_an_unregistered_module():
    # A leftover template would be dead weight the whitelist can never reach.
    on_disk = {path.stem for path in TEMPLATE_DIR.glob("*.html")}
    assert on_disk == set(ALL_KEYS)


def test_template_context_is_exactly_the_contract_context():
    # Contract §6 fixes the context at {module, spec, values, aggregates, mode}.
    # Anything else a template needs must arrive through a helper, not a new key.
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))
    # Helpers arrive as jinja globals, so the static analysis sees them as
    # undeclared names; they are part of the contract's helper surface, not
    # extra context keys.
    allowed = {"module", "spec", "values", "aggregates", "mode", "loop"}
    allowed |= set(render._ENV.globals)
    offenders: dict[str, list[str]] = {}
    for key in ALL_KEYS:
        source = (TEMPLATE_DIR / template_name(key)).read_text(encoding="utf-8")
        declared = meta.find_undeclared_variables(env.parse(source))
        extra = sorted(declared - allowed)
        if extra:
            offenders[key] = extra
    assert offenders == {}


@pytest.mark.parametrize("key", ALL_KEYS)
def test_every_module_renders_in_the_edit_viewpoint(key):
    html = render_interface(one(key), VALUES, AGGREGATES)
    assert f'data-type="{key}"' in html


@pytest.mark.parametrize("key", ALL_KEYS)
def test_every_module_renders_in_the_preview_viewpoint(key):
    html = render_preview(one(key))
    assert f'data-mode="preview"' in html
    assert f'data-type="{key}"' in html


@pytest.mark.parametrize("key", sorted(set(ALL_KEYS) - {"evidence_gallery"}))
def test_every_visible_module_renders_in_the_readonly_viewpoint(key):
    # evidence_gallery is department-private, so the manager view drops it by
    # design; test_readonly_skips_department_private_modules covers that case.
    html = render_read_only(one(key), VALUES, AGGREGATES)
    assert f'data-type="{key}"' in html


# --------------------------------------------------------------------------- #
# Data rules: widgets, aggregates, and the two iron rules from the concept doc
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key", sorted(INPUT_KEYS))
def test_input_modules_offer_widgets_in_edit(key):
    html = render_interface(one(key), VALUES, AGGREGATES)
    widgets = html.count("<input") + html.count("<select") + html.count("<textarea")
    assert widgets >= 1, key


@pytest.mark.parametrize("key", sorted(CONTAINER_KEYS | DISPLAY_KEYS))
def test_containers_and_displays_never_offer_data_widgets(key):
    """容器只排版、展示件只读数，两者都不该长出一个可填的数据字段控件。

    例外是组件自己的视觉机制：tabs 用 `wh-tab-radio` 做纯 CSS 分段切换，
    不绑任何字段，不算数据控件。
    """
    html = render_interface(one(key), VALUES, AGGREGATES)
    assert 'class="wh-input"' not in html
    assert "<select" not in html
    assert "<textarea" not in html


DATA_WIDGET = re.compile(r'<(?:input|select|textarea)\b[^>]*class="wh-input"[^>]*>')
ANY_WIDGET = re.compile(r"<(?:input|select|textarea)\b")


def widgets(html: str) -> list[str]:
    """每个字段控件的开标签（input / select / textarea 三种都带 wh-input）。"""

    return DATA_WIDGET.findall(html)


@pytest.mark.parametrize("key", sorted(INPUT_KEYS))
def test_edit_renders_live_widgets(key):
    """填报视角：控件在场、能填、必填项照旧标 required。"""
    found = widgets(render_interface(one(key), VALUES, AGGREGATES))
    assert found, key
    assert not [w for w in found if "disabled" in w], key


# 注：preview 角标（进汇总 / 不进汇总）已随 preview_tag 下线，网页审核页暂时没有
# 汇总口径展示——机器可读的出口在 MCP 的 preview_descriptor 返回
# （aggregates_preview 字段）。要不要在审核页补一块汇总面板属于新功能，
# 等模块细化 B0–B4 走完再评估（负责人 2026-10-30 定），这里不锁。
@pytest.mark.parametrize("key", sorted(INPUT_KEYS))
def test_preview_renders_disabled_widgets(key):
    """审核视角：控件在场，让人看见这页会长什么样，但一格都提交不了。

    三条一起断言，缺一条都不算数：控件必须出现、必须 disabled、必须
    tabindex="-1"（键盘也 tab 不进去）；而且**不许**带 required——审核的人
    没有被要求填任何东西。
    """
    html = render_preview(one(key))
    found = widgets(html)
    assert found, f"{key}: 审核视角要看得见控件"
    for tag in found:
        assert "disabled" in tag, f"{key}: {tag}"
        assert 'tabindex="-1"' in tag, f"{key}: {tag}"
        assert "required" not in tag, f"{key}: 审核视角不该要求填写 —— {tag}"


@pytest.mark.parametrize("key", sorted(INPUT_KEYS))
def test_readonly_renders_static_fields_only(key):
    """只读视角：一个字都填不了，连控件壳子都不出现，只有排好版的静态值。

    这里用最严的判据（任何 input / select / textarea 都算违规），因为只读
    视角不该有可交互的东西。
    """
    html = render_read_only(one(key), VALUES, AGGREGATES)
    assert not ANY_WIDGET.findall(html), f"{key}: 只读视角出现了控件"
    assert not widgets(html), key


@pytest.mark.parametrize("key", sorted(set(ALL_KEYS) - {"evidence_gallery"}))
def test_readonly_never_shows_a_wh_input_anywhere(key):
    """「只读下一个字段控件都没有」对每个可见模块单独出证。

    上面那条只管六个填报模块；容器与展示件本来就不长控件，拿它们间接覆盖
    是虚的——换个模板把控件漏出来也不会红。所以这里对全部可见模块各证一遍：
    `wh-input` 计数为 0。
    """

    html = render_read_only(one(key), VALUES, AGGREGATES)
    assert 'class="wh-input"' not in html, key
    assert not DATA_WIDGET.findall(html), key


@pytest.mark.parametrize("key", sorted(CONTAINER_KEYS | DISPLAY_KEYS))
def test_readonly_containers_and_displays_never_show_a_field(key):
    """容器与展示件在只读视角里同样没有字段控件。

    tabs 的分段切换是 `wh-tab-radio`，那是组件自己的纯 CSS 机制、不绑字段，
    任何视角都照样能切，不在这一条的判据里（它不带 wh-input）。
    """
    html = render_read_only(one(key), VALUES, AGGREGATES)
    assert not widgets(html), key


@pytest.mark.parametrize("key", sorted(CONTAINER_KEYS))
def test_containers_carry_no_data_binding_of_their_own(key):
    # Concept §3.1: a container arranges, it never displays a value or a label.
    html = render_interface(one(key), VALUES, AGGREGATES)
    assert 'class="wh-field-label"' not in html
    assert 'class="wh-field"' not in html
    assert 'class="wh-input"' not in html


def test_readonly_skips_department_private_modules():
    html = render_read_only(one("evidence_gallery"), VALUES, AGGREGATES)
    assert 'data-type="evidence_gallery"' not in html
    assert "sheet.pdf" not in html
    assert render_interface(one("evidence_gallery"), VALUES, AGGREGATES)


def test_readonly_drops_a_container_whose_children_are_all_private():
    tree = {"type": "card", "params": {"title": "证据"}, "children": [MODULES["evidence_gallery"]]}
    assert "sheet.pdf" not in render_read_only(page(tree), VALUES, AGGREGATES)


# --------------------------------------------------------------------------- #
# Display modules read aggregates, never the descriptor
# --------------------------------------------------------------------------- #


def test_metric_card_shows_the_aggregate_value():
    """卡片上的名字与数字都可追溯到本页声明的那条指标。

    平台目录空了以后，部门指标没有目录里的 name/unit，所以标签落到键的后段
    （`sample_done`）、单位留空——不写死中文名，也不编一个单位出来。
    """
    html = render_interface(one("metric_card"), {}, AGGREGATES)
    assert "sample_done" in html     # 标签来自本页 emit 的键
    assert "59" in html              # 数字来自聚合，不来自描述符


def test_metric_card_shows_the_declared_label_and_unit():
    """人话名与单位由 emit 绑定声明，页面照着印，数字仍是裸的。

    三个元素各自独立：`wh-metric-label` 是人话名、`wh-metric-value` 只是数字、
    `wh-metric-unit` 单独一格。把它们拼成「本周完成 1 项」那是排版的事，
    代码里不能提前拼——否则数字那一格再也套不上千分位/小数位规则。
    """
    metric_key = "dept.DESIGN.weekly_done"
    html = render_interface(
        page({"type": "metric_card", "params": {"metric_key": metric_key}},
             metrics=[{"metric_key": metric_key, "mode": "emit", "agg": "count",
                       "from_field": "f_status", "where_status": "done",
                       "label": "本周完成", "unit": "项"}]),
        {}, {metric_key: 1},
    )
    assert '<span class="wh-metric-label">本周完成</span>' in html
    assert '<span class="wh-metric-unit">项</span>' in html
    values = re.findall(r'<span class="wh-metric-value">(.*?)</span>', html)
    assert values == ["1"]              # 单位不在数字里，免得被格式化规则吞掉
    # 声明了人话名，标签那一格就不该再露 slug（`data-metric` 属性里留着键是对的，
    # 那是给机器看的身份，不是显示文案）。
    assert ">weekly_done<" not in html


def test_metric_card_without_a_label_falls_back_to_the_key_tail_without_a_unit():
    """不写两个字段也能用：名字回落键末段，单位那一格整个不渲染。

    「没有单位」和「单位是空串」在页面上必须是同一个样子——不能留一个空 span，
    否则布局会凭空多一格。
    """
    html = render_interface(one("metric_card"), {}, AGGREGATES)
    assert '<span class="wh-metric-label">sample_done</span>' in html
    assert "wh-metric-unit" not in html


def test_metric_grid_shows_the_declared_label_and_unit_per_cell():
    metric_a = "dept.DESIGN.weekly_done"
    metric_b = "dept.DESIGN.parcels"
    html = render_interface(
        page({"type": "metric_grid", "params": {"metric_keys": [metric_a, metric_b]}},
             metrics=[
                 {"metric_key": metric_a, "mode": "emit", "agg": "count",
                  "from_field": "f_status", "label": "本周完成", "unit": "项"},
                 {"metric_key": metric_b, "mode": "emit", "agg": "count",
                  "from_field": "f_status", "label": "待取件", "unit": "件"},
             ]),
        {}, {metric_a: 1, metric_b: 7},
    )
    assert '<span class="wh-metric-label">本周完成</span>' in html
    assert '<span class="wh-metric-label">待取件</span>' in html
    assert '<span class="wh-metric-unit">项</span>' in html
    assert '<span class="wh-metric-unit">件</span>' in html


def test_money_and_percent_units_ride_inside_the_number_not_in_a_span():
    """元与 % 自带字形（¥ / %），不再另起一个单位格，也不重复印。

    `format_number` 把货币写成 `¥1,280.00`（≥1 万才走「万元」），`metric_unit`
    对这两种单位返回空串——否则页面上会出现「¥1,280.00 元」这种同一件事说两遍。
    """
    money = "dept.DESIGN.spend"
    html = render_interface(
        page({"type": "metric_card", "params": {"metric_key": money}},
             metrics=[{"metric_key": money, "mode": "emit", "agg": "sum",
                       "from_field": "f_metric", "label": "本部门支出", "unit": "元"}]),
        {}, {money: 1280.0},
    )
    assert '<span class="wh-metric-value">\u00a51,280.00</span>' in html
    assert "wh-metric-unit" not in html

    percent = "dept.DESIGN.done_rate"
    html = render_interface(
        page({"type": "metric_card", "params": {"metric_key": percent}},
             metrics=[{"metric_key": percent, "mode": "emit", "agg": "avg",
                       "from_field": "f_metric", "label": "完成率", "unit": "%"}]),
        {}, {percent: 62.5},
    )
    assert '<span class="wh-metric-value">62.50%</span>' in html
    assert "wh-metric-unit" not in html


def test_metric_card_without_aggregates_does_not_invent_a_number():
    html = render_interface(one("metric_card"), {}, {})
    values = re.findall(r'<span class="wh-metric-value">(.*?)</span>', html)
    assert values == ["\u2014"]        # em dash，不是 0、也不是两个减号
    assert not re.search(r'wh-metric-value">[\d¥]', html)


def test_metric_grid_renders_one_cell_per_bound_metric():
    html = render_interface(one("metric_grid"), {}, AGGREGATES)
    assert html.count('class="wh-metric-cell"') == 2
    assert "sample_done" in html and "qty" in html
    assert "59" in html and "63,289" in html   # 第二个数带千分位


def test_metric_money_and_roi_use_the_canonical_formatting():
    """金额与 ROI 仍走同一份格式化，但页面只印数字本体。

    `metric_card`/`metric_grid` 用 `aggregate(key, with_unit=False)` 印裸数字，
    单位另起一个 span；而部门指标没有目录里的 unit 元数据，所以 ¥ / 万 / 倍
    这些字形目前不会出现在页面上——形状规则本身由
    `tests/test_metric_format.py` 逐条钉住（`dept.DESIGN.gmv` 仍按 `.gmv`
    后缀走万元写法）。这里钉的是「页面这一层没退化成未格式化的原值」。
    """
    money = render_interface(
        page({"type": "metric_card",
              "params": {"metric_key": "dept.DESIGN.gmv"}}), {}, AGGREGATES)
    assert "17,458,734" in money          # 千分位，不是 17458734
    roi = render_interface(page({"type": "metric_card",
                                 "params": {"metric_key": "dept.DESIGN.roi"}}), {}, AGGREGATES)
    # Rounded half up by owner ruling (2026-10-23): 5.1751 renders as 5.18.
    assert "5.18" in roi
    assert "5.17" not in roi


def test_flow_strip_uses_the_declared_stage_labels():
    html = render_interface(one("flow_strip"), {}, AGGREGATES)
    assert "设计" in html and "出样" in html
    assert html.count('class="wh-stage"') == 2


def test_status_chart_splits_the_aggregate_by_status_word():
    html = render_interface(one("status_chart"), {}, AGGREGATES)
    for word in STATUS_MAP:
        assert word in html
    assert html.count('class="wh-status-row"') == len(STATUS_MAP)


def test_rank_list_sorts_descending_and_honours_the_limit():
    html = render_interface(one("rank_list"), {}, AGGREGATES)
    labels = re.findall(r'class="wh-rank-label">([^<]*)<', html)
    assert labels == ["抖音", "天猫"]      # limit=2, highest first


def test_the_same_number_reads_the_same_in_a_card_a_grid_and_a_rank():
    """同一份聚合喂三处（卡片 / 网格 / 排行），那个数三处必须一字不差。

    排行就是「首页那一行」的同款：同一个 `rank_list` 模板、同一份 aggregates，
    只是挂在别的页面上。聚合层现在还不产 `rank.*` 键（`rank_rows` 是唯一的读方，
    全仓没有写方），所以这条钉的是渲染口径：等生产端接上，三处不许对同一个数
    给出两种写法（`820` 与 `820.00` 就是两个数，用户会以为其中一个是错的）。
    """
    key = "dept.DESIGN.qty"
    html = render_interface(
        page({"type": "metric_card", "params": {"metric_key": key}},
             {"type": "metric_grid", "params": {"metric_keys": [key]}},
             {"type": "rank_list", "params": {"metric_key": key, "limit": 5}}),
        {}, {key: 820, f"rank.{key}.抖音": 820},
    )
    card, grid = re.findall(r'<span class="wh-metric-value">(.*?)</span>', html)
    rank = re.search(r'class="wh-rank-value">([^<]*)<', html).group(1)
    assert card == grid == rank == "820"


def test_money_reads_the_same_in_a_card_a_grid_and_a_rank():
    """带单位的数同理：元自带 `¥`，三处都印 `¥1,280.00`，且都不另说一个「元」。

    排行行内承载单位（`¥1,280.00`），卡片与网格把单位让给数字本身（`metric_unit`
    对「元」返回空串），所以三处能直接比字符串。
    """
    money = "dept.DESIGN.spend"
    html = render_interface(
        page({"type": "metric_card", "params": {"metric_key": money}},
             {"type": "metric_grid", "params": {"metric_keys": [money]}},
             {"type": "rank_list", "params": {"metric_key": money, "limit": 5}},
             metrics=[{"metric_key": money, "mode": "emit", "agg": "sum",
                       "from_field": "f_metric", "label": "本部门支出", "unit": "元"}]),
        {}, {money: 1280.0, f"rank.{money}.设计部": 1280.0},
    )
    values = re.findall(r'<span class="wh-metric-value">(.*?)</span>', html)
    rank = re.search(r'class="wh-rank-value">([^<]*)<', html).group(1)
    assert values == ["\u00a51,280.00", "\u00a51,280.00"]
    assert rank == "\u00a51,280.00"
    assert "元" not in html


def test_activity_feed_honours_the_limit():
    html = render_interface(one("activity_feed"), {}, AGGREGATES)
    assert html.count("<li>") == 1


def test_activity_feed_says_nothing_when_there_is_no_data():
    html = render_interface(one("activity_feed"), {}, {})
    assert empty_lines(html) == ["还没有动态。"]


# --------------------------------------------------------------------------- #
# 空态：没数据的时候，每块都得说人话，不许出现空框
# --------------------------------------------------------------------------- #

EMPTY_LINE = re.compile(r'<p class="wh-empty">([^<]*)</p>')


def empty_lines(html: str) -> list[str]:
    """每一句空态文案，按出现顺序。"""
    return [text.strip() for text in EMPTY_LINE.findall(html)]


def test_a_display_module_says_there_is_no_ranking_yet():
    assert empty_lines(render_interface(one("rank_list"), {}, {})) == ["还没有排行数据。"]


def test_note_block_without_text_draws_nothing_at_all():
    # 空文本不该出一个空框：整块不渲染。
    html = render_interface(page({"type": "note_block", "params": {}}))
    assert "wh-note-block" not in html


def test_timeline_says_there_are_no_milestones_before_anything_is_planned():
    node = {"type": "timeline", "params": {"items": []},
            "source_fields": ["title", "due_at", "status"]}
    assert empty_lines(render_interface(page(node))) == ["还没有里程碑。"]


def test_table_empty_copy_depends_on_the_viewpoint():
    # 填报的人缺列配置是要去修的错；读的人只是没内容。
    node = {"type": "table", "params": {"columns": []}, "source_fields": ["title", "status"]}
    assert empty_lines(render_interface(page(node))) == ["缺列配置：这个表还没声明列。"]
    assert empty_lines(render_read_only(page(node))) == ["还没有明细。"]


def test_table_honours_a_flat_rows_count():
    """`rows: 3` written beside params.columns must reach the page, not be dropped."""
    node = {"type": "table", "params": {"columns": [{"key": "f_title", "label": "款号"}]},
            "rows": 3, "source_fields": ["title"]}
    html = render_interface(page(node), VALUES, AGGREGATES)
    assert html.count("<tr>") == 4  # 一行表头 + 三行明细

    # 声明过的上限在页面上也生效：写 9 不会画出 9 行（校验器此时已先报错）。
    clamped = dict(node, rows=9)
    assert render_interface(page(clamped), VALUES, AGGREGATES).count("<tr>") == 6


def test_readonly_drops_the_evidence_gallery_before_any_empty_state():
    # 证据件不进公司汇总，经理视角在 render_module 里就被 _hidden_in_readonly
    # 整块摘掉了（见 app/oams/render.py:755），所以只读视角没有「证据」这一块，
    # 也不该有「没有提供证据」这类空态文案——那句话原本永远到不了，已按裁决删掉。
    # 这里锁的是「整块不出现」，别拿空态文案当只读的验收点。
    html = render_read_only(one("evidence_gallery"), {}, {})
    assert "wh-evidence-gallery" not in html
    assert empty_lines(html) == []


def test_blocker_list_tells_a_missing_reason_from_no_blocker_at_all():
    # 三种情形要说三句不同的话：有原因、有事项没原因、根本没阻塞。
    with_reason = render_read_only(one("blocker_list"), {"f_blocked": "等检测报告"}, {})
    assert empty_lines(with_reason) == []
    assert "等检测报告" in with_reason

    reason_missing = render_read_only(one("blocker_list"), {"f_title": "打样卡住"}, {})
    assert empty_lines(reason_missing) == ["未填写原因"]

    nothing = render_read_only(one("blocker_list"), {}, {})
    assert empty_lines(nothing) == ["当前没有阻塞。"]


def test_metric_input_marks_a_missing_number_as_unfilled_not_as_a_dash():
    # 空值和 0 不是一回事：空值说「未填」，0 就是 0。
    empty = render_read_only(one("metric_input"), {}, {})
    assert '<em class="wh-num-empty">未填</em>' in empty
    assert "\u2014" not in empty

    zero = render_read_only(one("metric_input"), {"f_metric": "0"}, {})
    assert "wh-num-empty" not in zero
    assert ">0 件<" in zero


def test_status_chart_draws_no_track_when_there_is_no_data():
    html = render_interface(one("status_chart"), {}, {})
    assert empty_lines(html) == ["还没有状态数据。"]
    assert 'class="wh-status-row"' not in html


def test_unknown_metric_key_degrades_to_the_key_itself():
    html = render_interface(page({"type": "metric_card",
                                  "params": {"metric_key": "dept.DESIGN.qa"}}), {}, {})
    assert "dept.DESIGN.qa" in html


# --------------------------------------------------------------------------- #
# The three viewpoints differ only in what they ask of the reader
# --------------------------------------------------------------------------- #


def test_edit_shows_existing_values_readonly_shows_them_without_inputs():
    edit = render_interface(one("progress_row"), VALUES, AGGREGATES)
    readonly = render_read_only(one("progress_row"), VALUES, AGGREGATES)
    assert 'value="AW26-001"' in edit
    assert "AW26-001" in readonly
    assert "<input" not in readonly


def test_edit_warns_when_a_module_never_reaches_the_company_aggregate():
    assert "不参与公司汇总" in render_interface(one("evidence_gallery"), VALUES, AGGREGATES)
    # ...and the manager view has no such module to warn about
    assert "不参与公司汇总" not in render_read_only(one("evidence_gallery"), VALUES, AGGREGATES)


def test_status_word_gets_a_tone_from_the_descriptor_status_map():
    # Edit mode shows a <select>; the tone-carrying badge is the read viewpoint's.
    html = render_read_only(one("progress_row"), VALUES, AGGREGATES)
    assert "等面料" in html
    assert 'class="wh-badge wh-badge-blocked"' in html      # 等面料 -> blocked
    assert "select" not in html


def test_progress_bar_width_is_a_class_not_an_inline_style():
    html = render_interface(one("progress_row"), VALUES, AGGREGATES)
    assert "wh-bar-p70" in html       # 65 rounds to the 70 bucket
    assert "style=" not in html


@pytest.mark.parametrize("key", ALL_KEYS)
def test_no_module_emits_an_inline_style(key):
    for html in (render_interface(one(key), VALUES, AGGREGATES),
                 render_read_only(one(key), VALUES, AGGREGATES),
                 render_preview(one(key))):
        assert "style=" not in html, key


# --------------------------------------------------------------------------- #
# Escaping, ordering, nesting
# --------------------------------------------------------------------------- #


def test_note_block_text_is_escaped():
    html = render_interface(page({"type": "note_block",
                                  "params": {"text": "<script>alert(1)</script>"}}))
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_metric_key_is_escaped_inside_the_data_attribute():
    html = render_interface(page({"type": "metric_card",
                                  "params": {"metric_key": '"><script>x</script>'}}))
    assert "<script>" not in html


def test_tabs_render_one_panel_per_child():
    html = render_interface(one("tabs"), VALUES, AGGREGATES)
    assert html.count('class="wh-tabpanel"') == 2
    assert html.count('class="wh-tab') >= 2
    assert "<script" not in html          # no JavaScript for the tabs


def test_table_declares_its_columns_as_headers():
    html = render_interface(one("table"), VALUES, AGGREGATES)
    assert "<th>款号</th>" in html
    assert "<th>状态</th>" in html
    assert html.count("<tr>") == 1 + 2        # header row + two body rows


def test_spacer_and_divider_carry_no_readable_text():
    spacer = render_interface(one("spacer"), VALUES, AGGREGATES)
    assert re.sub(r"<[^>]+>", "", spacer).strip() == "渲染模块契约"
    divider = render_interface(page({"type": "divider", "params": {}}), VALUES, AGGREGATES)
    assert re.sub(r"<[^>]+>", "", divider).strip() == "渲染模块契约"


def test_children_render_in_declared_order():
    tree = {"type": "rows", "children": [
        {"type": "note_block", "params": {"text": "第一"}},
        {"type": "note_block", "params": {"text": "第二"}},
    ]}
    html = render_interface(page(tree))
    assert html.index("第一") < html.index("第二")


def test_two_container_levels_are_allowed():
    # Contract §1 / concept §3.3: two levels of layout, no more.
    tree = {"type": "columns", "params": {"columns": 2}, "children": [
        {"type": "card", "params": {"title": "内层"}, "children": [MODULES["progress_row"]]},
    ]}
    html = render_interface(page(tree), VALUES, AGGREGATES)
    assert "内层" in html
    assert 'data-type="progress_row"' in html


def test_a_leaf_may_not_grow_children():
    bad = page({"type": "note_block", "params": {"text": "x"},
                "children": [{"type": "note_block", "params": {"text": "y"}}]})
    with pytest.raises(RenderError) as exc:
        render_interface(bad)
    assert "MODULE_CANNOT_NEST" in str(exc.value)


def test_the_painter_guards_against_runaway_nesting():
    node: dict = {"type": "note_block", "params": {"text": "深"}}
    for _ in range(MAX_RENDER_DEPTH):
        node = {"type": "rows", "children": [node]}
    with pytest.raises(RenderError) as exc:
        render_interface(page(node))
    assert "LAYOUT_TOO_DEEP" in str(exc.value)


def test_an_unregistered_child_is_rejected_not_skipped():
    bad = page({"type": "rows", "children": [{"type": "magic", "key": "k"}]})
    with pytest.raises(RenderError) as exc:
        render_interface(bad)
    assert "magic" in str(exc.value)


# --------------------------------------------------------------------------- #
# The stylesheet that paints all of this
# --------------------------------------------------------------------------- #


CSS_PATH = Path(render.__file__).resolve().parent / "static" / "oams-modules.css"


def test_the_stylesheet_exists_and_is_namespaced():
    css = CSS_PATH.read_text(encoding="utf-8")
    assert ".wh-interface" in css
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)      # comments are not selectors
    selectors = [part.strip() for part in re.findall(r"(?:^|\})\s*([^{}@]+)\{", body)]
    classes = [sel for chunk in selectors for sel in chunk.split(",") if sel.strip()]
    assert classes, "no selectors parsed"
    strays = [sel for sel in classes if not re.match(r"^\s*\.wh-[a-z0-9-]+", sel)]
    assert strays == [], strays


def test_the_stylesheet_covers_every_class_the_templates_emit():
    # Structural classes must be painted. The two families that may go unstyled
    # are pure hooks that already exist as attributes on the same element:
    # wh-<module type> (mirrors data-type) and wh-interface-<mode>.
    hooks = {"wh-" + key.replace("_", "-") for key in ALL_KEYS}
    hooks |= {"wh-interface-" + mode for mode in render.MODES}
    css = CSS_PATH.read_text(encoding="utf-8")
    used: set[str] = set()
    for key in ALL_KEYS:
        for html in (render_interface(one(key), VALUES, AGGREGATES),
                     render_preview(one(key))):
            for match in re.findall(r'class="([^"]+)"', html):
                used |= {token for token in match.split() if token.startswith("wh-")}
    missing = sorted(token for token in used - hooks if f".{token}" not in css)
    assert missing == [], missing


# --------------------------------------------------------------------------- #
# Missing is not zero
# --------------------------------------------------------------------------- #


def test_progress_that_nobody_filled_in_reads_as_a_dash_not_zero():
    """没人填过的完成度不能读成「0% 完成」——那是一次假测量。

    进度只在该页被看的时候呈现（填报视角里是格子，没有百分比文字），
    所以这条走只读视角。
    """
    html = render_read_only(one("progress_row"), {}, {})
    assert "0%" not in html
    assert render.EMPTY in html
    assert "wh-bar-p0" not in html        # 也不要画一根零长度的轨道当测量结果


def test_progress_of_a_real_zero_is_still_zero():
    """真填了 0 就是 0：破折号只留给「没填」，不留给「填了零」。"""
    html = render_read_only(one("progress_row"), {"f_progress": "0"}, {})
    assert "0%" in html
    assert "wh-bar-p0" in html


def test_formatting_keeps_missing_and_zero_apart():
    assert render.format_number(None) == render.EMPTY
    assert render.format_number("") == render.EMPTY
    assert render.format_number(None, "款") == render.EMPTY
    assert render.format_number(0) == "0"
    assert render.format_number(0, "款") == "0 款"


def test_an_aggregate_of_zero_renders_as_zero_not_a_dash():
    """口子上的 0 是「这期真的是 0」，不是「没数据」——两种含义不能混。"""

    html = render_interface(one("metric_card"), {}, {"dept.DESIGN.sample_done": 0})
    values = re.findall(r'<span class="wh-metric-value">(.*?)</span>', html)
    assert values == ["0"], values
    assert render.EMPTY not in values
