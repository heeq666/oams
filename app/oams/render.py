"""Turn an OAMS interface descriptor into HTML -- three viewpoints, one template set.

The renderer is still deliberately dumb: it reads a descriptor and paints it. It
never executes descriptor content, never runs a query, and never treats a string
from the descriptor or from a submitted value as HTML -- Jinja autoescape is on
for every template, and the handful of markup fragments built in Python go
through html.escape / markupsafe.Markup explicitly.

What changed with interface v0.2: a page is no longer a flat list of bespoke
modules. It is a tree -- containers arrange a fixed registry of preset modules,
and only preset modules carry data. So the renderer looks a module up in
oams.modules, loads templates/modules/<key>.html, and renders it with the
contract's fixed context {module, spec, values, aggregates, mode}. Container
templates recurse into module.children themselves.

Three viewpoints share the same templates; 'mode' is the only difference:

* 'edit'     -- department filling the page in: inputs, live values.
* 'readonly' -- management reading the page: no input anywhere, and modules that
                can never reach the company aggregate are skipped entirely.
* 'preview'  -- MCP preview_descriptor / review screen: sample data rendered
                with disabled widgets -- the reviewer inspects the page's definition.

The registry lives in oams.modules (unit A). While that file is still being
written, the import below falls back to a mirror of the frozen contract table
(docs/architecture/p0-interface-contract.md section 1) so this unit can be built
and tested on its own; the moment oams/modules.py lands, the real registry
wins and the mirror is dead weight.

Note on validation: this module validates only what it needs in order to paint
(a name, a module tree it can recognise, a legal nesting depth). Semantic
validation (field mapping, metrics, capabilities) belongs to
oams/descriptor.py; duplicating it here would make the renderer a second,
silently diverging rulebook.
"""
from __future__ import annotations

import contextvars
import html
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateError
from markupsafe import Markup

MODULE_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates" / "modules"
MAX_RENDER_DEPTH = 8
MODES = ("edit", "readonly", "preview")
INLINE_MODES = ("edit", "preview")  # modes in which a module may show <input>

FIELD_TYPE_TO_INPUT = {
    "text": "text",
    "number": "number",
    "percent": "number",
    "date": "date",
    "enum": "select",
    "link": "url",
    "file": "text",
}

SAFE_URL_SCHEMES = ("http", "https", "mailto")


class RenderError(Exception):
    """Raised when a descriptor cannot be rendered; the caller shows the reasons."""


# ---------------------------------------------------------------------------
# Registry access: oams.modules (unit A) wins, contract mirror is the fallback
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _MirrorSpec:
    """Same field names as oams.modules.ModuleSpec, so templates cannot tell."""

    key: str
    name: str
    layer: str
    version: str
    description: str
    params: dict
    consumes: tuple
    produces: tuple
    aggregatable: bool | None
    can_nest: bool


# key, layer, name, params, consumes, produces, aggregatable  (contract section 1)
_CONTRACT_TABLE: tuple[tuple, ...] = (
    ("columns", "container", "列", {"columns": 2, "gap": "md"}, (), (), None),
    ("rows", "container", "行", {"gap": "md"}, (), (), None),
    ("grid", "container", "网格", {"columns": 3, "gap": "md"}, (), (), None),
    ("card", "container", "卡片", {"title": ""}, (), (), None),
    ("tabs", "container", "标签页", {"labels": []}, (), (), None),
    ("divider", "container", "分隔线", {"label": ""}, (), (), None),
    ("spacer", "container", "留白", {"size": "md"}, (), (), None),
    ("progress_row", "input", "进度行",
     {}, ("title", "status", "progress_value", "note", "evidence"),
     ("status", "progress_value", "note", "evidence"), True),
    ("table", "input", "明细表", {"columns": [], "rows": 1},
     ("title", "status", "note"), ("status", "note"), True),
    ("timeline", "input", "里程碑", {},
     ("title", "due_at", "status"), ("due_at", "status"), True),
    ("blocker_list", "input", "阻塞清单", {},
     ("title", "blocked_reason", "needs_decision", "status"),
     ("blocked_reason", "needs_decision"), True),
    ("metric_input", "input", "数值录入", {"unit": ""},
     ("title", "metric_value"), ("metric_value",), True),
    ("evidence_gallery", "input", "证据区", {},
     ("title", "evidence_url", "note"), ("evidence_url",), False),
    ("metric_card", "display", "指标卡", {"metric_key": "", "span": 1}, (), (), None),
    ("metric_grid", "display", "指标网格", {"metric_keys": [], "columns": 3}, (), (), None),
    ("flow_strip", "display", "流程条", {"metrics": [], "labels": []}, (), (), None),
    ("status_chart", "display", "状态分布", {"dimension": "status"}, (), (), None),
    ("rank_list", "display", "排行榜", {"metric_key": "", "limit": 5}, (), (), None),
    ("activity_feed", "display", "动态流", {"limit": 10}, (), (), None),
    ("note_block", "display", "说明块", {"text": ""}, (), (), None),
)

