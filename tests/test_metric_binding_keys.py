"""指标绑定不许静默忽略未知键（2026-11-02）。

真实踩点：负责人写了 `where: 紧急`（本意是 `where_status`），`validate_metric_binding`
只验 `from_field` / `agg` / `where_status` 三个键，剩下的一概不看——校验一声不响放过，
上线后这条指标数的是**全部**条目，却和写对的那条显示得一模一样。错的是名字，赔的是数。

同一条规矩在别处已经是明写的：模块节点上出现既不是声明参数、也不在固定键里的键，
`modules.py` 会 `DATA_RULE_VIOLATION` 报错，而不是忽略（契约 §1「参数写法」那段）。
指标绑定以前是那条规矩的例外，case 钉在这里。

旧树上的样子：第 1、2、3、4 条 problems 都与预期不符（未知键静默通过、where_status
只说英文元组），本文件先红；metrics.py 补上白名单与中文提示后转绿。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from oams.descriptor import validate_descriptor  # noqa: E402
from oams.metrics import validate_metric_binding  # noqa: E402

ORG = "PRODUCTION"

FIELDS = {
    "style_no": {"key": "style_no", "map_to": "title", "label": "款号"},
    "stage": {"key": "stage", "map_to": "status", "label": "打样状态"},
    "qty": {"key": "qty", "map_to": "quantity", "label": "件数"},
}

#: emit 绑定能写的键。数字与 `metrics.py` 的实现必须一致——用例是这条口径的看门人。
EMIT_KEYS = ("metric_key", "mode", "agg", "from_field", "where_status", "label", "unit")


def test_emit_binding_rejects_an_unknown_key_and_lists_the_supported_ones():
    problems = validate_metric_binding(
        {"metric_key": "dept.PRODUCTION.pending_count", "mode": "emit", "agg": "count",
         "from_field": "stage", "where": "紧急"},
        ORG, FIELDS,
    )
    assert problems, "未知键 where 被静默忽略了——这类错上线后只会算出错数，不会报错"
    text = "；".join(problems)
    assert "where" in text, "报错要点名是哪个键不认识"
    assert "不认识" in text or "未知" in text, f"要说人话，别只丢一个英文清单：{problems}"
    for key in EMIT_KEYS:
        assert key in text, f"报错要列出支持的键，缺 {key}：{problems}"
    # 最容易写错的那一个说清楚：状态条件写 where_status，且只收四个标准态。
    assert "where_status" in text
    for state in ("planned", "in_progress", "blocked", "done"):
        assert state in text


def test_display_binding_rejects_an_unknown_key():
    problems = validate_metric_binding(
        {"metric_key": "dept.PRODUCTION.qty_total", "mode": "display", "filter": "本周"},
        ORG, FIELDS, own_metrics={"dept.PRODUCTION.qty_total"},
    )
    assert problems, "display 绑定上的未知键也被静默忽略了"
    text = "；".join(problems)
    assert "filter" in text and ("不认识" in text or "未知" in text)
    assert "metric_key" in text and "mode" in text


def test_where_status_takes_the_four_standard_states_only():
    problems = validate_metric_binding(
        {"metric_key": "dept.PRODUCTION.pending_count", "mode": "emit", "agg": "count",
         "from_field": "stage", "where_status": "紧急"},
        ORG, FIELDS,
    )
    text = "；".join(problems)
    assert problems and "where_status" in text
    assert "四个标准态" in text, f"要说清它只收四个标准态，而不是丢一个元组：{problems}"
    for state in ("planned", "in_progress", "blocked", "done"):
        assert state in text
    # 部门自己的词要显式指回 status_map，否则读者只会去改字段名
    assert "status_map" in text


def test_a_clean_emit_binding_still_passes():
    assert validate_metric_binding(
        {"metric_key": "dept.PRODUCTION.pending_count", "mode": "emit", "agg": "count",
         "from_field": "stage", "where_status": "planned", "label": "待处理", "unit": "单"},
        ORG, FIELDS,
    ) == []


def test_a_clean_display_binding_still_passes():
    assert validate_metric_binding(
        {"metric_key": "dept.PRODUCTION.qty_total", "mode": "display"},
        ORG, FIELDS, own_metrics={"dept.PRODUCTION.qty_total"},
    ) == []


def test_the_descriptor_surfaces_the_unknown_key_in_the_review_report():
    """走一遍 validate_descriptor：外人看到的是 review 里的一条，不是 traceback。"""
    descriptor = {
        "schema_version": "0.2",
        "interface_id": "production-sampling",
        "name": "生产部打样进度",
        "org_unit_code": ORG,
        "position_codes": ["PRODUCTION_LEAD"],
        "capabilities": ["entries:write", "entries:read:dept", "aggregate:read:dept"],
        "modules": [{
            "key": "rows", "type": "progress_row", "module_version": "^1.0",
            "source_fields": ["style_no", "stage"],
        }],
        "fields": [
            {"key": "style_no", "label": "款号", "type": "text", "map_to": "title"},
            {"key": "stage", "label": "打样状态", "type": "enum", "map_to": "status",
             "options": ["待排产", "打样中", "已完成"]},
        ],
        "status_map": {"待排产": "planned", "打样中": "in_progress", "已完成": "done"},
        "metrics": [{"metric_key": "dept.PRODUCTION.pending_count", "mode": "emit",
                     "agg": "count", "from_field": "stage", "where": "紧急"}],
        "rules": {},
        "meta": {"visibility": "department", "review": {"status": "draft"}},
    }
    problems = validate_descriptor(
        descriptor, known_org_unit_codes={ORG}, known_position_codes={"PRODUCTION_LEAD"})
    assert problems, "整份 descriptor 校验没把未知键带出来"
    joined = "；".join(problems)
    assert "where" in joined and "where_status" in joined
