# P0 接口冻结（竖切第一步的施工图）

> 状态：Lead 冻结稿，2026-10-22。**这四个文件的签名与常量一经冻结，并行开发期间不得单方面改动**；确需改动必须先提给 Lead，由 Lead 改本文并通知全员。
> 目标：让「契约/渲染/MCP/指标计算」四块能同时开工，互不阻塞。

---

## 0. 冻结的四个单元与归属

| 单元 | 文件 | 归属 | 下游消费者 |
|---|---|---|---|
| 契约与注册表 | `app/workhub/modules.py`、`app/workhub/metrics.py`、`app/workhub/descriptor.py` | A | B（渲染）、C（MCP） |
| 渲染 | `app/workhub/render.py`、`app/workhub/templates/modules/*.html`、`app/workhub/static/workhub-modules.css` | B | 页面路由 |
| MCP 服务端 | `app/workhub/mcp/` | C | 部门管理员 |
| 指标计算 | `app/workhub/funnel.py`、`app/workhub/datacenter.py` | D | 首页、部门页 |

依赖方向：**B / C / D 可以只按本文的签名写代码，不需要等 A 的文件存在**（缺失时先写 `from workhub.modules import ...`，A 落地即可跑通）。

---

## 1. 模块注册表（B、C 都按这张表写）

**层（layer）**：`container` / `input` / `display`

```python
# app/workhub/modules.py
from dataclasses import dataclass

@dataclass(frozen=True)
class ModuleSpec:
    key: str                  # "progress_row"
    name: str                 # "进度行"
    layer: str                # "container" | "input" | "display"
    version: str              # "1.0"
    description: str
    params: dict              # 参数声明，见下
    consumes: tuple[str, ...] # 接受的 source_fields（标准字段名）；() = 不接受
    produces: tuple[str, ...] # 产出的标准字段；() = 不产出
    aggregatable: bool | None # True/False；None = 不适用（容器与只读展示）
    can_nest: bool            # 只有 container 为 True

MODULE_REGISTRY: dict[str, ModuleSpec]
CONTAINER_DEPTH_LIMIT = 2

def get_module(key: str) -> ModuleSpec | None
def list_modules(layer: str | None = None) -> list[dict]          # MCP list_modules 直接消费
def module_depth(modules: list[dict]) -> int                      # 返回布局嵌套层数
def validate_module_tree(modules: list[dict]) -> list[str]        # 全部结构问题，不止第一条
```

### 首批模块（key 冻结，不得改名）

| key | 层 | 参数 params | consumes | produces | 可汇总 |
|---|---|---|---|---|---|
| `columns` | container | `columns: int = 2`、`gap: str = "md"` | — | — | — |
| `rows` | container | `gap: str = "md"` | — | — | — |
| `grid` | container | `columns: int = 3`、`gap: str = "md"` | — | — | — |
| `card` | container | `title: str` | — | — | — |
| `tabs` | container | `labels: list[str]` | — | — | — |
| `divider` | container | `label: str = ""` | — | — | — |
| `spacer` | container | `size: str = "md"` | — | — | — |
| `progress_row` | input | — | title, status, progress_value, note, evidence | status, progress_value, note, evidence | True |
| `table` | input | `columns: list[{key,label}]` | title, status, note | status, note | True |
| `timeline` | input | — | title, due_at, status | due_at, status | True |
| `blocker_list` | input | — | title, blocked_reason, needs_decision, status | blocked_reason, needs_decision | True |
| `metric_input` | input | `unit: str` | title, metric_value | metric_value | True |
| `evidence_gallery` | input | — | title, evidence_url, note | evidence_url | False |
| `metric_card` | display | `metric_key: str`、`span: int = 1` | — | — | None |
| `metric_grid` | display | `metric_keys: list[str]`、`columns: int = 3` | — | — | None |
| `flow_strip` | display | `metrics: list[str]`、`labels: list[str]` | — | — | None |
| `status_chart` | display | `dimension: str = "status"` | — | — | None |
| `rank_list` | display | `metric_key: str`、`limit: int = 5` | — | — | None |
| `activity_feed` | display | `limit: int = 10` | — | — | None |
| `note_block` | display | `text: str` | — | — | None |

**铁律（校验器必须拦）**：容器节点不允许出现 `source_fields`；容器不允许有 `params.text`；任何文字/数字只能来自 `note_block`、`metric_card` 等预设模块。容器只能有 `children`。

**深度语义（重要，别把叶子算成一层）**：`CONTAINER_DEPTH_LIMIT = 2` 指的是**最多两层嵌套容器**。即下面的结构是**合法**的：