MIRROR_REGISTRY: dict[str, _MirrorSpec] = {
    row[0]: _MirrorSpec(key=row[0], name=row[2], layer=row[1], version="1.0",
                        description="", params=row[3], consumes=row[4],
                        produces=row[5], aggregatable=row[6],
                        can_nest=row[1] == "container")
    for row in _CONTRACT_TABLE
}

try:  # pragma: no cover - exercised by whichever branch the checkout is in
    from oams.modules import MODULE_REGISTRY as _MODULE_REGISTRY  # noqa: F401
    from oams.modules import get_module as _real_get_module
except Exception:  # unit A has not landed yet (or is mid-write)
    _MODULE_REGISTRY = {}
    _real_get_module = None

_REGISTRY_SOURCE = "oams.modules" if _real_get_module is not None else "contract-mirror"


def registry_source() -> str:
    """Where module specs came from -- 'oams.modules' once unit A has landed."""
    return _REGISTRY_SOURCE


def resolve_spec(key: str | None):
    """Return the ModuleSpec for a module key, from the real registry when present."""
    if not key:
        return None
    if _real_get_module is not None:
        try:
            spec = _real_get_module(key)
        except Exception:
            spec = None
        if spec is not None:
            return spec
    if isinstance(_MODULE_REGISTRY, dict):
        spec = _MODULE_REGISTRY.get(key)
        if spec is not None:
            return spec
    return MIRROR_REGISTRY.get(key)


def known_module_keys() -> tuple[str, ...]:
    """Every key the renderer can paint (the two unions, mirror last)."""
    keys = list(MIRROR_REGISTRY)
    if isinstance(_MODULE_REGISTRY, dict):
        keys = list(_MODULE_REGISTRY) + [k for k in keys if k not in _MODULE_REGISTRY]
    return tuple(keys)


# ---------------------------------------------------------------------------
# Metric catalogue access: oams.metrics (unit A) wins, mirror is the fallback
# ---------------------------------------------------------------------------

# key -> (name, unit); contract section 2, platform metrics only.
_METRIC_MIRROR: dict[str, tuple[str, str]] = {
    "funnel.designed": ("有设计图", "款"),
    "funnel.sample_done": ("已出样", "款"),
    "funnel.sample_wip": ("打样在制", "款"),
    "funnel.sample_pending": ("打样未开始", "款"),
    "funnel.sample_open": ("打样未完成", "款"),
    "funnel.priced": ("核价完成", "款"),
    "funnel.ordered": ("已下单", "款"),
    "funnel.not_ordered": ("未下单", "款"),
    "funnel.qty": ("下单件数", "件"),
    "funnel.gmv": ("下单金额", "元"),
    "funnel.roi": ("推广 ROI", "倍"),
}

try:  # pragma: no cover - branch depends on whether unit A has landed
    from oams.metrics import get_metric as _real_get_metric
except Exception:
    _real_get_metric = None

try:  # pragma: no cover - 格式化实现在 metric_format，页面与看板共用同一份
    from oams.metric_format import format_metric as _real_format_metric
except Exception:
    _real_format_metric = None


def metric_meta(metric_key: str | None) -> dict[str, str]:
    """Name and unit for a metric key, without ever trusting the descriptor."""
    if not metric_key:
        return {"key": "", "name": "", "unit": ""}
    spec = None
    if _real_get_metric is not None:
        try:
            spec = _real_get_metric(metric_key)
        except Exception:
            spec = None
    if spec is not None:
        return {"key": metric_key,
                "name": str(getattr(spec, "name", "") or metric_key),
                "unit": str(getattr(spec, "unit", "") or "")}
    name, unit = _METRIC_MIRROR.get(metric_key, (metric_key, ""))
    return {"key": metric_key, "name": name, "unit": unit}


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


EMPTY = "\u2014"  # 空值的唯一写法（em dash）；0 永远显示成 0


def format_number(value: Any, unit: str = "", with_unit: bool = True) -> str:
    """Human formatting for one aggregate number (unit-aware, never raises).

    with_unit=False keeps the number bare so a card can show the unit in its own
    element; currency and percent always carry their own mark either way.
    Missing is EMPTY ("—"), never "0" and never "--": 未填 与 零 是两件事。
    """
    if value is None or value == "":
        return EMPTY
    number = _number(value)
    if number is None:
        return str(value)
    if unit == "元":
        return f"\u00a5{number / 10000:,.2f} 万" if abs(number) >= 10000 else f"\u00a5{number:,.2f}"
    text = f"{number:,.0f}" if number.is_integer() else f"{number:,.2f}"
    if unit == "%":
        return f"{text}%"
    return f"{text} {unit}".strip() if (with_unit and unit) else text


