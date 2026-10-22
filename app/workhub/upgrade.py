"""把 schema 0.1 的部门界面描述升级到 schema 0.2。

为什么需要这个工具：0.1 与 0.2 的外层形状几乎一样，变的是模块词汇表。
0.1 的 11 个旧类型（item_card / progress_bar / blocked_card / ...）在 0.2 里作废，
渲染器只认 MODULE_REGISTRY 里的 20 个 key，所以任何存量 0.1 页面在 0.2 渲染器上
都会直接抛 RenderError(MODULE_NOT_WHITELISTED)，而 validate_descriptor 对 0.1 文档
走旧分支、照旧返回 []，两套口径不一致。这个模块负责把存量文档搬到新词汇表上。

    from workhub.upgrade import upgrade_descriptor
    upgraded, hints = upgrade_descriptor(old_descriptor)

行为约定：
  * 纯函数。入参被深拷贝，绝不就地修改；不连数据库、不读写文件、不发网络请求。
  * 不抛异常。转换不了的地方（未知模块类型、残缺字段）原样保留，并把要做的事写在
    返回的提示行里——工具负责搬能搬的，人负责拍板语义。
  * 返回 (描述, 提示行)。提示行中文、一条一句、可以直接打印给部门管理员看。
  * 转换后会自检 validate_descriptor；过不了不抛异常，而是把每条问题也写成提示行。

## 固定模块映射表（冻结，改动要先过 Lead）

  item_card      -> progress_row           一个事项 = 一行录入
  progress_bar   -> progress_row 的进度槽   同组已有 progress_row 时并入其 source_fields，
                                           不新起一行；否则自成一行
  evidence_input -> evidence_gallery       证据录入
  list_block     -> note_block             0.1 的列表框在 0.2 没有动态对应模块；
                                           降级为静态文字并提示人工改写
  blocked_card   -> blocker_list           阻塞原因 + 待决事项
  timeline       -> timeline               同名同义，原样保留
  metric_card    -> status_chart / note_block
                 特殊处理：0.1 的 metric_card 语义是「状态展示」，不是公司指标卡。
                 只有它引用的字段 map_to 是 status 时才转 status_chart，否则转
                 note_block；两种情况都出提示行，绝不静默当成指标卡
  其它（custom 等）原样保留，出提示行「无法自动转换，请手工改写」

## 自动补齐的顶层键

  * schema_version 改为 "0.2"。
  * capabilities：有 input 层模块 -> entries:write；有指标型展示模块（metric_card /
    metric_grid / flow_strip / rank_list）-> aggregate:read:dept；恒有 entries:read:dept。
    不会自动给 aggregate:read:company——那需要二级审批。
  * metrics：模块 params 里引用到的 platform 指标 -> mode=display 绑定；若本界面有
    map_to=status 的字段且 status_map 里有词映射到 done，补一条部门指标
    dept.<CODE>.finished（count，where_status=done），并出提示行。

## 字段与模块上遗留的键

0.1 的模块节点带 title 与 aggregatable 两个键，0.2 的模块只能有 key/type/order/
children/params/module_version（容器）或 key/type/order/params/source_fields（叶子）。
转换时 title 丢弃（0.1 的卡片标题语义已由模块自身模板承担；降级成 note_block 时
会把标题用作 params.text），aggregatable: false 保留——渲染器用它实现「仅本部门
可见、不进公司汇总」，丢了会改变可见性。
"""
from __future__ import annotations

import copy
from typing import Any

from workhub.descriptor import CAPABILITIES, validate_descriptor
from workhub.metrics import get_metric
from workhub.modules import get_module

CURRENT_SCHEMA_VERSION = "0.2"
LEGACY_SCHEMA_VERSION = "0.1"

#: 0.1 类型 -> 0.2 模块 key。只收录语义能对上的；表外的类型原样保留并提示。
MODULE_MAP: dict[str, str] = {
    "item_card": "progress_row",
    "progress_bar": "progress_row",
    "evidence_input": "evidence_gallery",
    "list_block": "note_block",
    "blocked_card": "blocker_list",
    "timeline": "timeline",
}

#: 有这些 0.2 展示模块时，界面要读部门级汇总。
_AGGREGATE_DISPLAY_MODULES = ("metric_card", "metric_grid", "flow_strip", "rank_list")