```yaml
- type: columns        # 第 1 层容器
  children:
    - type: card       # 第 2 层容器
      children:
        - type: progress_row   # 叶子，不是容器，不计入层数
```

```yaml
- type: columns        # 1
  children:
    - type: card       # 2
      children:
        - type: grid   # 3 ← 超限，报 LAYOUT_TOO_DEEP
```

判定规则：对每个节点算「从根到它的**容器**层数」（根为 0，进入一个 container 子节点 +1）。任何 container 节点其容器层数 > 2 即报 `LAYOUT_TOO_DEEP`；非容器叶子节点不参与层数判定。

---

## 2. 指标目录（C、D 都按这张表写）

```python
# app/workhub/metrics.py
@dataclass(frozen=True)
class MetricSpec:
    key: str            # "funnel.sample_done"
    name: str           # "已出样"
    unit: str           # "款" | "件" | "元" | "倍" | "%" | ""
    origin: str         # "platform"（数据中心算） | "interface"（部门条目算）
    formula: str        # 人读口径说明
    source: str         # "datacenter:styles" 等
    wave_scoped: bool
    group: str          # "design" | "sample" | "order" | "money"
    on_home: bool
    home_order: int | None

METRIC_CATALOG: dict[str, MetricSpec]
HOME_DEFAULT: tuple[str, ...]   # 首页默认 6 格的顺序

def get_metric(key: str) -> MetricSpec | None
def list_metrics(group: str | None = None, home_only: bool = False,
                 origin: str | None = None) -> list[dict]
def validate_metric_binding(binding: dict, org_unit_code: str,
                            fields: dict[str, dict]) -> list[str]
```

### platform 指标（origin=platform，部门只能引用不能改口径）

| key | name | unit | on_home | 口径 |
|---|---|---|---|---|
| `funnel.designed` | 有设计图 | 款 | | `has_design_image = true` |
| `funnel.sample_done` | 已出样 | 款 | ✅ 1 | 至少一个颜色已出样（款级派生） |
| `funnel.sample_wip` | 打样在制 | 款 | | 无色出样且有色在制 |
| `funnel.sample_pending` | 打样未开始 | 款 | | 无色出样、无色在制 |
| `funnel.sample_open` | 打样未完成 | 款 | ✅ 2 | wip + pending |
| `funnel.priced` | 核价完成 | 款 | | 款级最新核价状态=核价完成 |
| `funnel.ordered` | 已下单 | 款 | ✅ 3 | `is_ordered = true` |
| `funnel.not_ordered` | 未下单 | 款 | ✅ 4 | `is_ordered = false` |
| `funnel.qty` | 下单件数 | 件 | | `sum(quantity)` |
| `funnel.gmv` | 下单金额 | 元 | ✅ 5 | `sum(paid_amount)` |
| `funnel.roi` | 推广 ROI | 倍 | ✅ 6 | `sum(marketing_sales)/sum(spend)` |

### interface 指标（origin=interface，部门声明，命名强制 `dept.<org_unit_code>.<slug>`）

binding 形状（descriptor `metrics[]` 的一项）：

```yaml
- metric_key: dept.PRODUCTION.finished_count   # emit 必须带 dept.<CODE>. 前缀
  mode: emit                                   # emit | display
  agg: count                                   # count | sum | avg | count_distinct
  from_field: status_word                      # 必须是本插件已声明的 field.key
  where_status: done                           # 可选，四态之一
- metric_key: funnel.sample_done                # display 只能引用 platform 指标
  mode: display
```

---

## 3. descriptor v0.2 形状（A 实现，B/C 解析用）

在 v0.1 的 9 个顶层键基础上：

| 变化 | 说明 |
|---|---|
| `schema_version` 新增 `"0.2"` | 仍接受 `"0.1"`，但 0.1 不允许出现 0.2 字段 |
| 新增顶层 `capabilities: list[str]` | 从冻结集合里选，见 §4 |
| 新增顶层 `metrics: list[binding]` | 见 §2 |
| `modules[]` 支持 `children` | 容器嵌套，`module_depth() <= 2` |
| `modules[]` 支持 `module_version: str` | 形如 `"^1.0"`，缺省视为 `"^<注册表版本>"` |
| `modules[].type` 必须来自 `MODULE_REGISTRY` | 旧的 11 个 type 作废，`custom` 废弃（改为容器组合） |
| `fields[].map_to` 必须属于标准字段词汇表或 `display_only` | 词汇表见 §5 |
| 保留检查：title/status/evidence 必须可产出 | 报错文案不变 |

`validate_descriptor()` 签名不变，**返回值仍是 list[str]，仍然收集全部问题**。新增关键字参数均可选：

```python
def validate_descriptor(descriptor, known_org_unit_codes=None,
                        known_position_codes=None, *, strict_v02: bool = True) -> list[str]
```