# ---------------------------------------------------------------------------
# Per-render state (kept out of the template context on purpose)
# ---------------------------------------------------------------------------

@dataclass
class _RenderState:
    descriptor: dict
    values: dict
    aggregates: dict
    mode: str
    fields_by_key: dict
    key_by_standard: dict
    status_words: tuple
    seen_fields: dict  # field key -> how many widgets already rendered for it
    uid_seq: int = 0  # monotonic counter for unique in-page DOM ids


_STATE: contextvars.ContextVar = contextvars.ContextVar("oams_render_state", default=None)
_DEPTH: contextvars.ContextVar = contextvars.ContextVar("oams_render_depth", default=0)
_STACK: contextvars.ContextVar = contextvars.ContextVar("oams_render_stack", default=())


def _state() -> _RenderState:
    state = _STATE.get()
    if state is None:
        raise RenderError("渲染器尚未初始化：没有可用的渲染上下文")
    return state


def _text(value: Any) -> str:
    return html.escape("" if value is None else str(value))


# ---------------------------------------------------------------------------
# Small template-facing helpers
# ---------------------------------------------------------------------------

def _param_default(decl: Any) -> Any:
    """Defaults may be declared bare (2) or as a schema ({'type':..,'default':2})."""
    if isinstance(decl, dict):
        if "default" in decl:
            return decl["default"]
        return None
    return decl


def params_of(module: Any) -> dict:
    """Merged params: registry defaults overridden by the node's own params."""
    if not isinstance(module, dict):
        return {}
    spec = resolve_spec(module.get("type"))
    merged: dict[str, Any] = {}
    declared = getattr(spec, "params", None) or {}
    if isinstance(declared, dict):
        for name, decl in declared.items():
            merged[name] = _param_default(decl)
    override = module.get("params")
    given = override if isinstance(override, dict) else {}
    merged.update(given)
    # A declared param may also be written flat on the node (`rows: 3` beside
    # params.columns). Precedence matches oams.modules.module_params: the nested
    # shape wins. Without this the validator would accept a value the page ignores.
    for name in declared:
        if name in module and name not in given:
            merged[name] = module[name]
    return merged


def _as_int(value: Any, default: int, *, low: int = 1, high: int = 6) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _as_list(value: Any) -> list:
    return list(value) if isinstance(value, (list, tuple)) else []


def field_of(source: Any) -> dict | None:
    """Descriptor field behind a standard source name (or a field key itself)."""
    state = _state()
    if not isinstance(source, str) or not source:
        return None
    field = state.fields_by_key.get(source)
    if field is not None:
        return field
    field_key = state.key_by_standard.get(source)
    if field_key is not None:
        return state.fields_by_key.get(field_key)
    return None


def _value_for(source: Any) -> Any:
    """Submitted value for a source: field-key lookup first, then standard name."""
    state = _state()
    field = field_of(source)
    if field is not None and field.get("key") in state.values:
        return state.values.get(field["key"])
    if isinstance(source, str) and source in state.values:
        return state.values.get(source)
    return None


def value_of(source: Any) -> str:
    value = _value_for(source)
    return "" if value is None else str(value)


def text_of(source: Any) -> str:
    """Escaped text for readonly/preview display (autoescape does the escaping)."""
    return value_of(source)


def safe_url(value: Any) -> str:
    """Only http/https/mailto links survive; javascript: and data: become ''."""
    raw = "" if value is None else str(value).strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    if parts.scheme and parts.scheme.lower() not in SAFE_URL_SCHEMES:
        return ""
    return raw


# --- status -----------------------------------------------------------------

def status_options() -> tuple:
    return _state().status_words


def status_word() -> str:
    return value_of("status")


def status_badge() -> Markup:
    """One escaped status chip; the descriptor's own word, mapped to a tone."""
    word = status_word()
    if not word:
        return Markup('<span class="wh-badge wh-badge-empty">&mdash;</span>')
    state = _state()
    standard = (state.descriptor.get("status_map") or {}).get(word, "")
    tone = {"blocked": "blocked", "done": "done", "in_progress": "progress",
            "planned": "planned"}.get(str(standard), "plain")
    return Markup(f'<span class="wh-badge wh-badge-{tone}">{_text(word)}</span>')


def progress_percent() -> int | None:
    """Rounded 0-100 progress, or None when the department has not filled it in.

    None is not 0: an untouched progress field must not read as "0% done", and
    the bar must not paint a zero-length track as if it were a measurement.
    """
    raw = _value_for("progress_value")
    if raw is None or raw == "":
        return None
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return None
    return int(max(0, min(100, round(number))))


