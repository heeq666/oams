"""给外部 AI 的操作指南不能自己写坏——文档里的示例要能真过校验。

理由：这份指南是外部 AI 唯一的操作依据，它照抄示例里的东西。示例错了，它不会
怀疑示例，只会一轮轮撞同一堵墙；而文档是没人跑的东西，改了代码不回来改文档也
不会红。所以把文档里能被机器验的部分钉住：JSON 能不能解析、示例能不能过校验、
点名的模块与能力是不是真的存在。
"""

import json
import pathlib
import re

from oams.descriptor import (
    CAPABILITIES,
    UNIMPLEMENTED_CAPABILITIES,
    validate_descriptor,
)
from oams.modules import MODULE_REGISTRY

GUIDE = (
    pathlib.Path(__file__).resolve().parents[1]
    / "skills"
    / "oams-department-interface"
    / "SKILL.md"
)


def _text() -> str:
    return GUIDE.read_text(encoding="utf-8")


def _json_blocks() -> list[str]:
    return re.findall(r"```json\n(.*?)\n```", _text(), re.S)


def _sample() -> dict:
    blocks = [block for block in _json_blocks() if "schema_version" in block]
    assert len(blocks) == 1, "指南里应当恰好有一份完整的示例 descriptor"
    return json.loads(blocks[0])


def test_every_json_block_in_the_guide_parses() -> None:
    blocks = _json_blocks()
    assert blocks, "指南里应当带可复制的 JSON 片段"
    for block in blocks:
        json.loads(block)


def test_the_sample_descriptor_validates_clean() -> None:
    doc = _sample()
    problems = validate_descriptor(
        doc,
        known_org_unit_codes={doc["org_unit_code"]},
        known_position_codes=set(doc["position_codes"]),
    )
    assert problems == [], "指南里那份示例自己过不了校验：%s" % problems


def test_the_sample_leaves_department_marks_to_be_replaced() -> None:
    """示例必须是「换四样就能用」的形状：部门码、岗位码要从别处取，不能写死成真部门。"""
    doc = _sample()
    assert doc["org_unit_code"] == "OA", "示例部门码应当是个明摆着的占位"
    assert doc["meta"]["author_position_code"] in doc["position_codes"]


def _module_types(node: object) -> set[str]:
    """示例里用到的模块类型；字段的 "type"（text/enum/percent）不算。"""
    found: set[str] = set()
    if isinstance(node, dict):
        if isinstance(node.get("type"), str) and isinstance(node.get("key"), str):
            found.add(node["type"])
        for value in node.values():
            found |= _module_types(value)
    elif isinstance(node, list):
        for item in node:
            found |= _module_types(item)
    return found


def test_the_guide_only_names_modules_that_exist() -> None:
    named = _module_types(_sample()["modules"])
    unknown = named - set(MODULE_REGISTRY)
    assert unknown == set(), "指南点了不存在的模块：%s" % sorted(unknown)
    assert named, "示例里一个模块都没有？"


def test_the_guide_names_every_capability_it_is_allowed_to_use() -> None:
    """六条能用的能力都要交代清楚；词表外的一个都不许提。"""
    mentioned = set(re.findall(r"((?:entries|aggregate):[a-z:_]+)", _text()))
    unknown = mentioned - set(CAPABILITIES)
    assert unknown == set(), "指南提了词表外的能力：%s" % sorted(unknown)
    usable = set(CAPABILITIES) - set(UNIMPLEMENTED_CAPABILITIES)
    assert usable <= mentioned, "指南漏讲了这些能力：%s" % sorted(usable - mentioned)


def test_the_guide_tells_the_ai_not_to_write_the_unimplemented_capability() -> None:
    text = _text()
    for capability in UNIMPLEMENTED_CAPABILITIES:
        position = text.find(capability)
        assert position > 0, "指南没交代 %s 不能写" % capability
        near = text[position : position + 80]
        assert "别写" in near or "没有实现" in near, (
            "指南提到 %s 却没说明 v1 不实现，外部 AI 会照写" % capability
        )