新增错误码（拒绝原因分类，写进 `meta.reject_codes` 文档用）：
`FIELD_UNMAPPED` / `STATUS_UNMAPPED` / `METRIC_UNDEFINED` / `CAPABILITY_ESCALATION` / `MODULE_NOT_WHITELISTED` / `LAYOUT_TOO_DEEP` / `DATA_RULE_VIOLATION`。
每条问题消息前缀必须带错误码，格式：`"[LAYOUT_TOO_DEEP] modules[root]: ..."`。

---

## 4. 能力声明集合（冻结）

```
entries:write            entries:read:dept       entries:read:self
aggregate:read:dept      aggregate:read:company  entries:link:work_item
export:dept
```

`aggregate:read:company` 与「首次上架」触发二级审批（见概念稿 4.5）。

---

## 5. 标准字段词汇表（冻结，来自商品数据中心）

```
# A 对象引用
style_code skc_code color sku_code wave category brand designer style_position
supplier_code material_code order_no
# B 度量
count quantity amount percent ratio duration metric_value
# C 时间
planned_at due_at completed_at reported_at status_since
# D 状态
status sample_status approval_status
# E 文本证据
title note evidence evidence_url file
# F 关系
work_item_id parent_ref
# G 汇报过程字段（概念稿 line 80 明确要求，不能漏）
blocked_reason needs_decision progress_value
```

`display_only` 不是字段。`map_to` 只能取上面这些或 `display_only`。

> 修订记录（Lead，本轮）：G 组三个字段是第一版冻结时漏掉的。概念稿第 80 行写明「标准字段 = 数据中心词汇表 + 汇报必需的过程字段（status / progress / note / evidence / needs_decision）」，而 `blocked_reason` / `progress_value` 是 `progress_row` 与 `blocker_list` 两个录入模块的实际产出列。已加入冻结集。

---

## 6. 渲染器契约（B 实现）

```python
# app/workhub/render.py
class RenderError(Exception): ...

def render_interface(descriptor: dict, values: dict | None = None,
                     aggregates: dict[str, float] | None = None) -> str
    # 部门视角：录入模块出输入控件，展示模块出真实数字（读 aggregates）

def render_read_only(descriptor: dict, values: dict | None = None,
                     aggregates: dict[str, float] | None = None) -> str
    # 管理层视角：不出现任何 input，跳过 aggregatable=False 的模块

def render_preview(descriptor: dict, sample: bool = True) -> str
    # MCP preview_descriptor 用：假数据 + 在每块右上角标出「进汇总 / 不进汇总」
```

模块 → 片段：每个 key 对应 `templates/modules/<key>.html`，由 `render.py` 用 `jinja2.Environment` 载入并渲染，**上下文固定为** `{module, spec, values, aggregates, mode}`（`mode` ∈ `edit`/`readonly`/`preview`）。

---

## 7. 指标计算契约（D 实现）

```python
# app/workhub/datacenter.py
class DataCenter(Protocol):
    def list_waves(self) -> list[str]: ...
    def list_products(self, *, wave: str | None) -> list[dict]: ...
    def list_development(self, *, wave: str | None) -> dict: ...
    def list_marketing(self, *, wave: str | None) -> dict: ...

# app/workhub/funnel.py
SAMPLE_DONE_WORDS: tuple[str, ...]
SAMPLE_WIP_WORDS: tuple[str, ...]

def derive_style_sample_state(color_states: list[str]) -> str   # "done" | "wip" | "pending"
def compute_funnel(dc: DataCenter, *, wave: str | None) -> dict[str, float | int]
    # 返回 11 个 platform metric_key 的值，键名必须与 §2 完全一致
def format_metric(metric_key: str, value: float | int | None) -> str
    # 63,289 件 / ¥1,745.87 万 / 5.17 / 59 款
```

`list_products` 返回的每条**必须**含：`style_code, wave, category, has_design_image, is_ordered, quantity, paid_amount`。
`list_development` 返回 `{"styles": {style_code: {"sample_states": [颜色级状态...], "pricing_status": "核价完成"|...}}}`。

---

## 8. 并行工作纪律

1. 只改自己表格里的文件；需要别人文件里的东西，按本文学名调用。
2. 任何公共签名变更 → 先报 Lead，Lead 改本文并通知全员。
3. 每个单元交付时必须带 **pytest 可跑的测试**（`.venv/bin/python -m pytest tests/<你的测试> -q` 全绿）。本机没有 PostgreSQL，**不要写需要数据库的测试**。
4. 测试留档一律用真断言（`assert`），不要再写 `print("PASS")` 那套手工脚本。