def tabs_uid(module: Any) -> str:
    """Stable, unique-per-render id base for a tab group (radio group name)."""
    state = _state()
    state.uid_seq += 1
    raw = str((module or {}).get("id") or "") if isinstance(module, dict) else ""
    tail = re.sub(r"[^a-zA-Z0-9_-]+", "-", raw).strip("-") or str(state.uid_seq)
    return f"wh-tabs-{tail}"


def progress_text() -> str:
    """Progress as the page shows it: "35%" or a dash when nothing was filled in."""
    percent = progress_percent()
    return EMPTY if percent is None else f"{percent}%"


def inline_widgets() -> bool:
    """True when this viewpoint still shows widgets (edit live, preview disabled)."""
    return _state().mode in INLINE_MODES


# --- widgets ----------------------------------------------------------------

def _input_html(field: dict, value: Any, *, textarea: bool = False,
                labelled: bool = True) -> Markup:
    """One escaped widget for one descriptor field, respecting the render mode."""
    state = _state()
    key = str(field.get("key") or "")
    label = _text(field.get("label") or key)
    # A field may be bound by more than one module (and a table repeats its
    # columns per row), so the DOM id carries an occurrence suffix while the
    # form name stays the field key -- web.py reads values by field key.
    occurrence = state.seen_fields.get(key, 0) + 1
    state.seen_fields[key] = occurrence
    fid = f"wh-f-{_text(key)}" if occurrence == 1 else f"wh-f-{_text(key)}-{occurrence}"
    raw = "" if value is None else str(value)
    # 表格单元格里列头已经是标签，再给每个格子挂一遍 label 就是三行表头；
    # 那时用 aria-label 保可读性，视觉上不重复。
    label_html = f'<label class="wh-field-label" for="{fid}">{label}</label>' if labelled else ""
    aria = "" if labelled else f' aria-label="{label}"'
    if state.mode not in INLINE_MODES:
        shown = _text(raw) if raw else "&mdash;"
        static_label = f'<span class="wh-field-label">{label}</span>' if labelled else ""
        return Markup(f'<div class="wh-field wh-field-static">{static_label}'
                      f'<span class="wh-field-value">{shown}</span></div>')
    # preview renders the same widgets with sample values, but dead: 审核看的是这页
    # 的定义，能不能填、填完长什么样，所以控件要在场而不能提交。
    dead = state.mode != "edit"
    # 浏览器层面的 required 在这里是错的：一个填报页有三块模块、每块各一行，
    # 只填一行的用户会被另外两行空控件的 required 拦死（端到端试用 R2：按钮
    # 点了毫无反应）。必填只留 aria-required 给读屏，真正的校验在服务端
    # （web.py 的 _read_entry_form 只对主记录字段强制）。
    required = "" if dead or not field.get("required") else ' aria-required="true"'
    inert = ' disabled tabindex="-1"' if dead else ""
    if textarea:
        return Markup(f'<div class="wh-field">{label_html}'
                      f'<textarea class="wh-input" id="{fid}" name="{_text(key)}" rows="2"{aria}{required}{inert}>'
                      f'{_text(raw)}</textarea></div>')
    widget = FIELD_TYPE_TO_INPUT.get(str(field.get("type") or "text"), "text")
    if widget == "select":
        options = _as_list(field.get("options"))
        inner = ['<option value="">请选择</option>']
        for option in options:
            selected = " selected" if str(option) == raw else ""
            inner.append(f'<option value="{_text(option)}"{selected}>{_text(option)}</option>')
        return Markup(f'<div class="wh-field">{label_html}'
                      f'<select class="wh-input" id="{fid}" name="{_text(key)}"{aria}{required}{inert}>'
                      f'{"".join(inner)}</select></div>')
    attrs = ""
    if widget == "number":
        attrs = ' min="0" step="any"'
        if str(field.get("type")) == "percent":
            attrs = ' min="0" max="100" step="any"'
    placeholder = _text(field.get("example") or "")
    return Markup(f'<div class="wh-field">{label_html}'
                  f'<input class="wh-input" id="{fid}" name="{_text(key)}" type="{widget}"'
                  f'{attrs}{aria}{required}{inert} placeholder="{placeholder}" value="{_text(raw)}"></div>')


def input_for(source: Any, *, textarea: bool = False, labelled: bool = True) -> Markup:
    """Widget for a standard source name -- empty when the descriptor has no field.

    `labelled=False` drops the per-widget label (table cells already carry the
    column header, otherwise the table reads as three header rows).
    """
    field = field_of(source)
    if field is None:
        return Markup("")
    return _input_html(field, _value_for(source), textarea=textarea, labelled=labelled)


