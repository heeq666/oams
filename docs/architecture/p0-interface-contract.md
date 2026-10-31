# P0 接口冻结（竖切第一步的施工图）

> 状态：Lead 冻结稿，2026-10-22。**这四个文件的签名与常量一经冻结，并行开发期间不得单方面改动**；确需改动必须先提给 Lead，由 Lead 改本文并通知全员。
> 目标：让「契约/渲染/MCP/指标计算」四块能同时开工，互不阻塞。

---

## 0. 冻结的四个单元与归属

| 单元 | 文件 | 归属 | 下游消费者 |
|---|---|---|---|
| 契约与注册表 | `app/oams/modules.py`、`app/oams/metrics.py`、`app/oams/descriptor.py` | A | B（渲染）、C（MCP） |
| 渲染 | `app/oams/render.py`、`app/oams/templates/modules/*.html`、`app/oams/static/oams-modules.css` | B | 页面路由 |
| MCP 服务端 | `app/oams/mcp/` | C | 部门管理员 |
| 指标计算 | `app/oams/funnel.py`、`app/oams/datacenter.py` | D | 首页、部门页 |

依赖方向：**B / C / D 可以只按本文的签名写代码，不需要等 A 的文件存在**（缺失时先写 `from oams.modules import ...`，A 落地即可跑通）。

---

## 1. 模块注册表（B、C 都按这张表写）

**层（layer）**：`container` / `input` / `display`

```python
# app/oams/modules.py
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
| `table` | input | `columns: list[{key,label}]`、`rows: int = 1`（1–5） | title, status, note | status, note | True |
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

**参数写法（2026-10-30 补）**：声明过的参数可以写在 `params` 里，也可以平铺在节点上（`rows: 3` 与 `params: {rows: 3}` 等价；两处都写时以 `params` 为准）。两种写法都按声明校验：`table` 的 `rows` 只能是 1–5 的整数，越界或写错类型**明确报错**（`DATA_RULE_VIOLATION`），不再静默忽略。节点上出现既不是声明参数、也不在节点固定键（`key`/`type`/`order`/`params`/`module_version`/`children`/`source_fields`/`aggregatable`）里的键，同样报错。

**但平铺只对叶子模块成立（2026-10-30 补）**：容器节点只能写在 `params` 里，不许平铺。容器的键白名单是 `CONTAINER_ALLOWED_KEYS`（`key`/`type`/`order`/`children`/`params`/`module_version`），比叶子的窄——`source_fields`、`aggregatable` 一律不许带；在容器上写 `title: "xx"` 会被 `DATA_RULE_VIOLATION` 拒掉，那不是 bug。这个不对称是有意的：容器已经带 `children`，参数再平铺上来，读者分不清哪个键是结构、哪个是参数。锚：`app/oams/modules.py:73`（白名单）、`:597`（校验）、`app/oams/descriptor.py:391-397`（为什么不在容器上再查一遍）。

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
# app/oams/metrics.py
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

METRIC_CATALOG: dict[str, MetricSpec]   # 商品数据中心下线后为空表
ORIGINS: tuple[str, ...] = ("interface",)   # 只剩部门条目算的这一种
GROUPS: tuple[str, ...] = ()                # 平台分组（design/sample/order/money）随之下线

def get_metric(key: str) -> MetricSpec | None
def list_metrics(group: str | None = None, home_only: bool = False,
                 origin: str | None = None) -> list[dict]   # 目前恒返回 []
def validate_metric_binding(binding: dict, org_unit_code: str,
                            fields: dict[str, dict],
                            own_metrics: set[str] | None = None) -> list[str]
    # own_metrics = 本页 metrics[] 里 mode=emit 的键集合；display 绑定可引它（自产自用）
```

### display 能引什么（2026-11-02 定，取代「display 只能引 platform」）

`metric_card` / `metric_grid` / `rank_list` / `flow_strip` 的指标参数可以引两类：

1. **platform 指标**——目录里 `origin="platform"` 的键（商品数据中心下线后目录为空，
   规则保留，平台将来回来还能这么写）；
2. **本页自己 emit 的指标**——须同时满足 ①键以 `dept.<本单位 org_unit_code>.` 开头、
   ②该键在**同一个 descriptor 的 `metrics[]` 里以 `mode=emit` 声明过**（自产自用）。

三条一律拒（`METRIC_UNDEFINED`，文案点名是哪一条没满足）：**别的部门**的 `dept.<别人CODE>.*`、
**凭空编的键**、以及**引了但本页没有 emit 它**。别放宽成「只要键存在就通过」——那等于把
跨部门读数塞进一个显示参数里。

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
- metric_key: funnel.sample_done                # display：platform 指标（目录里有才行）
  mode: display