def upgrade_descriptor(descriptor: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """把一份 0.1 描述转成 0.2，返回 (新描述, 需要人工确认的提示行)。

    入参不会被修改。已经是一份 0.2 描述时原样深拷贝返回，只给一行说明。
    """
    if not isinstance(descriptor, dict):
        raise TypeError("descriptor must be a dict")

    upgraded = copy.deepcopy(descriptor)
    hints: list[str] = []

    if upgraded.get("schema_version") == CURRENT_SCHEMA_VERSION:
        hints.append(
            f"schema_version 已经是 {CURRENT_SCHEMA_VERSION!r}，没有做任何转换；"
            "如仍打不开页面，请直接跑 workhub plugin check 看具体问题。"
        )
        return upgraded, hints

    fields = upgraded.get("fields")
    fields_by_key = _fields_by_key(fields)

    modules = upgraded.get("modules")
    legacy_modules = copy.deepcopy(modules) if isinstance(modules, list) else []
    if isinstance(modules, list):
        upgraded["modules"] = _convert_modules(modules, fields_by_key, hints)
    else:
        hints.append("modules 缺失或不是列表，无法转换模块；请补齐后重新升级。")
        upgraded["modules"] = []

    upgraded["schema_version"] = CURRENT_SCHEMA_VERSION

    upgraded["capabilities"] = _infer_capabilities(upgraded)

    metrics, metric_hints = _infer_metrics(upgraded, fields_by_key, legacy_modules)
    upgraded["metrics"] = metrics
    hints.extend(metric_hints)

    hints.extend(_legacy_field_hints(fields))

    problems = validate_descriptor(upgraded)
    for problem in problems:
        hints.append(f"转换后仍有校验问题：{problem}")
    if not problems:
        hints.append("转换完成：新描述已通过 validate_descriptor（严格 0.2 口径）。")

    return upgraded, hints


# --------------------------------------------------------------------------- #
# Modules
# --------------------------------------------------------------------------- #


def _convert_modules(modules: list[Any], fields_by_key: dict[str, dict],
                     hints: list[str]) -> list[dict[str, Any]]:
    """按顺序转换一层模块；同层的 progress_bar 并入它前面的 progress_row。"""
    converted: list[Any] = []
    for index, module in enumerate(modules):
        if not isinstance(module, dict):
            hints.append(f"modules[{index}]：不是对象，已原样保留，请手工删除或改写。")
            converted.append(module)
            continue

        key = _module_key(module, index)
        mtype = module.get("type")
        sources = _sources(module)

        if mtype == "progress_bar":
            target = _last_progress_row(converted)
            if target is not None:
                _merge_sources(target, sources)
                hints.append(
                    f"modules[{key}]：progress_bar 的进度槽已并入同组的 "
                    f"progress_row[{target.get('key')}]"
                    "（0.2 里进度是进度行的一个槽，不是独立模块）。"
                )
                continue
            node = _build("progress_row", key, module, sources)
            hints.append(
                f"modules[{key}]：没有可并入的 progress_row，progress_bar 已自成一行；"
                "如果它本该属于某个事项行，请手工把它们并到同一行。"
            )
            _place(converted, node, module, fields_by_key, hints)
            continue

        if mtype == "metric_card":
            node, note = _convert_metric_card(key, module, sources, fields_by_key)
            hints.append(note)
            _place(converted, node, module, fields_by_key, hints)
            continue

        if mtype == "list_block":
            node, note = _convert_list_block(key, module, fields_by_key)
            hints.append(note)
            _place(converted, node, module, fields_by_key, hints)
            continue

        target_type = MODULE_MAP.get(mtype)
        if target_type is None:
            if get_module(mtype) is not None:
                # 已经是合法的 0.2 模块（例如同名的 timeline），原样保留。
                converted.append(copy.deepcopy(module))
                continue
            hints.append(
                f"modules[{key}]：类型 {mtype!r} 没有 v0.2 对应模块，已原样保留；"
                "无法自动转换，请手工改写（例如 progress_row / blocker_list / table / note_block）。"
            )
            converted.append(copy.deepcopy(module))
            continue

        node = _build(target_type, key, module, sources)
        if target_type != mtype:
            hints.append(f"modules[{key}]：{mtype} -> {target_type}。")
        if target_type == "progress_row" and not _has_title(sources, fields_by_key):
            # 一个不带标题的事项卡单独成行会让部门看到两行「空标题」，
            # 提醒人工判断它是不是本该并进上一行（工具不去猜）。
            hints.append(
                f"modules[{key}]：这张卡没有映射到 title 的字段，转换后是一行没有标题的事项；"
                "如果它属于上一个进度行，请手工把 source_fields 并进那一行。"
            )
        _place(converted, node, module, fields_by_key, hints)

    return converted


def _convert_metric_card(key: str, module: dict, sources: list[str],
                         fields_by_key: dict[str, dict]) -> tuple[dict, str]:
    """0.1 的 metric_card 是「状态展示」，不是公司指标卡。

    只有明确引用了 map_to=status 的字段才转 status_chart；其余转 note_block。
    两种情况都要求人工确认，不许静默猜成指标卡——猜错会让部门看到别人的数字。
    """
    mapped = [fields_by_key[s].get("map_to") for s in sources if s in fields_by_key]
    if "status" in mapped:
        node = _build("status_chart", key, module, sources, params={"dimension": "status"})
        note = (
            f"modules[{key}]：0.1 的 metric_card 表示「状态展示」，已转为 status_chart"
            "（按状态分布画条）。如果它其实是公司指标卡，请手工改回 metric_card，并在 "
            "params.metric_key 写上平台指标（如 funnel.sample_done）。"
        )
        return node, note

    text = _text_for(module, sources, fields_by_key, fallback=key)
    node = _build("note_block", key, module, [], params={"text": text})
    note = (
        f"modules[{key}]：0.1 的 metric_card 引用字段的 map_to 不是 status，已转为 note_block"
        f"（静态文字「{text}」）而不是指标卡。请人工确认它到底要展示什么："
        "公司指标请用 metric_card + params.metric_key，部门自己的数请用 metric_input。"
    )
    return node, note


def _convert_list_block(key: str, module: dict,
                        fields_by_key: dict[str, dict]) -> tuple[dict, str]:
    text = _text_for(module, _sources(module), fields_by_key, fallback=key)
    node = _build("note_block", key, module, [], params={"text": text})
    note = (
        f"modules[{key}]：list_block 在 0.2 没有动态对应模块，已降级为 note_block"
        f"（只显示静态文字「{text}」）。原来的列表数据不再随条目录入，"
        "需要动态列表请手工改用 table 或 progress_row。"
    )
    return node, note


def _place(converted: list, node: dict, module: dict,
           fields_by_key: dict[str, dict], hints: list[str]) -> None:
    """把转换好的节点放进结果；0.1 若带了 children，尽量一起搬过去。"""
    children = module.get("children")
    if isinstance(children, list) and children:
        spec = get_module(node.get("type"))
        inner = _convert_modules(children, fields_by_key, hints)
        if spec is not None and spec.layer == "container":
            node["children"] = inner
        else:
            converted.append(node)
            converted.extend(inner)
            return
    converted.append(node)


def _build(target_type: str, key: str, module: dict, sources: list[str],
           params: dict | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"key": key, "type": target_type}
    order = module.get("order")
    if isinstance(order, (int, float)):
        node["order"] = order
    if sources:
        node["source_fields"] = list(sources)
    if params:
        node["params"] = params
    # aggregatable=False 是「仅本部门可见、不进公司汇总」，必须保留。
    if module.get("aggregatable") is False:
        node["aggregatable"] = False
    return node


def _last_progress_row(converted: list) -> dict | None:
    for node in reversed(converted):
        if isinstance(node, dict) and node.get("type") == "progress_row":
            return node
    return None


def _merge_sources(target: dict, sources: list[str]) -> None:
    if not sources:
        return
    existing = target.setdefault("source_fields", [])
    for source in sources:
        if source not in existing:
            existing.append(source)


def _module_key(module: dict, index: int) -> str:
    key = module.get("key")
    if isinstance(key, str) and key.strip():
        return key
    mtype = module.get("type")
    return f"{mtype or 'module'}_{index + 1}"


def _sources(module: dict) -> list[str]:
    raw = module.get("source_fields")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str) and item.strip()]