def input_for_key(field_key: Any, *, textarea: bool = False, labelled: bool = True) -> Markup:
    """Widget addressed by the descriptor field key itself (table columns)."""
    field = _state().fields_by_key.get(field_key) or field_of(field_key)
    if field is None:
        return Markup("")
    return _input_html(field, _value_for(field.get("key")), textarea=textarea, labelled=labelled)


def static_for(source: Any) -> Markup:
    """Label + value pair, no widget (used for readonly table cells)."""
    field = field_of(source)
    if field is None:
        return Markup('<span class="wh-field-value">&mdash;</span>')
    raw = _value_for(source)
    shown = _text(raw) if raw not in (None, "") else "&mdash;"
    return Markup(f'<span class="wh-field-value">{shown}</span>')


# --- aggregates -------------------------------------------------------------

def aggregate_raw(metric_key: Any) -> Any:
    if not metric_key:
        return None
    return _state().aggregates.get(str(metric_key))


def aggregate(metric_key: Any, with_unit: bool = True) -> str:
    """Formatted aggregate value for display modules.

    格式化统一走 metric_format.format_metric，页面与看板不会对同一个数给出两种写法。
    """
    raw = aggregate_raw(metric_key)
    if raw is None or raw == "":
        return EMPTY
    if _real_format_metric is not None and metric_key and with_unit:
        try:
            return str(_real_format_metric(str(metric_key), raw,
                                           metric_meta(metric_key).get("unit", "")))
        except Exception:
            pass
    return format_number(raw, metric_meta(metric_key).get("unit", ""), with_unit=with_unit)


def metric_name(metric_key: Any) -> str:
    return metric_meta(metric_key).get("name") or ("" if not metric_key else str(metric_key))


def metric_unit(metric_key: Any) -> str:
    """Unit label for the small suffix element; empty when the number carries it."""
    unit = metric_meta(metric_key).get("unit", "")
    return "" if unit in ("%", "元") else unit


def status_counts() -> list[dict]:
    """Status distribution from aggregates; tolerates either key shape."""
    aggregates = _state().aggregates
    rows = []
    seen = set()
    for word in status_options():
        standard = str((_state().descriptor.get("status_map") or {}).get(word, ""))
        raw = aggregates.get(f"status.{standard}") if standard else None
        if raw is None:
            raw = aggregates.get(f"status.{word}", 0)
        rows.append({"label": str(word), "standard": standard,
                     "value": float(raw or 0)})
        seen.add(word)
    # Aggregates may key the spread by the department's own words or by the
    # standard status names (a caller can supply both); count each status once.
    state_map = _state().descriptor.get("status_map") or {}
    mapped = {str(standard) for standard in state_map.values() if standard}
    extra = [(k, v) for k, v in aggregates.items()
             if isinstance(k, str) and k.startswith("status.")
             and k.split(".", 1)[1] not in seen and k.split(".", 1)[1] not in mapped]
    for key, value in extra:
        try:
            rows.append({"label": key.split(".", 1)[1], "standard": "", "value": float(value or 0)})
        except (TypeError, ValueError):
            continue
    return rows


def rank_rows(metric_key: Any) -> list[dict]:
    """Ranked rows for rank_list: aggregates keys shaped rank.<metric>.<label>."""
    key = str(metric_key or "")
    if not key:
        return []
    prefix = f"rank.{key}."
    rows = []
    for agg_key, agg_value in _state().aggregates.items():
        if not isinstance(agg_key, str) or not agg_key.startswith(prefix):
            continue
        try:
            rows.append({"label": agg_key[len(prefix):], "value": float(agg_value or 0)})
        except (TypeError, ValueError):
            continue
    rows.sort(key=lambda row: row["value"], reverse=True)
    unit = metric_meta(key).get("unit", "")
    return [dict(row, display=format_number(row["value"], unit)) for row in rows]


def activity_items(limit: Any) -> list[dict]:
    """Recent activity rows when the caller supplies them under aggregates['activity']."""
    raw = _state().aggregates.get("activity")
    if not isinstance(raw, (list, tuple)):
        return []
    try:
        count = int(limit)
    except (TypeError, ValueError):
        count = 10
    rows = []
    for item in list(raw)[: max(0, count)]:
        if isinstance(item, dict):
            rows.append({"when": str(item.get("when") or ""), "what": str(item.get("what") or ""),
                         "who": str(item.get("who") or "")})
        else:
            rows.append({"when": "", "what": str(item), "who": ""})
    return rows


_GAP_TOKENS = ("none", "sm", "md", "lg")


def gap_of(module: Any, default: str = "md") -> str:
    """Gap token from params, clamped to the four sizes the stylesheet knows."""
    value = str(params_of(module).get("gap") or default)
    return value if value in _GAP_TOKENS else default