- metric_key: dept.PRODUCTION.finished_count   # display：本页自己 emit 的那条（同为 ①②）
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
                        known_position_codes=None, *, strict_v02: bool = True,
                        require_recorded_approval: bool = False) -> list[str]
```

`require_recorded_approval`（默认 `False`）：为 `True` 时要求文档已带**系统写入的**批准记录（见 §4.1）。**只在两条没有审核过程的路上用**——「管理员直接建上线页」（`/setup/interfaces`）和 CLI 自检（`oams plugin check`）：这两处没有任何人签过字，文件必须自带批准，否则等于用导入绕开审核。部门新建、提交审核、升级自检（`upgrade.py`）**都不传**，走默认口径：校验判断的是「这份文档合不合规」，不是「它被没被批过」——后者是流程状态，不该藏在文档校验里。

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

公司终审只看**三条判据**（`app/oams/review.py:207` `needs_company_review()`），满足任一条就不能由部门自己签完：

| # | 路径 | 作者怎么写 | 代码怎么判 |
|---|---|---|---|
| ① | 要对外公开 | `meta.visibility: public` | 读 `visibility`（`meta` 优先，兼容顶层老写法） |
| ② | 要进公司汇总 | `capabilities` 里含 `aggregate:read:company` | 与 `REVIEWED_CAPABILITIES` 取交集（今天只这一项） |
| ③ | 跨部门沿用别部门的页 | `based_on` 指向对方已上线的页 | 查 `based_on` 那行的 `org_unit_id` 与本页是否不同（`web.py` 的 `_cross_department`） |

**什么时候判**：三条都在**部门初审点「通过」那一刻**统一判（`app/oams/web.py:1528` 调 `needs_company_review`）——不升级就直接启用；升级就把行上的 `review_stage` 翻成 `company`，等公司层再审一次才启用。**提交动作本身不判**，草稿阶段随便改，不占审批资源。

§4 原句里「与『**首次上架**』触发二级审批」这半句**未实现、待决定**，见 §4.2。

### 4.1 可见性与能力：文档只声明意图，批准由启用节点记录

**文档里写的都是意向，不是批准。** 作者在 `meta.visibility` 声明想公开、在 `capabilities` 里声明要 `aggregate:read:company`，都只是「我要这个」；批准是**别人的动作**，不能由作者在提交前自证。

- **作者写什么**：`"meta": {"visibility": "public"}`，`"capabilities": [..., "aggregate:read:company"]`。**不写 `meta.review`**（写了也不作数，见下）。
- **批准记录在哪**：权威记录是行上的 `interfaces.status` / `reviewed_by` / `reviewed_at` / `review_stage` 加审计表。文档里的 `meta.review` 只是**导出用的副本**。
- **谁来写副本**：系统在页面**启用时**（`_activate_page` 那一票）写进 `meta.review`，字段固定五个：`status`（`approved`）/ `reviewed_by`（批准人的工号，不是角色）/ `reviewed_at`（ISO8601 UTC）/ `stage`（`department` 或 `company`）/ `version_no`。
- **为什么这样**：能写 `meta.review` 的只有作者，能批准它的只有别人。把「已被批准」做成文档校验的前置条件，等于要作者在提交前自证已被批准，逻辑上无解——这正是 2026-10-31 那两次「公开页 / 能力页卡死」的唯一成因。改成「一个关口、且这个关口是盖戳而不是查验」，也顺带堵死「手写 `approved` 混过校验」。
- **起草、提交、部门初审、公司终审都不要求文档带批准**；只有 §3 里那两条无审核路径（`/setup/interfaces` 直建上线页、CLI 自检）传 `require_recorded_approval=True`。
- **草稿期间的可见性**：行上 `visibility` 一直是 `department`，公开只在启用时提升；所以草稿与待审期间，公众看不到它。

### 4.2 「首次上架也走二级审批」：未实现，待决定

**状态：未实现，待决定。** 不是「已实现」，也不是「废弃」——先记在这里，别当成现行规则去照做。

- **它要求什么**：「一个部门第一次上线时，哪怕只是内部自用页，也要走公司终审」。
- **为什么没做**：这条与上面三条判据（公开 / 公司汇总能力 / 跨部门）**无关**，是独立的第四条。真做了会把**所有新页**都推到平台终审——每个部门的第一页都要等公司点头，属**流程变更**，不是修 bug，所以不搭着审批死锁的修复一起上。
- **代码现状**：`needs_company_review()` 只认那三条，没有「首次上架」这一条，也没有任何按 slug 首次上线做的判断。
- **谁定**：老大（C经理 派活时才动）。定了之后二选一——补进代码当第四条判据，或从 §4 与概念稿 4.5 里删掉这句。

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

**但有两个是例外，只能平台写、不能部门填**：`sample_status` 与 `approval_status`（`descriptor.PLATFORM_DERIVED_FIELDS`）。它们是**颜色级**记录派生的款级结论，部门把字段映射到它们会分叉事实，校验器报 `[DATA_RULE_VIOLATION]`。词汇表成员身份与 `map_to` 使用权是两件事：留在词汇表里（平台派生数据要用这个名字），但不许 `fields[].map_to` 指向。要表达状态请用 `status`。

> 修订记录（Lead，本轮）：G 组三个字段是第一版冻结时漏掉的。概念稿第 80 行写明「标准字段 = 数据中心词汇表 + 汇报必需的过程字段（status / progress / note / evidence / needs_decision）」，而 `blocked_reason` / `progress_value` 是 `progress_row` 与 `blocker_list` 两个录入模块的实际产出列。已加入冻结集。

---

## 6. 渲染器契约（B 实现）

```python
# app/oams/render.py
class RenderError(Exception): ...

