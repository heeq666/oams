"""schema 0.1 -> 0.2 升级器的测试。

背景：0.1 的 11 个旧模块类型在 0.2 渲染器上必然抛 RenderError，而
validate_descriptor 对 0.1 文档走旧分支照样返回 []，两套口径不一致。
这个文件盯的就是「转换后既过校验、又能真的渲染出 HTML」。

全部是纯函数测试：不连数据库、不读网络。
"""
import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from workhub.descriptor import validate_descriptor
from workhub.plugin_cli import main as plugin_main
from workhub.render import render_read_only
from workhub.upgrade import (
    CURRENT_SCHEMA_VERSION,
    LEGACY_SCHEMA_VERSION,
    MODULE_MAP,
    upgrade_descriptor,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "descriptor_v01_design_sampling.json"
LIVE_DATA = REPO_ROOT / "data" / "live-design-interface.json"


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def with_modules(modules: list[dict], **overrides) -> dict:
    """拿真实 fixture 换掉 modules，构造各种 0.1 形状的输入。

    fields / status_map / rules / meta 不动 —— 它们本来就能满足 title/status/
    evidence 三条聚合要求，这样每个用例只差在模块上。
    """
    descriptor = load_fixture()
    descriptor["modules"] = modules
    descriptor.update(overrides)
    return descriptor


def types_of(descriptor: dict) -> list[str]:
    return [module["type"] for module in descriptor["modules"]]


# --------------------------------------------------------------------------- #
# 真实 fixture：库里的那份 design-sampling
# --------------------------------------------------------------------------- #


def test_fixture_is_schema_0_1():
    assert load_fixture()["schema_version"] == LEGACY_SCHEMA_VERSION


def test_fixture_upgrades_to_schema_0_2():
    upgraded, _hints = upgrade_descriptor(load_fixture())
    assert upgraded["schema_version"] == CURRENT_SCHEMA_VERSION


def test_upgraded_fixture_passes_validate_descriptor():
    upgraded, _hints = upgrade_descriptor(load_fixture())
    assert validate_descriptor(upgraded) == []


def test_upgraded_fixture_renders_without_raising():
    upgraded, _hints = upgrade_descriptor(load_fixture())
    html = render_read_only(upgraded)
    assert isinstance(html, str)
    assert html.strip()


def test_upgraded_fixture_contains_no_legacy_module_type():
    upgraded, _hints = upgrade_descriptor(load_fixture())
    legal = {"progress_row", "evidence_gallery", "status_chart", "note_block", "timeline"}
    assert set(types_of(upgraded)) <= legal
    for module in upgraded["modules"]:
        assert module["type"] not in ("item_card", "progress_bar", "evidence_input",
                                      "list_block", "blocked_card", "metric_card")


def test_upgraded_fixture_reports_success_hint():
    _upgraded, hints = upgrade_descriptor(load_fixture())
    assert any("已通过 validate_descriptor" in hint for hint in hints)


def test_input_descriptor_is_not_mutated():
    original = load_fixture()
    snapshot = copy.deepcopy(original)
    upgrade_descriptor(original)
    assert original == snapshot
    assert original["schema_version"] == LEGACY_SCHEMA_VERSION


# --------------------------------------------------------------------------- #
# 每个 0.1 类型都要有明确归属
# --------------------------------------------------------------------------- #


def test_every_module_map_value_is_a_registered_v02_module():
    from workhub.modules import get_module

    for legacy, target in MODULE_MAP.items():
        spec = get_module(target)
        assert spec is not None, f"{legacy} -> {target} 不是注册过的 0.2 模块"
        assert spec.key == target


def test_item_card_becomes_progress_row():
    upgraded, hints = upgrade_descriptor(with_modules([
        {"key": "item", "type": "item_card", "order": 1, "title": "事项",
         "source_fields": ["title"], "aggregatable": True},
    ]))
    assert [m["type"] for m in upgraded["modules"]] == ["progress_row"]
    assert upgraded["modules"][0]["source_fields"] == ["title"]
    assert any("item_card -> progress_row" in hint for hint in hints)


def test_item_card_drops_legacy_title_key():
    upgraded, _hints = upgrade_descriptor(with_modules([
        {"key": "item", "type": "item_card", "order": 1, "title": "事项",
         "source_fields": ["title"], "aggregatable": True},
    ]))
    # 0.1 的卡片标题不是合法的 0.2 模块键，必须在转换时去掉。
    assert "title" not in upgraded["modules"][0]
    assert validate_descriptor(upgraded) == []


def test_progress_bar_merges_into_preceding_progress_row():
    upgraded, hints = upgrade_descriptor(with_modules([
        {"key": "item", "type": "item_card", "order": 1, "title": "事项",
         "source_fields": ["title"], "aggregatable": True},
        {"key": "progress", "type": "progress_bar", "order": 2, "title": "进度",
         "source_fields": ["sample_done"], "aggregatable": True},
    ]))
    assert [m["type"] for m in upgraded["modules"]] == ["progress_row"]
    assert upgraded["modules"][0]["source_fields"] == ["title", "sample_done"]
    assert any("并入同组的 progress_row" in hint for hint in hints)


def test_progress_bar_without_a_row_becomes_its_own_row():
    upgraded, hints = upgrade_descriptor(with_modules([
        {"key": "progress", "type": "progress_bar", "order": 1, "title": "进度",
         "source_fields": ["sample_done"], "aggregatable": True},
    ]))
    assert [m["type"] for m in upgraded["modules"]] == ["progress_row"]
    assert any("自成一行" in hint for hint in hints)


def test_item_card_without_a_title_field_warns_about_the_merge():
    # 0.1 里第二张 item_card 常常是「这次做了什么」，它没有 title 字段，
    # 单独成行会多出一行空标题；工具不猜，只提醒人并。
    upgraded, hints = upgrade_descriptor(with_modules([
        {"key": "item", "type": "item_card", "order": 1, "title": "事项",
         "source_fields": ["title"], "aggregatable": True},
        {"key": "note", "type": "item_card", "order": 2, "title": "这次做了什么",
         "source_fields": ["fabric_note"], "aggregatable": True},
    ]))
    assert [m["key"] for m in upgraded["modules"]] == ["item", "note"]
    assert any("没有映射到 title 的字段" in hint for hint in hints)
    assert validate_descriptor(upgraded) == []


def test_evidence_input_becomes_evidence_gallery():
    upgraded, hints = upgrade_descriptor(with_modules([
        {"key": "evidence", "type": "evidence_input", "order": 1, "title": "依据",
         "source_fields": ["evidence_ref"], "aggregatable": True},
    ]))
    assert types_of(upgraded) == ["evidence_gallery"]
    assert any("evidence_input -> evidence_gallery" in hint for hint in hints)


def test_list_block_becomes_note_block_and_warns():
    upgraded, hints = upgrade_descriptor(with_modules([
        {"key": "private", "type": "list_block", "order": 1, "title": "面料备注",
         "source_fields": ["fabric_note"], "aggregatable": False},
    ]))
    node = upgraded["modules"][0]
    assert node["type"] == "note_block"
    assert node["params"]["text"] == "面料备注"
    # aggregatable=False 是「仅本部门可见」，必须原样保留。
    assert node["aggregatable"] is False
    assert any("list_block 在 0.2 没有动态对应模块" in hint for hint in hints)
    assert validate_descriptor(upgraded) == []


def test_blocked_card_becomes_blocker_list():
    upgraded, hints = upgrade_descriptor(with_modules([
        {"key": "blocked", "type": "blocked_card", "order": 1, "title": "卡在哪",
         "source_fields": ["blocked_reason"], "aggregatable": True},
    ]))
    assert types_of(upgraded) == ["blocker_list"]
    assert upgraded["modules"][0]["source_fields"] == ["blocked_reason"]
    assert any("blocked_card -> blocker_list" in hint for hint in hints)


def test_timeline_stays_timeline():
    upgraded, _hints = upgrade_descriptor(with_modules([
        {"key": "trace", "type": "timeline", "order": 1, "title": "轨迹",
         "source_fields": ["title"], "aggregatable": True},
    ]))
    assert types_of(upgraded) == ["timeline"]
    assert "title" not in upgraded["modules"][0]
    assert validate_descriptor(upgraded) == []


# --------------------------------------------------------------------------- #
# metric_card 的特殊处理：它能变两种东西，绝不被静默当成指标卡
# --------------------------------------------------------------------------- #


def test_metric_card_with_status_field_becomes_status_chart():
    upgraded, hints = upgrade_descriptor(load_fixture())
    state = next(m for m in upgraded["modules"] if m["key"] == "state")
    assert state["type"] == "status_chart"
    assert state["params"]["dimension"] == "status"
    assert any("0.1 的 metric_card 表示「状态展示」" in hint for hint in hints)


def test_metric_card_without_status_field_becomes_note_block():
    upgraded, hints = upgrade_descriptor(with_modules([
        {"key": "state", "type": "metric_card", "order": 1, "title": "打样状态",
         "source_fields": ["fabric_note"], "aggregatable": True},
    ]))
    node = upgraded["modules"][0]
    assert node["type"] == "note_block"
    assert node["params"]["text"] == "打样状态"
    assert any("不是 status" in hint and "请人工确认" in hint for hint in hints)


def test_metric_card_never_silently_becomes_metric_card():
    # 0.1 的 metric_card 是状态展示；静默转成指标卡等于给部门看别人的数字。
    for modules in (
        [{"key": "state", "type": "metric_card", "order": 1, "title": "打样状态",
          "source_fields": ["state_word"], "aggregatable": True}],
        [{"key": "state", "type": "metric_card", "order": 1, "title": "打样状态",
          "source_fields": ["fabric_note"], "aggregatable": True}],
    ):
        upgraded, hints = upgrade_descriptor(with_modules(modules))
        assert types_of(upgraded) != ["metric_card"]
        assert any("请人工确认" in hint or "如果它其实是公司指标卡" in hint for hint in hints)


# --------------------------------------------------------------------------- #
# 未知类型：原样保留 + 提示，不抛异常
# --------------------------------------------------------------------------- #


def test_unknown_module_type_is_kept_with_a_hint():
    upgraded, hints = upgrade_descriptor(with_modules([
        {"key": "weird", "type": "custom_widget", "order": 1, "title": "自造块",
         "source_fields": ["title"], "aggregatable": True},
    ]))
    assert types_of(upgraded) == ["custom_widget"]
    assert any("无法自动转换，请手工改写" in hint for hint in hints)


def test_unknown_module_type_does_not_raise_even_though_check_fails():
    upgraded, hints = upgrade_descriptor(with_modules([
        {"key": "weird", "type": "custom_widget", "order": 1, "title": "自造块",
         "source_fields": ["title"]},
    ]))
    # 转换器不负责遮盖问题：校验过不了就把它写进提示行，而不是抛异常。
    problems = validate_descriptor(upgraded)
    assert problems
    assert any(hint.startswith("转换后仍有校验问题：") for hint in hints)


def test_unknown_module_does_not_block_the_rest():
    upgraded, _hints = upgrade_descriptor(with_modules([
        {"key": "weird", "type": "custom_widget", "order": 1, "title": "自造块",
         "source_fields": ["title"]},
        {"key": "evidence", "type": "evidence_input", "order": 2, "title": "依据",
         "source_fields": ["evidence_ref"], "aggregatable": True},
    ]))
    assert types_of(upgraded) == ["custom_widget", "evidence_gallery"]


# --------------------------------------------------------------------------- #
# 顶层键：capabilities / metrics
# --------------------------------------------------------------------------- #


def test_capabilities_are_added_and_lifted_from_the_frozen_set():
    from workhub.descriptor import CAPABILITIES

    upgraded, _hints = upgrade_descriptor(load_fixture())
    capabilities = upgraded["capabilities"]
    assert capabilities
    for capability in capabilities:
        assert capability in CAPABILITIES
    # 有录入模块就必须能写。
    assert "entries:write" in capabilities
    # 汇总审批要二级审批，工具不许自作主张。
    assert "aggregate:read:company" not in capabilities


def test_metrics_are_added_for_the_inferred_department_metric():
    upgraded, hints = upgrade_descriptor(load_fixture())
    assert upgraded["metrics"] == [{
        "metric_key": "dept.DESIGN.finished",
        "mode": "emit",
        "agg": "count",
        "from_field": "state_word",
        "where_status": "done",
    }]
    assert any("dept.DESIGN.finished" in hint for hint in hints)


def test_metrics_display_binding_is_inferred_from_a_metric_card_param():
    descriptor = with_modules([
        {"key": "state", "type": "metric_card", "order": 1, "title": "状态",
         "source_fields": ["state_word"], "aggregatable": True,
         "params": {"metric_key": "funnel.sample_done"}},
    ])
    upgraded, _hints = upgrade_descriptor(descriptor)
    assert {"metric_key": "funnel.sample_done", "mode": "display"} in upgraded["metrics"]


def test_no_status_field_means_no_emit_metric():
    descriptor = load_fixture()
    for field in descriptor["fields"]:
        if field["map_to"] == "status":
            field["map_to"] = "note"
    upgraded, _hints = upgrade_descriptor(descriptor)
    assert all(binding["mode"] != "emit" for binding in upgraded["metrics"])


# --------------------------------------------------------------------------- #
# 边界输入
# --------------------------------------------------------------------------- #


def test_already_v02_descriptor_is_returned_unchanged():
    descriptor = json.loads(LIVE_DATA.read_text(encoding="utf-8"))
    assert descriptor["schema_version"] == CURRENT_SCHEMA_VERSION
    upgraded, hints = upgrade_descriptor(descriptor)
    assert upgraded == descriptor
    assert any("没有做任何转换" in hint for hint in hints)


def test_empty_module_list_does_not_raise():
    upgraded, hints = upgrade_descriptor(with_modules([]))
    assert upgraded["modules"] == []
    # 没有模块就没有 title/status/evidence 的生产者，校验会说清楚，但不抛异常。
    assert any(hint.startswith("转换后仍有校验问题：") for hint in hints)


def test_non_dict_input_is_rejected():
    with pytest.raises(TypeError):
        upgrade_descriptor([])


# --------------------------------------------------------------------------- #
# 仓库里的演示数据与 CLI
# --------------------------------------------------------------------------- #


def test_live_data_file_is_already_migrated():
    descriptor = json.loads(LIVE_DATA.read_text(encoding="utf-8"))
    assert descriptor["schema_version"] == CURRENT_SCHEMA_VERSION
    assert validate_descriptor(descriptor) == []
    assert render_read_only(descriptor)


def test_live_data_file_has_no_legacy_types():
    descriptor = json.loads(LIVE_DATA.read_text(encoding="utf-8"))
    legacy_types = set(MODULE_MAP) | {"metric_card"}
    assert not set(types_of(descriptor)) & legacy_types


def test_plugin_cli_upgrade_writes_a_renderable_descriptor(tmp_path, capsys):
    out = tmp_path / "upgraded.json"
    code = plugin_main(["upgrade", str(FIXTURE), "--out", str(out)])
    assert code == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["schema_version"] == CURRENT_SCHEMA_VERSION
    assert validate_descriptor(written) == []
    assert render_read_only(written)
    printed = capsys.readouterr().out
    assert "需要人工确认" in printed


def test_plugin_cli_upgrade_json_outputs_hints(tmp_path, capsys):
    code = plugin_main(["upgrade", str(FIXTURE), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["descriptor"]["schema_version"] == CURRENT_SCHEMA_VERSION
    assert payload["hints"]


def test_plugin_cli_upgrade_reports_a_missing_file(capsys):
    code = plugin_main(["upgrade", "tests/fixtures/does-not-exist.json"])
    assert code == 2
    assert "文件不存在" in capsys.readouterr().err