def _text_for(module: dict, sources: list[str], fields_by_key: dict[str, dict],
              fallback: str) -> str:
    """给 note_block 找一段静态文字：模块标题 > 字段标题 > key。"""
    title = module.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    for source in sources:
        field = fields_by_key.get(source) or {}
        label = field.get("label")
        if isinstance(label, str) and label.strip():
            return label.strip()
    return fallback


def _has_title(sources: list[str], fields_by_key: dict[str, dict]) -> bool:
    return any(
        (fields_by_key.get(source) or {}).get("map_to") == "title" for source in sources
    )


def _fields_by_key(fields: Any) -> dict[str, dict]:
    if not isinstance(fields, list):
        return {}
    result: dict[str, dict] = {}
    for field in fields:
        if isinstance(field, dict) and isinstance(field.get("key"), str):
            result[field["key"]] = field
    return result


# --------------------------------------------------------------------------- #
# capabilities / metrics
# --------------------------------------------------------------------------- #


def _walk_modules(modules: Any) -> list[dict]:
    found: list[dict] = []
    if not isinstance(modules, list):
        return found
    for module in modules:
        if not isinstance(module, dict):
            continue
        found.append(module)
        children = module.get("children")
        if isinstance(children, list):
            found.extend(_walk_modules(children))
    return found


