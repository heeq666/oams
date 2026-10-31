"""What changed between two versions of a department page, and why it was sent back.

A reviewer is asked to approve a diff, not a whole page.  Re-reading a
descriptor from scratch every time is how review turns into rubber-stamping:
the page looks fine, nothing stands out, approved.  The thing that actually
needs a human eye is the delta -- the field that quietly changed its summary
mapping, the status word that disappeared, the capability that was added.

So this module owns three things and nothing else:

1. diff_descriptors() -- a stable, human-readable list of changes between the
   version that is live and the version asking to replace it.
2. REJECTION_CODES -- the fixed vocabulary a reviewer sends a page back with,
   so a rejection carries a machine-readable reason instead of a sentence
   nobody can count.
3. stamp_approval() -- the one way "approved" gets written into a descriptor.
   Authors do not write it; the signature is copied in from the person who
   actually clicked, at the moment the page goes live.

Severity is not decoration: it is the answer to "does this change alter what the
company's numbers mean?".  A page that only renames a label is low; a page that
re-points a field from status to progress_value is high and must be read before
it is approved.

Nothing here touches the database; the callers hand in two descriptors.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

# --- severity ---------------------------------------------------------------

SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"

SEVERITY_ORDER = {SEVERITY_HIGH: 0, SEVERITY_MEDIUM: 1, SEVERITY_LOW: 2}
SEVERITY_LABELS = {
    SEVERITY_HIGH: "需注意",
    SEVERITY_MEDIUM: "一般",
    SEVERITY_LOW: "提示",
}

AREA_FIELDS = "字段"
AREA_STATUS = "状态"
AREA_METRICS = "指标"
AREA_MODULES = "模块"
AREA_CAPABILITY = "权限"
AREA_SCOPE = "范围"
AREA_BASIC = "基本信息"

# --- rejection vocabulary ---------------------------------------------------

#: The seven reasons a page comes back.  These are the same codes the validator
#: prefixes onto its problems, so a department reading the rejection and a
#: department running "workhub plugin check" see one vocabulary, not two.
REJECTION_CODES = (
    ("FIELD_UNMAPPED", "字段没接到标准字段上",
     "有字段既没映射到标准字段，也没标成 display_only，公司汇总时不知道它是什么。"),
    ("STATUS_UNMAPPED", "状态词没有映射",
     "部门的状态叫法没有落到 planned / in_progress / blocked / done 上，这部分工作进不了公司的进度口径。"),
    ("METRIC_UNDEFINED", "指标没有定义清楚",
     "页面声明了要产出某个指标，但没说从哪个字段、按什么方式汇总。"),
    ("CAPABILITY_ESCALATION", "权限范围超出约定",
     "这一版要了比上一版更大的能力（例如从只读部门变成可写、或者要对外公开）。"),
    ("MODULE_NOT_WHITELISTED", "用了不存在的模块",
     "模块类型不在平台的预设模块表里；容器只负责排版，不能自己造数据模块。"),
    ("LAYOUT_TOO_DEEP", "版式嵌套超过两层",
     "容器最多套两层，再深平台就没法保证各地都能正确渲染。"),
    ("DATA_RULE_VIOLATION", "数据规则不满足",
     "例如填报了平台派生字段（款级打样状态由商品数据中心的颜色级记录派生，部门不能自己填）。"),
)

REJECTION_LABELS = {code: label for code, label, _hint in REJECTION_CODES}
REJECTION_HINTS = {code: hint for code, _label, hint in REJECTION_CODES}
REJECTION_CHOICES = [{"code": code, "label": label, "hint": hint}
                     for code, label, hint in REJECTION_CODES]


def rejection_label(code: str | None) -> str:
    """Display name for a rejection code; unknown or empty codes stay readable."""
    if not code:
        return "未分类"
    return REJECTION_LABELS.get(code, code)


def is_rejection_code(code: str | None) -> bool:
    return bool(code) and code in REJECTION_LABELS


# --- formatting helpers -----------------------------------------------------

_MISSING = "(无)"


def _text(value: Any) -> str:
    """A short, display-safe rendering of any descriptor value."""
    if value is None:
        return _MISSING
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (list, tuple)):
        items = [_text(item) for item in value]
        return "、".join(items) if items else _MISSING
    if isinstance(value, Mapping):
        return "、".join(f"{key}={_text(value[key])}" for key in sorted(value, key=str))
    text = str(value).strip()
    return text or _MISSING


def _change(*, kind: str, area: str, severity: str, path: str, label: str,
            before: Any = None, after: Any = None, why: str = "") -> dict:
    return {
        "kind": kind,
        "area": area,
        "severity": severity,
        "severity_label": SEVERITY_LABELS[severity],
        "path": path,
        "label": label,
        "before": _text(before),
        "after": _text(after),
        "why": why,
    }


def _sorted_changes(changes: list[dict]) -> list[dict]:
    """Most alarming first, then by area, so the reviewer reads top-down."""
    return sorted(changes, key=lambda item: (
        SEVERITY_ORDER[item["severity"]], item["area"], item["path"]))


# --- descriptor pieces ------------------------------------------------------

def _index_by_key(items: Any, key_field: str = "key") -> dict[str, dict]:
    """Fields and modules are matched by their declared key, not by position.

    Position is not identity: inserting one module must not read as "every
    later module changed".
    """
    indexed: dict[str, dict] = {}
    if not isinstance(items, (list, tuple)):
        return indexed
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            continue
        key = item.get(key_field)
        if isinstance(key, str) and key.strip():
            indexed.setdefault(key.strip(), dict(item))
        else:
            indexed.setdefault(f"#{index}", dict(item))
    return indexed


def _flatten_modules(modules: Any, prefix: str = "modules") -> list[tuple[str, dict]]:
    """Depth-first walk of the module tree, containers included."""
    flat: list[tuple[str, dict]] = []
    if not isinstance(modules, (list, tuple)):
        return flat
    for index, module in enumerate(modules):
        if not isinstance(module, Mapping):
            continue
        path = f"{prefix}[{index}]"
        flat.append((path, dict(module)))
        children = module.get("children")
        if isinstance(children, (list, tuple)) and children:
            flat.extend(_flatten_modules(children, path + ".children"))
    return flat


def _module_key(module: Mapping, path: str) -> str:
    key = module.get("key")
    if isinstance(key, str) and key.strip():
        return key.strip()
    return path


# --- the diff ---------------------------------------------------------------

def diff_descriptors(old: Mapping | None, new: Mapping | None) -> list[dict]:
    """Every meaningful change from old (live) to new (proposed).

    old may be None for a first version, in which case there is nothing to
    compare against -- a first version is reviewed as a whole page, not a diff.
    """
    if not isinstance(new, Mapping):
        return []
    if not isinstance(old, Mapping):
        return []
    changes: list[dict] = []
    changes.extend(_diff_basic(old, new))
    changes.extend(_diff_fields(old, new))
    changes.extend(_diff_status_map(old, new))
    changes.extend(_diff_metrics(old, new))
    changes.extend(_diff_capabilities(old, new))
    changes.extend(_diff_modules(old, new))
    return _sorted_changes(changes)


def visibility_of(descriptor: Mapping) -> object:
    """Audience requested by a descriptor, in whichever shape it was written."""
    return _visibility(descriptor)


#: The one place that answers "does this approval have to go up one more level".
def needs_company_review(descriptor: Mapping | None, *, cross_department: bool = False) -> bool:
    """Whether a page cannot be signed off by its own department alone.

    Three things take a page out of the department's hands, and they are the
    three the plan named:

    * it asks to be `public`, so it stops being that department's screen;
    * it claims a capability the company has to sign for by name
      (`REVIEWED_CAPABILITIES`, today only `aggregate:read:company`) -- those
      decide what enters company numbers;
    * it was written on top of another department's page (`cross_department`),
      which the caller works out from `based_on` because the descriptor does not
      carry the owner of the version it started from.

    Everything else a department can approve itself: its own screen, its own
    fields, its own status words.
    """
    from oams.descriptor import REVIEWED_CAPABILITIES

    if not isinstance(descriptor, Mapping):
        return False
    if _visibility(descriptor) == "public":
        return True
    if cross_department:
        return True
    declared = descriptor.get("capabilities")
    if not isinstance(declared, (list, tuple)):
        return False
    return bool({str(capability) for capability in declared} & set(REVIEWED_CAPABILITIES))


def _visibility(descriptor: Mapping) -> object:
    """The page's audience lives under meta in descriptors the department saves.

    Read both shapes: a descriptor that carries the value at the top level (the
    older fixtures and hand-written plugins do) must still be comparable, and a
    change from one shape to the other is itself worth showing a reviewer.
    """
    meta = descriptor.get("meta")
    if isinstance(meta, Mapping) and meta.get("visibility") is not None:
        return meta.get("visibility")
    return descriptor.get("visibility")


def _diff_basic(old: Mapping, new: Mapping) -> list[dict]:
    changes: list[dict] = []
    if _text(old.get("name")) != _text(new.get("name")):
        changes.append(_change(kind="name_changed", area=AREA_BASIC, severity=SEVERITY_LOW,
                               path="name", label="页面名称",
                               before=old.get("name"), after=new.get("name")))
    if _text(old.get("org_unit_code")) != _text(new.get("org_unit_code")):
        changes.append(_change(kind="org_changed", area=AREA_SCOPE, severity=SEVERITY_HIGH,
                               path="org_unit_code", label="归属部门",
                               before=old.get("org_unit_code"), after=new.get("org_unit_code"),
                               why="换部门等于换数据主人：历史条目还挂在原部门，汇总归属会断。"))
    if _text(old.get("position_codes")) != _text(new.get("position_codes")):
        changes.append(_change(kind="position_changed", area=AREA_SCOPE, severity=SEVERITY_MEDIUM,
                               path="position_codes", label="可用岗位",
                               before=old.get("position_codes"), after=new.get("position_codes"),
                               why="岗位决定谁能填这一页，范围变大要确认是有意为之。"))
    old_visibility = _visibility(old)
    new_visibility = _visibility(new)
    if _text(old_visibility) != _text(new_visibility):
        to_public = str(new_visibility or "").strip() == "public"
        changes.append(_change(kind="visibility_changed", area=AREA_SCOPE,
                               severity=SEVERITY_HIGH if to_public else SEVERITY_MEDIUM,
                               path="visibility", label="可见范围",
                               before=old_visibility, after=new_visibility,
                               why="改成公共模板意味着别的部门也会看到这一版的结构。"))
    if _text(old.get("schema_version")) != _text(new.get("schema_version")):
        changes.append(_change(kind="schema_changed", area=AREA_BASIC, severity=SEVERITY_LOW,
                               path="schema_version", label="描述格式版本",
                               before=old.get("schema_version"), after=new.get("schema_version")))
    return changes


def _diff_fields(old: Mapping, new: Mapping) -> list[dict]:
    changes: list[dict] = []
    before = _index_by_key(old.get("fields"))
    after = _index_by_key(new.get("fields"))
    for key in after:
        if key not in before:
            changes.append(_change(kind="field_added", area=AREA_FIELDS, severity=SEVERITY_LOW,
                                   path=f"fields[{key}]", label=f"新增字段「{_field_label(after[key], key)}」",
                                   after=after[key].get("map_to"),
                                   why="新字段从这一版开始才有历史，之前的条目没有它的值。"))
    for key in before:
        if key not in after:
            changes.append(_change(kind="field_removed", area=AREA_FIELDS, severity=SEVERITY_HIGH,
                                   path=f"fields[{key}]", label=f"删掉字段「{_field_label(before[key], key)}」",
                                   before=before[key].get("map_to"),
                                   why="历史条目里这个字段的值不再有落点，它原先贡献的汇总数会变小。"))
    for key in after:
        if key not in before:
            continue
        old_field, new_field = before[key], after[key]
        label = _field_label(new_field, key)
        if _text(old_field.get("map_to")) != _text(new_field.get("map_to")):
            changes.append(_change(kind="field_mapping_changed", area=AREA_FIELDS,
                                   severity=SEVERITY_HIGH, path=f"fields[{key}].map_to",
                                   label=f"字段「{label}」的汇总口径",
                                   before=old_field.get("map_to"), after=new_field.get("map_to"),
                                   why="同一个字段换了落点，改前改后的数字不是一回事，不能直接比较。"))
        if _text(old_field.get("type")) != _text(new_field.get("type")):
            changes.append(_change(kind="field_type_changed", area=AREA_FIELDS,
                                   severity=SEVERITY_HIGH, path=f"fields[{key}].type",
                                   label=f"字段「{label}」的类型",
                                   before=old_field.get("type"), after=new_field.get("type"),
                                   why="类型变了，历史值可能不再合法（例如文本变成数字）。"))
        old_agg = old_field.get("aggregatable", True)
        new_agg = new_field.get("aggregatable", True)
        if bool(old_agg) != bool(new_agg):
            leaving = bool(old_agg) and not bool(new_agg)
            changes.append(_change(kind="field_aggregatable_changed", area=AREA_FIELDS,
                                   severity=SEVERITY_HIGH if leaving else SEVERITY_MEDIUM,
                                   path=f"fields[{key}].aggregatable",
                                   label=f"字段「{label}」是否进汇总",
                                   before=old_agg, after=new_agg,
                                   why="退出汇总后，引用它的指标会立刻少算。" if leaving
                                       else "新进汇总的字段，从这一版开始才计入。"))
        if _text(old_field.get("label")) != _text(new_field.get("label")):
            changes.append(_change(kind="field_label_changed", area=AREA_FIELDS,
                                   severity=SEVERITY_LOW, path=f"fields[{key}].label",
                                   label=f"字段「{key}」的叫法",
                                   before=old_field.get("label"), after=new_field.get("label")))
    return changes


def _field_label(field: Mapping, key: str) -> str:
    label = field.get("label")
    if isinstance(label, str) and label.strip():
        return label.strip()
    return key


def _status_options(descriptor: Mapping) -> list[str]:
    """The words a department can actually pick, taken from the enum field."""
    words: list[str] = []
    for field in descriptor.get("fields") or []:
        if not isinstance(field, Mapping) or field.get("map_to") != "status":
            continue
        for word in field.get("options") or []:
            if isinstance(word, str) and word not in words:
                words.append(word)
    return words


def _diff_status_map(old: Mapping, new: Mapping) -> list[dict]:
    changes: list[dict] = []
    before = old.get("status_map") if isinstance(old.get("status_map"), Mapping) else {}
    after = new.get("status_map") if isinstance(new.get("status_map"), Mapping) else {}

    # The dropdown and the mapping are edited separately, and a word can quietly
    # drop out of one but not the other: gone from the dropdown while the mapping
    # lingers means nobody can pick it again, gone from the mapping while the
    # dropdown still offers it means the next entry fails to save.
    old_options = _status_options(old)
    new_options = _status_options(new)
    for word in new_options:
        if word not in old_options and word not in before:
            changes.append(_change(kind="status_added", area=AREA_STATUS, severity=SEVERITY_LOW,
                                   path=f"status_option[{word}]", label=f"新增状态词「{word}」",
                                   after=word))
    for word in old_options:
        if word not in new_options and word in after:
            changes.append(_change(kind="status_removed", area=AREA_STATUS, severity=SEVERITY_HIGH,
                                   path=f"status_option[{word}]", label=f"不再可选状态词「{word}」",
                                   before=after.get(word),
                                   why="映射还留着，已经用这个词填过的条目不会立刻丢，但新条目再也选不到它。"))


    for word in after:
        if word not in before:
            changes.append(_change(kind="status_added", area=AREA_STATUS, severity=SEVERITY_LOW,
                                   path=f"status_map[{word}]", label=f"新增状态词「{word}」",
                                   after=after[word]))
    for word in before:
        if word not in after:
            changes.append(_change(kind="status_removed", area=AREA_STATUS, severity=SEVERITY_HIGH,
                                   path=f"status_map[{word}]", label=f"删掉状态词「{word}」",
                                   before=before[word],
                                   why="已经用这个词填过的条目会落在映射之外，公司口径里就没有它们了。"))
    for word in after:
        if word in before and _text(before[word]) != _text(after[word]):
            changes.append(_change(kind="status_remapped", area=AREA_STATUS, severity=SEVERITY_HIGH,
                                   path=f"status_map[{word}]", label=f"状态词「{word}」的意思",
                                   before=before[word], after=after[word],
                                   why="同一个词换了标准状态，同一批条目的进度归属会整体平移。"))
    return changes


def _diff_metrics(old: Mapping, new: Mapping) -> list[dict]:
    changes: list[dict] = []
    before = _index_by_key(old.get("metrics"), "metric_key")
    after = _index_by_key(new.get("metrics"), "metric_key")
    for key in after:
        if key not in before:
            changes.append(_change(kind="metric_added", area=AREA_METRICS, severity=SEVERITY_MEDIUM,
                                   path=f"metrics[{key}]", label=f"新增指标「{key}」",
                                   after=_metric_shape(after[key]),
                                   why="公司看板会多一个来源，口径要确认对得上。"))
    for key in before:
        if key not in after:
            changes.append(_change(kind="metric_removed", area=AREA_METRICS, severity=SEVERITY_HIGH,
                                   path=f"metrics[{key}]", label=f"撤掉指标「{key}」",
                                   before=_metric_shape(before[key]),
                                   why="它原先喂的公司数字会直接消失，看板上无法解释的断点就是这么来的。"))
    for key in after:
        if key not in before:
            continue
        old_shape = _metric_shape(before[key])
        new_shape = _metric_shape(after[key])
        if old_shape != new_shape:
            changes.append(_change(kind="metric_changed", area=AREA_METRICS, severity=SEVERITY_HIGH,
                                   path=f"metrics[{key}]", label=f"指标「{key}」的算法",
                                   before=old_shape, after=new_shape,
                                   why="指标算法变了，历史值不会重算，新旧数字放在一起会自相矛盾。"))
    return changes


def _metric_shape(metric: Mapping) -> str:
    return " / ".join([
        f"方式={_text(metric.get('mode'))}",
        f"聚合={_text(metric.get('agg'))}",
        f"字段={_text(metric.get('from_field'))}",
        f"限定={_text(metric.get('where_status'))}",
    ])


def _diff_capabilities(old: Mapping, new: Mapping) -> list[dict]:
    changes: list[dict] = []
    before = {str(item) for item in (old.get("capabilities") or []) if isinstance(item, str)}
    after = {str(item) for item in (new.get("capabilities") or []) if isinstance(item, str)}
    for capability in sorted(after - before):
        changes.append(_change(kind="capability_added", area=AREA_CAPABILITY, severity=SEVERITY_HIGH,
                               path=f"capabilities[{capability}]", label=f"新增能力「{capability}」",
                               after=capability,
                               why="能力只增不减才对得上账：加一条等于扩大这一页能碰的数据范围。"))
    for capability in sorted(before - after):
        changes.append(_change(kind="capability_removed", area=AREA_CAPABILITY, severity=SEVERITY_LOW,
                               path=f"capabilities[{capability}]", label=f"去掉能力「{capability}」",
                               before=capability))
    return changes


def _diff_modules(old: Mapping, new: Mapping) -> list[dict]:
    changes: list[dict] = []
    before_flat = _flatten_modules(old.get("modules"))
    after_flat = _flatten_modules(new.get("modules"))
    before = {_module_key(module, path): (path, module) for path, module in before_flat}
    after = {_module_key(module, path): (path, module) for path, module in after_flat}
    for key in after:
        if key not in before:
            path, module = after[key]
            changes.append(_change(kind="module_added", area=AREA_MODULES, severity=SEVERITY_MEDIUM,
                                   path=path, label=f"新增模块「{module.get('type')}」",
                                   after=module.get("type"),
                                   why="新模块会带来新的填报项，部门要重新教一遍。"))
    for key in before:
        if key not in after:
            path, module = before[key]
            changes.append(_change(kind="module_removed", area=AREA_MODULES, severity=SEVERITY_MEDIUM,
                                   path=path, label=f"删掉模块「{module.get('type')}」",
                                   before=module.get("type"),
                                   why="用它填过的内容不再显示，但数据仍在条目里。"))
    for key in after:
        if key not in before:
            continue
        old_path, old_module = before[key]
        new_path, new_module = after[key]
        label = f"模块「{key}」"
        if _text(old_module.get("type")) != _text(new_module.get("type")):
            changes.append(_change(kind="module_retargeted", area=AREA_MODULES, severity=SEVERITY_HIGH,
                                   path=new_path, label=label + " 的模块类型",
                                   before=old_module.get("type"), after=new_module.get("type"),
                                   why="同一个位置上换了模块，字段的消费方式也跟着变。"))
        if _text(old_module.get("source_fields")) != _text(new_module.get("source_fields")):
            changes.append(_change(kind="module_source_fields_changed", area=AREA_MODULES,
                                   severity=SEVERITY_MEDIUM, path=new_path + ".source_fields",
                                   label=label + " 读取的字段",
                                   before=old_module.get("source_fields"),
                                   after=new_module.get("source_fields")))
        if _text(old_module.get("params")) != _text(new_module.get("params")):
            changes.append(_change(kind="module_param_changed", area=AREA_MODULES,
                                   severity=SEVERITY_MEDIUM, path=new_path + ".params",
                                   label=label + " 的参数",
                                   before=old_module.get("params"), after=new_module.get("params")))
        if _text(old_module.get("order")) != _text(new_module.get("order")):
            changes.append(_change(kind="module_reordered", area=AREA_MODULES, severity=SEVERITY_LOW,
                                   path=new_path + ".order", label=label + " 的顺序",
                                   before=old_module.get("order"), after=new_module.get("order")))
    return changes


# --- summary ----------------------------------------------------------------

def summarize_changes(changes: Iterable[Mapping]) -> dict:
    """Counts plus one headline, for the review card and for the audit log."""
    items = list(changes)
    counts = {SEVERITY_HIGH: 0, SEVERITY_MEDIUM: 0, SEVERITY_LOW: 0}
    areas: dict[str, int] = {}
    for item in items:
        severity = item.get("severity")
        if severity in counts:
            counts[severity] += 1
        area = item.get("area") or AREA_BASIC
        areas[area] = areas.get(area, 0) + 1
    if not items:
        headline = "这一版与在线版本没有差异"
    elif counts[SEVERITY_HIGH]:
        headline = f"{counts[SEVERITY_HIGH]} 处会改变公司数字的含义，需要逐条看"
    elif counts[SEVERITY_MEDIUM]:
        headline = f"{counts[SEVERITY_MEDIUM]} 处一般变化，没有改变口径"
    else:
        headline = f"{counts[SEVERITY_LOW]} 处提示级变化（叫法、顺序之类）"
    return {
        "total": len(items),
        "high": counts[SEVERITY_HIGH],
        "medium": counts[SEVERITY_MEDIUM],
        "low": counts[SEVERITY_LOW],
        "has_high": counts[SEVERITY_HIGH] > 0,
        "headline": headline,
        "areas": [{"area": area, "count": count}
                  for area, count in sorted(areas.items(), key=lambda pair: (-pair[1], pair[0]))],
    }


def describe_changes(changes: Iterable[Mapping], limit: int = 6) -> str:
    """One line per change, for the audit event reason field."""
    lines = []
    for item in list(changes)[:limit]:
        lines.append(f"[{item.get('severity_label')}] {item.get('label')}：{item.get('before')} → {item.get('after')}")
    return "；".join(lines)


# --- review bundle ----------------------------------------------------------

def review_bundle(old: Mapping | None, new: Mapping | None) -> dict:
    """Everything the review card needs, in one dict.

    A first version is its own case: a brand-new page has no live sibling, so
    there is no diff to read and the reviewer should look at the whole structure
    instead of being told "no changes".
    """
    first_version = not isinstance(old, Mapping)
    changes = diff_descriptors(old, new)
    summary = summarize_changes(changes)
    if first_version:
        summary["headline"] = "首版：没有可对比的在线版本，请按整页审核"
    return {
        "first_version": first_version,
        "changes": changes,
        "summary": summary,
        "high_changes": [item for item in changes if item["severity"] == SEVERITY_HIGH],
    }


# --- approval stamp ---------------------------------------------------------

#: Which half of the two-stage review a signature belongs to.
APPROVAL_STAGES = ("department", "company")


def stamp_approval(descriptor: Mapping | None, *, reviewed_by: str,
                   stage: str = "company", reviewed_at: str | None = None,
                   version_no: int | None = None) -> dict:
    """The descriptor as it stands *after* this signature: who approved it, when.

    The document a department writes and the document that runs are two
    different objects. An author can type `"review": {"status": "approved"}`
    into their own JSON -- that is a sentence about themselves, and before this
    there was nothing in the system that could tell it apart from a real
    sign-off, which is what made "declare a public page" impossible to do
    honestly. So the signature is written here instead: at the moment the page
    goes live, from the person who clicked the button, overwriting whatever the
    author claimed. A hand-written approval is not a weaker signature; it is
    none, and this is where it gets replaced.

    `stage` says which half signed (`department` / `company`); a department
    pass that only moves the page up does not stamp anything, because the page
    is not live yet and can still be withdrawn.

    Pure: no clock, no database, no mutation of the caller's dict. The caller
    owns the transaction and hands in the timestamp.
    """
    stamped = json.loads(json.dumps(descriptor or {}, default=str))
    meta = stamped.get("meta")
    if not isinstance(meta, dict):
        meta = {}
    meta["review"] = {
        "status": "approved",
        "reviewed_by": reviewed_by,
        "reviewed_at": reviewed_at or _utc_now(),
        "stage": stage if stage in APPROVAL_STAGES else "company",
        "version_no": version_no,
    }
    stamped["meta"] = meta
    return stamped


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
