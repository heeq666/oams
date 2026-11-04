"""模块参数是「声明」，形状不对就该在校验阶段被拦下。

一次真实的翻车：外部 AI 把 `table` 的 `params.columns` 写成一串字段名
（`["title","room"]`），注册表声明的是 `list[{key,label}]`。`plugin check` 放过、
审核放过，上线后第一次打开就 500：`'str object' has no attribute 'get'`。
渲染层后来学了兜底（`_as_columns` 按字段名补标签），但兜底不是契约——校验必须教人写对。

这里钉三层：
1. 每个注册模块、每种声明类型，坏形状都报中文，且指出是哪个模块的哪个参数；
2. `validate_descriptor`（`plugin check` 与 `oams_check_descriptor` 走的那条）也带出同样的句子；
3. 审核页在签之前就说「预览打不开 + 原因」，不让人盲签。
"""

from __future__ import annotations

import copy
import json
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import conftest as suite  # noqa: E402
import test_connected_apps as edges  # noqa: E402

from oams.descriptor import validate_descriptor  # noqa: E402
from oams.modules import MODULE_REGISTRY, validate_module_tree  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
SAMPLE = json.loads(
    (REPO / "data" / "neutral-demo-ga-parcel-lookup.json").read_text(encoding="utf-8")
)


def _bad_value(declaration: dict):
    """One value that is wrong for this declaration, and only for it."""

    type_name = declaration.get("type")
    if type_name == "int":
        maximum = declaration.get("maximum")
        if maximum is not None:
            return maximum + 1          # 越界，比「类型错」更像真人会写的东西
        minimum = declaration.get("minimum")
        if minimum is not None:
            return minimum - 1
        return "三"                     # 类型错：写成中文数字
    if type_name == "str":
        return ["一段", "文字"]
    if type_name == "list[str]":
        return "title,room"             # 一整个字符串，而不是一组
    if type_name == "list[{key,label}]":
        return ["title", "room"]        # 就是那次翻车的写法
    return None


def _node(spec, param_name: str, value) -> dict:
    module = {"key": "m1", "type": spec.key, "params": {param_name: value}}
    if spec.layer == "container":
        module["children"] = [{"key": "leaf", "type": "note_block",
                               "params": {"text": "x"}}]
    return module


# ---------------------------------------------------------------------------
# ① 每个注册模块、每种声明类型
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module_key,param_name",
    [(key, name) for key, spec in MODULE_REGISTRY.items() for name in spec.params],
)
def test_every_declared_param_refuses_a_wrong_shape(module_key, param_name):
    spec = MODULE_REGISTRY[module_key]
    value = _bad_value(spec.params[param_name])
    assert value is not None, f"{module_key}.{param_name} 没有对应的坏样本"

    problems = validate_module_tree([_node(spec, param_name, value)])
    assert problems, f"{module_key}.{param_name} = {value!r} 被静默放过了"
    joined = "\n".join(problems)
    assert "[DATA_RULE_VIOLATION]" in joined
    assert param_name in joined, "要指出是哪个参数"
    assert spec.name in joined, "要用中文模块名，人看得懂"


# ---------------------------------------------------------------------------
# ② 三份真人写过的坏样本：走完整的 validate_descriptor
# ---------------------------------------------------------------------------


def _with_modules(*modules) -> dict:
    descriptor = copy.deepcopy(SAMPLE)
    descriptor["modules"] = list(modules)
    return descriptor


def test_table_columns_as_bare_field_names_names_the_shape_to_write():
    problems = validate_descriptor(
        _with_modules({"key": "t", "type": "table",
                       "params": {"columns": ["title", "room"]}}))
    text = "\n".join(problems)
    assert "{key,label}" in text
    assert "字符串 'title'" in text
    assert "按字段名补标签" in text, "要说清渲染层会兜底，但这不是契约"


def test_metric_grid_keys_as_one_string_is_refused():
    problems = validate_descriptor(
        _with_modules({"key": "g", "type": "metric_grid",
                       "params": {"metric_keys": "dept.GA.pending"}}))
    text = "\n".join(problems)
    assert "指标网格" in text and "metric_keys" in text
    assert "一组文字" in text