def children_of(module: Any) -> list:
    """Real child modules of a container (anything malformed is ignored)."""
    if not isinstance(module, dict):
        return []
    return [child for child in _as_list(module.get("children")) if isinstance(child, dict)]


def bucket(value: Any, total: Any) -> str:
    """Quantised share for CSS bars: p10..p100 (no inline styles anywhere)."""
    try:
        part, whole = float(value or 0), float(total or 0)
    except (TypeError, ValueError):
        return "p0"
    if whole <= 0 or part <= 0:
        return "p0"
    # Half-up on purpose: Python's round() is banker's rounding, so 65% would
    # quantise to 60 and a bar would sit visibly short of its number.
    step = int(min(1.0, part / whole) * 10 + 0.5) * 10
    return f"p{max(0, min(100, step))}"


def private_note(module: Any, spec: Any = None) -> Markup:
    """Inline honesty note in the department view for never-aggregated modules."""
    if _state().mode != "edit":
        return Markup("")
    spec = spec or resolve_spec(module.get("type") if isinstance(module, dict) else None)
    if _aggregatable(module if isinstance(module, dict) else {}, spec) is False:
        return Markup('<p class="wh-meta">本部门可见，不参与公司汇总</p>')
    return Markup("")


def status_tone(words: Any) -> str:
    """Tone class for a status word (blocked / done / progress / planned / plain)."""
    descriptor = _state().descriptor
    standard = (descriptor.get("status_map") or {}).get(str(words), "")
    return {"blocked": "blocked", "done": "done", "in_progress": "progress",
            "planned": "planned"}.get(str(standard), "plain")


# ---------------------------------------------------------------------------
# The environment
# ---------------------------------------------------------------------------

_ENV = Environment(
    loader=FileSystemLoader(str(MODULE_TEMPLATE_DIR)),
    autoescape=True,  # the safety floor: every {{ }} in every template is escaped
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=False,
)

_ENV.globals.update({
    "params_of": params_of,
    "field_of": field_of,
    "value_of": value_of,
    "text_of": text_of,
    "safe_url": safe_url,
    "input_for": input_for,
    "input_for_key": input_for_key,
    "static_for": static_for,
    "status_options": status_options,
    "status_word": status_word,
    "status_badge": status_badge,
    "progress_percent": progress_percent,
    "progress_text": progress_text,
    "tabs_uid": tabs_uid,
    "inline_widgets": inline_widgets,
    "aggregate": aggregate,
    "aggregate_raw": aggregate_raw,
    "metric_name": metric_name,
    "metric_unit": metric_unit,
    "status_counts": status_counts,
    "rank_rows": rank_rows,
    "activity_items": activity_items,
    "as_int": _as_int,
    "as_list": _as_list,
    "gap_of": gap_of,
    "children_of": children_of,
    "bucket": bucket,
    "private_note": private_note,
    "status_tone": status_tone,
    "format_number": format_number,
})


def template_name(key: str) -> str:
    return f"{key}.html"


def has_template(key: str) -> bool:
    try:
        _ENV.get_template(template_name(key))
    except TemplateError:
        return False
    return True


def render_module(module: Any) -> Markup:
    """Render one module node (recursion entry point used by container templates)."""
    state = _state()
    if not isinstance(module, dict):
        raise RenderError("modules: 每个模块必须是对象")
    key = str(module.get("type") or "")
    spec = resolve_spec(key)
    if spec is None:
        raise RenderError(f"MODULE_NOT_WHITELISTED modules[{key or '?'}]: 未注册的模块类型")
    if not has_template(key):
        raise RenderError(f"MODULE_NOT_WHITELISTED modules[{key}]: 缺少模板 {template_name(key)}")

    depth = _DEPTH.get() + 1
    if depth > MAX_RENDER_DEPTH:
        raise RenderError(f"LAYOUT_TOO_DEEP modules[{key}]: 嵌套超过 {MAX_RENDER_DEPTH} 层")
    if any(id(node) == id(module) for node in _STACK.get()):
        raise RenderError(f"LAYOUT_TOO_DEEP modules[{key}]: 模块树存在循环引用")

    if state.mode == "readonly" and _hidden_in_readonly(module, spec):
        return Markup("")

    token_depth = _DEPTH.set(depth)
    token_stack = _STACK.set(_STACK.get() + (module,))
    try:
        template = _ENV.get_template(template_name(key))
        return Markup(template.render(module=module, spec=spec, values=state.values,
                                      aggregates=state.aggregates, mode=state.mode))
    except TemplateError as exc:  # a template bug must not 500 the page silently
        raise RenderError(f"RENDER_FAILED modules[{key}]: {exc}") from exc
    finally:
        _DEPTH.reset(token_depth)
        _STACK.reset(token_stack)