def render_interface(descriptor: dict, values: dict | None = None,
                     aggregates: dict[str, float] | None = None) -> str
    # 部门视角：录入模块出输入控件，展示模块出真实数字（读 aggregates）

def render_read_only(descriptor: dict, values: dict | None = None,
                     aggregates: dict[str, float] | None = None) -> str
    # 管理层视角：一个字段控件都不出现（input / select / textarea 全无），只出静态值

def render_preview(descriptor: dict, sample: bool = True) -> str
    # 审核视角：假数据 + 真实控件，但控件一律 disabled 且 tabindex="-1"（看得见、点不动）
    # 不再标注「进汇总 / 不进汇总」；汇总口径的出口是 preview_descriptor 返回的
    # aggregates_preview（app/oams/mcp/tools.py:474）
```

模块 → 片段：每个 key 对应 `templates/modules/<key>.html`，由 `render.py` 用 `jinja2.Environment` 载入并渲染，**上下文固定为** `{module, spec, values, aggregates, mode}`（`mode` ∈ `edit`/`readonly`/`preview`）。

### 6.1 空值语义（行为级约定，不只是样式）

- 空值统一渲染 **`—`（U+2014）**，`0` 只表示零，两者不许混。`render.py:226` 的 `EMPTY = "\u2014"` 是唯一写法。
- `aggregate()` / `format_number()` 遇到 `None` / `""` 返回 `EMPTY`，**不返回 `0`，也不返回 `--`**。
- `progress_percent()` 对未填返回 **`None`**（不是 `0`）；模板据此渲染 `—` + 空轨道，不能显示成 `0%`（`render.py:394-402`，`progress_row.html:16`）。
- 这条是契约不是样式：谁把未填渲染成 `0`，汇总数字就会凭空多出一个零，属于必须拦下的错。

### 6.2 只读视角剔除「不进公司汇总」的模块

只读是**公司层看数**的视角，不是审核视角。规则：`aggregatable=False` 的模块在 `readonly` 下**整块不渲染**——不是「渲染出来但不给控件」。

- **为什么**：只读的产物要进公司汇总，进不了汇总的模块留着只占地方；证据区的链接是部门私有的、不出楼，给公司层看既无汇总意义，又多一层外泄面。
- **现在只有一个**：`evidence_gallery`，`MODULE_REGISTRY` 里唯一的 `aggregatable=False`（`app/oams/modules.py:249`）。
- **审核要看证据，走 `preview`**：真实控件 `disabled`、证据区照常渲染。两个视角分工清楚，不冲突。
- 代码：`render.py:18-20` 的 docstring 写明 "skipped entirely"、`:732` 的 `state.mode == "readonly" and _hidden_in_readonly(...)`、`:755` `_hidden_in_readonly`；测试 `tests/test_render_modules.py:327` 断言只读下不出现 `data-type="evidence_gallery"`。

### 6.3 一条条目算不算数：部门一面、公司一面（2026-11-02 定）

**同一个条目，在部门页算数、在公司汇总不算数。** 这不是 bug，是两套谓词各答各的问题，
代码里写成两条常量，任何读数字的地方必须挑对那一条：

| 常量 | 谓词 | 谁用 |
|---|---|---|
| `api.DEPARTMENT_ROLLUP_PREDICATE` | `status_entry = 'active' AND entry_mode = 'live'` | 部门页自己的指标（`dept.*` 的 emit）、页面上的展示模块 |
| `api.ROLLUP_PREDICATE` | 上面两条 **且** `work_item_id IS NOT NULL` | 公司汇总（公司层看数、进公司数字的那条路） |

- **部门一面为什么要松**：部门看自己那一面问的是「我们记了什么」。一条报销、一件快递代收，
  本来就不必先挂在某个工作项上；拿公司口径去卡部门页，结果就是演示页永远 0，部门做了的事
  一个数都显示不出来。
- **公司一面为什么不能松**：公司数字回答的是「有计划的活干到哪了」。没有工作项的填报如果都
  能进公司数字，谁都能把公司数字抬起来，这条谨慎必须留着。
- **状态标签也要各说各话**：部门面用 `api.DEPARTMENT_ROLLUP_STATE_SQL`（`counted` 已计入本部门
  且进公司 / `counted_unlinked` 已计入本部门但未进公司 / `draft` / `retracted`），公司面用
  `api.ROLLUP_STATE_SQL`（`rolled` / `unlinked` / `draft` / `retracted`）。**`unlinked` 只表示
  「进不了公司数字」，绝不表示「不算数」**——把部门里已经算进去的条目标成「未关联，不算」，
  是这一节要防的那类误读。
- 代码：`api.py:967-980`（两条谓词与两套 CASE）、`_binding_entries(..., company=False)`、
  `page_entries()` 每行额外带 `feeds_company` 布尔值（这一条挂不挂工作项、能不能进公司数字）。

---

## 7. 指标计算契约

**商品数据中心（`datacenter.py`）与平台漏斗推导（`funnel.py`）已于 2026-11-02 整体下线**
（`96d6a6f`）：`DataCenter` Protocol、`FixtureDataCenter`、`DataCenterHTTP`、
`build_datacenter`、`derive_style_sample_state`、`compute_funnel` 全部删除，11 个
`funnel.*` 平台指标随之从目录里消失。现在指标只有两种来路：

- **部门 emit 指标**（`dept.<CODE>.<slug>`）：由本页字段在渲染时算出来，见 §2；
- **四态分布**（`status.*`）：由条目的标准状态聚合。

```python
# app/oams/metric_format.py —— 这是唯一剩下的指标格式化入口
def format_metric(metric_key: str, value: float | int | None, unit: str = "") -> str
    # 63,289 件 / ¥1,745.87 万 / 5.18 / 59 款（unit 为空时只写数字）