def test_status_chart_dimension_must_be_text():
    problems = validate_descriptor(
        _with_modules({"key": "s", "type": "status_chart",
                       "params": {"dimension": ["state"]}}))
    text = "\n".join(problems)
    assert "状态分布" in text and "dimension" in text


def test_table_rows_out_of_range_says_the_limit():
    problems = validate_descriptor(
        _with_modules({"key": "t", "type": "table",
                       "params": {"columns": [{"key": "title", "label": "事项"}],
                                  "rows": 9}}))
    assert any("最多 5" in problem for problem in problems), problems


def test_the_flat_shape_is_validated_like_the_nested_one():
    """`rows: 9` 写在节点上（不是 params 里）也是这个参数，同样要拦。"""
    problems = validate_module_tree([{"key": "t", "type": "table", "rows": 9,
                                      "params": {"columns": [{"key": "title"}]}}])
    assert any("rows" in problem and "最多 5" in problem for problem in problems), problems


def test_a_correct_descriptor_still_passes():
    assert validate_module_tree(SAMPLE["modules"]) == []
    assert not [p for p in validate_descriptor(SAMPLE) or []
                if "columns" in p or "metric_keys" in p]


# ---------------------------------------------------------------------------
# ③ 审核页体检：签之前就知道这页打不开
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def world(db_ready):
    import psycopg

    name, url = suite._create_database(db_ready)
    try:
        suite._cli(["db", "init"], url)
        suite._cli(
            ["bootstrap-admin", "--name", "管理员", "--employee-code", "ADMIN001",
             "--org-code", "COMPANY", "--org-name", "公司"], url)
        with psycopg.connect(url, autocommit=True) as conn:
            admin_id = conn.execute(
                "SELECT member_id FROM oams.workforce_members WHERE employee_code = 'ADMIN001'"
            ).fetchone()[0]
            # 作者不能签自己的页，所以待审页的作者得是别人：审核人（管理员）只看不写
            author_id = conn.execute(
                """INSERT INTO oams.workforce_members (employee_code, display_name, employment_status)
                   VALUES ('HEALTH01', '写页的人', 'active') RETURNING member_id"""
            ).fetchone()[0]
        with suite.serve(url) as base:
            yield {"url": url, "base": base, "admin_member_id": str(admin_id),
                   "author_member_id": str(author_id)}
    finally:
        suite._drop_database(db_ready, name)


def _insert_pending(world, descriptor: dict) -> str:
    """直接写一行待审页——模拟「校验还没这么严的时候进库的老页」。"""

    import psycopg

    with psycopg.connect(world["url"], autocommit=True) as conn:
        row = conn.execute(
            """INSERT INTO oams.interfaces
                   (org_unit_id, slug, name, status, schema_version, descriptor,
                    created_by, updated_by)
               SELECT org_unit_id, %s, '体检用页', 'pending', '0.2', %s, %s, %s
                 FROM oams.org_units WHERE org_code = 'COMPANY'
               RETURNING interface_id""",
            ("health-" + uuid.uuid4().hex[:6], json.dumps(descriptor),
             world["author_member_id"], world["author_member_id"])).fetchone()
    return str(row[0])


def test_the_review_card_warns_before_anybody_signs(world):
    broken = copy.deepcopy(SAMPLE)
    # 渲染器对这一种真的会抛（list 里混了数字），不是我们能兜住的写法
    broken["modules"] = [{"key": "g", "type": "metric_grid",
                          "params": {"metric_keys": [1]}}]
    _insert_pending(world, broken)
    healthy = _insert_pending(world, copy.deepcopy(SAMPLE))

    human = edges._signed_in(world)
    status, page = human.get("/reviews")
    assert status == 200

    assert "预览打不开" in page, "签之前就该看见这一行"
    assert "先别签" in page
    # 健康的那一页不许被连坐：两行都出现才说明是按卡算的
    assert page.count("预览打不开") == 1, page.count("预览打不开")
    assert "TypeError" in page, "原因要说到可查的程度，不是「未知错误」"