def _aggregatable(module: dict, spec: Any) -> Any:
    flag = module.get("aggregatable")
    if isinstance(flag, bool):
        return flag
    return getattr(spec, "aggregatable", None)


def _hidden_in_readonly(module: dict, spec: Any = None) -> bool:
    """Readonly drops department-private leaves, and containers left empty."""
    spec = spec or resolve_spec(module.get("type"))
    if spec is not None and getattr(spec, "layer", "") == "container":
        children = [c for c in _as_list(module.get("children")) if isinstance(c, dict)]
        if not children:
            return False
        return all(_hidden_in_readonly(child) for child in children)
    return _aggregatable(module, spec) is False


_ENV.globals["render_module"] = render_module


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def _structural_problems(descriptor: Any) -> list[str]:
    """Only the checks the painter itself needs; semantics belong to descriptor.py."""
    problems: list[str] = []
    if not isinstance(descriptor, dict):
        return ["descriptor: 必须是 JSON 对象"]
    for key in ("interface_id", "name", "org_unit_code", "modules"):
        if not descriptor.get(key) and key != "modules":
            problems.append(f"{key}: 必填项缺失")
    if "modules" not in descriptor:
        problems.append("modules: 必填项缺失")
        return problems
    modules = descriptor.get("modules")
    if not isinstance(modules, list) or not modules:
        problems.append("modules: 至少要有一个模块")
        return problems
    for index, module in enumerate(modules):
        if not isinstance(module, dict):
            problems.append(f"modules[{index}]: 必须是对象")
            continue
        key = module.get("type")
        if not key:
            problems.append(f"modules[{index}]: type 必填")
            continue
        if resolve_spec(str(key)) is None:
            problems.append(f"MODULE_NOT_WHITELISTED modules[{index}]: 未注册的模块类型 {key!r}")
            continue
        if not has_template(str(key)):
            problems.append(f"modules[{index}]: 模块 {key!r} 没有模板")
        _walk_modules(module, f"modules[{index}]", 1, problems)
    return problems


def _walk_modules(module: dict, path: str, depth: int, problems: list[str]) -> None:
    spec = resolve_spec(module.get("type"))
    can_nest = bool(getattr(spec, "can_nest", False))
    children = module.get("children")
    if children in (None, []):
        return
    if not can_nest:
        problems.append(
            f"MODULE_CANNOT_NEST {path}.children: 模块 {module.get('type')!r} 不是容器，不能有子模块")
        return
    if not isinstance(children, list):
        problems.append(f"MODULE_TREE_INVALID {path}.children: 必须是数组")
        return
    if depth + 1 > MAX_RENDER_DEPTH:
        problems.append(f"LAYOUT_TOO_DEEP {path}.children: 嵌套超过 {MAX_RENDER_DEPTH} 层")
        return
    for index, child in enumerate(children):
        child_path = f"{path}.children[{index}]"
        if not isinstance(child, dict):
            problems.append(f"{child_path}: 必须是对象")
            continue
        if resolve_spec(child.get("type")) is None:
            problems.append(f"MODULE_NOT_WHITELISTED {child_path}: 未注册的模块类型 {child.get('type')!r}")
            continue
        _walk_modules(child, child_path, depth + 1, problems)


def _descriptor_fields(descriptor: dict) -> tuple[dict, dict]:
    fields: dict[str, dict] = {}
    key_by_standard: dict[str, str] = {}
    for field in descriptor.get("fields") or []:
        if not isinstance(field, dict) or not field.get("key"):
            continue
        key = str(field["key"])
        fields[key] = field
        map_to = field.get("map_to")
        if isinstance(map_to, str) and map_to and map_to != "display_only":
            key_by_standard.setdefault(map_to, key)
    return fields, key_by_standard


def _sorted_modules(descriptor: dict) -> list[dict]:
    modules = [m for m in (descriptor.get("modules") or []) if isinstance(m, dict)]
    return sorted(modules, key=lambda m: (m.get("order", 0) or 0))


def _render(descriptor: dict, values: dict | None, aggregates: dict | None,
            mode: str) -> str:
    if mode not in MODES:
        raise RenderError(f"mode: 未知视角 {mode!r}")
    problems = _structural_problems(descriptor)
    if problems:
        raise RenderError("; ".join(problems))
    fields, key_by_standard = _descriptor_fields(descriptor)
    status_words = tuple((descriptor.get("status_map") or {}).keys())
    state = _RenderState(descriptor=descriptor, values=dict(values or {}),
                         aggregates=dict(aggregates or {}), mode=mode,
                         fields_by_key=fields, key_by_standard=key_by_standard,
                         status_words=status_words, seen_fields={}, uid_seq=0)
    token_state = _STATE.set(state)
    token_depth = _DEPTH.set(0)
    token_stack = _STACK.set(())
    try:
        blocks = "".join(str(render_module(module)) for module in _sorted_modules(descriptor))
    finally:
        _STACK.reset(token_stack)
        _DEPTH.reset(token_depth)
        _STATE.reset(token_state)
    return (f'<div class="wh-interface wh-interface-{mode}" '
            f'data-interface="{_text(descriptor.get("interface_id"))}" '
            f'data-org="{_text(descriptor.get("org_unit_code"))}" data-mode="{mode}">'
            f'<h2 class="wh-interface-title">{_text(descriptor.get("name"))}</h2>'
            f'<div class="wh-interface-body">{blocks}</div></div>')