def _infer_capabilities(descriptor: dict) -> list[str]:
    """按模块推断能力声明。宁少不多：多给一条就是多一份授权。"""
    wanted = {"entries:read:dept"}
    for module in _walk_modules(descriptor.get("modules")):
        spec = get_module(module.get("type"))
        if spec is not None and spec.layer == "input":
            wanted.add("entries:write")
        if module.get("type") in _AGGREGATE_DISPLAY_MODULES:
            wanted.add("aggregate:read:dept")
    # 用冻结集合的顺序，让同样的输入永远生成同样的文件。
    return [capability for capability in CAPABILITIES if capability in wanted]


def _infer_metrics(descriptor: dict, fields_by_key: dict[str, dict],
                   legacy_modules: list | None = None) -> tuple[list[dict], list[str]]:
    metrics: list[dict] = []
    hints: list[str] = []
    seen: set[str] = set()

    # 0.1 的 metric_card 若本来就引用了平台指标，它转换时会丢掉 params，
    # 所以这里连原始模块一起扫，别把它的指标引用弄丢。
    candidates = _walk_modules(descriptor.get("modules")) + _walk_modules(legacy_modules)

    for module in candidates:
        params = module.get("params")
        if not isinstance(params, dict):
            continue
        for param_name in ("metric_key", "metric_keys", "metrics"):
            raw = params.get(param_name)
            if raw is None:
                continue
            refs = raw if isinstance(raw, list) else [raw]
            for ref in refs:
                if not isinstance(ref, str) or not ref.strip():
                    continue
                spec = get_metric(ref)
                if spec is None or spec.origin != "platform" or ref in seen:
                    continue
                seen.add(ref)
                metrics.append({"metric_key": ref, "mode": "display"})

    org_unit_code = descriptor.get("org_unit_code")
    status_field = next(
        (field for field in fields_by_key.values() if field.get("map_to") == "status"), None
    )
    status_map = descriptor.get("status_map")
    has_done = isinstance(status_map, dict) and "done" in status_map.values()
    if isinstance(org_unit_code, str) and org_unit_code and status_field and has_done:
        metric_key = f"dept.{org_unit_code}.finished"
        if metric_key not in seen:
            seen.add(metric_key)
            metrics.append({
                "metric_key": metric_key,
                "mode": "emit",
                "agg": "count",
                "from_field": status_field["key"],
                "where_status": "done",
            })
            hints.append(
                f"已按状态字段 {status_field['key']} 推断部门指标 {metric_key}"
                "（状态为 done 的条目数，agg=count）；口径不对请改 metrics[] 的 "
                "agg / where_status，或删掉这条绑定。"
            )
    return metrics, hints


def _legacy_field_hints(fields: Any) -> list[str]:
    """0.1 的 org_unit_id / position_id 在 0.2 词汇表里没有对应项。"""
    hints: list[str] = []
    if not isinstance(fields, list):
        return hints
    for field in fields:
        if not isinstance(field, dict):
            continue
        if field.get("map_to") in ("org_unit_id", "position_id"):
            hints.append(
                f"fields[{field.get('key')}]：map_to={field.get('map_to')!r} 是 0.1 词汇，"
                "0.2 没有对应字段（部门与岗位来自会话）；"
                "这条字段需要手工改成标准字段或 display_only。"
            )
    return hints


__all__ = ["CURRENT_SCHEMA_VERSION", "LEGACY_SCHEMA_VERSION", "MODULE_MAP", "upgrade_descriptor"]