```

**单位从哪来**：`unit` 一律由调用方传入，不在格式化函数里查表——`render.aggregate` 取
`MetricSpec.unit`（经 `metric_meta`），`api._format_metric` 取 binding 上若有的 `unit`；
部门指标的 binding **目前没有 `unit` 字段**（emit 只声明 `metric_key`/`agg`/`from_field`/
`where_status`），所以部门数字现在按「无单位」渲染，空串是合法的显示结果。要给部门指标
配单位，先在本契约里给 binding 加 `unit`，再实现，不要在渲染层硬编码。

展示类模块（`metric_card`/`metric_grid`/`rank_list`/`flow_strip`）能引哪些键、为什么
不许引别部门的，见 §2「display 能引什么」——一句话：**platform 指标，或本页自己 emit 的
指标；跨部门的一律拒**。

### 7.1 没有自己列的字段（金额）怎么参与汇总（2026-11-02 定）

条目表只有 `title`/`status`/`blocked_reason`/`progress_value`/`note`/`evidence` 六列，
其余标准字段（`amount`、`completed_at`、`planned_at`、`style_code`…）落在 `interface_entries.
extra`（jsonb）里。两条规矩：

- **填了不能丢**：`web._read_entry_form` 遇到「有标准含义、表里没有列」的字段，一律写进
  `extra`。以前这些字段被静默丢弃——页面上显示填过、库里查不到，部门指标自然永远算不出来。
- **金额可以求和**：`api.EXTRA_AGGREGATE_COLUMNS = ("amount",)` 允许 `amount` 作为 `from_field`
  参与 `sum`/`avg`/`count_distinct`；`api._column_value()` 先读列、再读 `extra`，两条读取路径
  （`aggregate_entries` 的多页合并、`build_department_aggregates` 的逐页取值）都走它。非数字
  值被跳过，不会被当成 0。
- 这一条只放开**金额**，没有放开所有 extra 字段：任意自定义字段进汇总会把「部门私有字段」
  变成公司可算的数字，那是另一个决定，要做先改本节。

---

## 8. 并行工作纪律

1. 只改自己表格里的文件；需要别人文件里的东西，按本文学名调用。
2. 任何公共签名变更 → 先报 Lead，Lead 改本文并通知全员。
3. 每个单元交付时必须带 **pytest 可跑的测试**（`.venv/bin/python -m pytest tests/<你的测试> -q` 全绿）。本机没有 PostgreSQL，**不要写需要数据库的测试**。
4. 测试留档一律用真断言（`assert`），不要再写 `print("PASS")` 那套手工脚本。