def render_interface(descriptor: dict, values: dict | None = None,
                     aggregates: dict[str, float] | None = None) -> str:
    """Department viewpoint: input modules render widgets, display modules real numbers."""
    return _render(descriptor, values, aggregates, mode="edit")


def render_read_only(descriptor: dict, values: dict | None = None,
                     aggregates: dict[str, float] | None = None) -> str:
    """Manager viewpoint: no input anywhere, department-private modules skipped."""
    return _render(descriptor, values, aggregates, mode="readonly")


def render_preview(descriptor: dict, sample: bool = True) -> str:
    """Review viewpoint: sample data, widgets present but disabled.

    审的是这页的定义（字段、密度、空态），不写库。角标已下线；机器可读的汇总
    口径走 MCP 的 `preview_descriptor` → `aggregates_preview`（mcp/tools.py）。
    """
    if not isinstance(descriptor, dict):
        raise RenderError("descriptor: 必须是 JSON 对象")
    values = _sample_values(descriptor) if sample else {}
    aggregates = _sample_aggregates(descriptor) if sample else {}
    return _render(descriptor, values, aggregates, mode="preview")


# ---------------------------------------------------------------------------
# Preview sample data
# ---------------------------------------------------------------------------

_SAMPLE_BY_FIELD: dict[str, str] = {
    "title": "AW26-001 羊毛混纺外套",
    "status": "",
    "progress_value": "65",
    "note": "面料已到仓，等待二次确认",
    "evidence": "SY-2610",
    "evidence_url": "https://example.com/sample-sheet.pdf",
    "due_at": "2026-11-05",
    "blocked_reason": "等面料检测报告",
    "metric_value": "128",
}


def _sample_values(descriptor: dict) -> dict:
    """Plausible fake values keyed by descriptor field key (never from user data)."""
    values: dict[str, str] = {}
    status_words = list((descriptor.get("status_map") or {}).keys())
    for field in descriptor.get("fields") or []:
        if not isinstance(field, dict) or not field.get("key"):
            continue
        key = str(field["key"])
        map_to = str(field.get("map_to") or "")
        if map_to == "status" and status_words:
            values[key] = status_words[min(1, len(status_words) - 1)]
        elif map_to in _SAMPLE_BY_FIELD and _SAMPLE_BY_FIELD[map_to]:
            values[key] = _SAMPLE_BY_FIELD[map_to]
        elif field.get("options"):
            values[key] = str(field["options"][0])
        elif field.get("example"):
            values[key] = str(field["example"])
        else:
            values[key] = "示例内容"
    return values


def _sample_aggregates(descriptor: dict) -> dict:
    """Fake numbers for every metric the descriptor mentions, plus a status spread."""
    aggregates: dict[str, Any] = {}
    for key in ("funnel.designed", "funnel.sample_done", "funnel.sample_wip",
                "funnel.sample_pending", "funnel.sample_open", "funnel.priced",
                "funnel.ordered", "funnel.not_ordered", "funnel.qty",
                "funnel.gmv", "funnel.roi"):
        aggregates[key] = {"funnel.qty": 63289, "funnel.gmv": 17458734.0,
                           "funnel.roi": 5.1751}.get(key, 128)
    for index, (word, standard) in enumerate((descriptor.get("status_map") or {}).items()):
        aggregates[f"status.{word}"] = 12 + index * 7
        aggregates[f"status.{standard}"] = 12 + index * 7
    aggregates["activity"] = [
        {"when": "今天 09:12", "what": "补充了打样单号 SY-2610", "who": "设计部"},
        {"when": "昨天 16:40", "what": "把 3 款从待打样推进到打样中", "who": "设计部"},
        {"when": "昨天 10:05", "what": "登记了一条阻塞：等面料检测报告", "who": "设计部"},
    ]
    for key in ("funnel.gmv", "funnel.qty", "funnel.sample_done"):
        for label, value in (("抖音", 820), ("天猫", 640), ("拼多多", 310)):
            aggregates[f"rank.{key}.{label}"] = value
    return aggregates
