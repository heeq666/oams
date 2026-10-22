# 界面描述格式（JSON Schema 草案 v0.1）

状态：**草案，随界面规则同步演进。** 这是 AI 与底座之间的契约：AI 产出这份描述，底座校验通过后渲染，底座不执行任何来自描述的逻辑。

## 设计原则

1. **描述而非代码。** 描述「用什么模块、什么字段、怎么映射、怎么校验」，不含脚本、不含查询、不含凭证。
2. **可校验。** 任何一份描述都能被底座静态检查，不通过就拒绝渲染并给出具体原因。
3. **可汇总。** 描述必须能让底座知道每条数据落在哪个标准字段上。
4. **可进化。** 自定义字段与自定义模块都带来源与统计信息，供每日扫描。

## 顶层结构

```json
{
  "schema_version": "0.1",
  "interface_id": "dept-design-sampling-v3",
  "name": "设计部打样进度",
  "org_unit_code": "DESIGN",
  "position_codes": ["DESIGN_LEAD"],
  "based_on": "public-weekly-basic-v1",
  "modules": [],
  "fields": [],
  "status_map": {},
  "rules": {},
  "meta": {}
}
```

| 键 | 必填 | 说明 |
|---|---|---|
| `schema_version` | 是 | 描述格式版本，底座据此校验 |
| `interface_id` | 是 | 界面唯一标识，部门内可用 slug |
| `name` | 是 | 界面名称，给人看 |
| `org_unit_code` | 是 | 所属部门代码 |
| `position_codes` | 是 | 可填写此界面的岗位代码列表 |
| `based_on` | 否 | fork 自哪个模板，便于回溯与升级 |
| `modules` | 是 | 模块数组，决定界面结构 |
| `fields` | 是 | 字段数组，每个字段必须给映射 |
| `status_map` | 是 | 部门状态词 → 标准状态 |
| `rules` | 是 | 校验规则 |
| `meta` | 是 | 作者、版本、审核状态 |

## modules[]

```json
{
  "key": "sampling_progress",
  "type": "progress_bar",
  "title": "打样进度",
  "order": 2,
  "source_fields": ["sample_done", "sample_total"],
  "aggregatable": true
}
```

| 键 | 说明 |
|---|---|
| `type` | 默认模块库中的一个，或 `custom` |
| `title` | 界面上显示的标题 |
| `order` | 展示顺序 |
| `source_fields` | 引用的字段 key |
| `aggregatable` | 是否参与公司汇总；false 表示仅本部门可见 |

**type = custom 时额外必填：**

```json
{
  "custom": {
    "purpose": "把款号清单与打样状态放一起看",
    "composed_of": ["list_block", "progress_bar"],
    "display": "table",
    "declares_logic": false
  }
}
```

`declares_logic` 必须为 false。若为 true 或出现脚本、查询、URL 调用字段，底座直接拒绝渲染。

## fields[]

```json
{
  "key": "sample_done",
  "label": "已完成打样数",
  "type": "number",
  "required": true,
  "map_to": "progress_value",
  "aggregatable": true,
  "example": "12"
}
```

| 键 | 必填 | 说明 |
|---|---|---|
| `key` | 是 | 字段标识 |
| `label` | 是 | 显示名，用部门术语素材 |
| `type` | 是 | text / number / percent / date / enum / link / file |
| `required` | 是 | 是否必填 |
| `map_to` | 是 | 标准字段名，或 `display_only` |
| `aggregatable` | 是 | 是否参与汇总；map_to 为 display_only 时必须 false |
| `example` | 是 | 示例值，供试填与校验演示 |

**标准字段可选值：** work_item_id、title、org_unit_id、position_id、status、blocked_reason、needs_decision、progress_value、note、evidence、display_only。

## status_map

```json
{
  "待打样": "planned",
  "打样中": "in_progress",
  "等面料": "blocked",
  "已完成": "done"
}
```

值只能是：planned / in_progress / blocked / done。未列出的部门词视为未映射，拒绝渲染。

## rules

```json
{
  "evidence_required": true,
  "blocked_reason_required_when_blocked": true,
  "max_note_length": 4000,
  "max_evidence_length": 4000,
  "progress_range": [0, 100],
  "review_required_for_public": true
}
```

## meta

```json
{
  "author_position_code": "DESIGN_LEAD",
  "version": 3,
  "visibility": "department",
  "review": {"status": "draft", "reviewed_by": null, "reviewed_at": null}
}
```

`visibility` 为 department 时仅本部门可见；要变成 public 必须 `review.status = approved` 且有审核人与时间。

## 底座校验清单（渲染前必须全部通过）

1. schema_version 受支持。
2. 必填顶层键齐全，org_unit_code 与 position_codes 都存在。
3. 每个 field 都有 map_to；map_to = display_only 时 aggregatable 必须为 false。
4. status_map 的值全部是标准状态。
5. 每个 module 的 source_fields 都在 fields 里存在。
6. type = custom 时必须带 custom 声明且 declares_logic 为 false。
7. 不含 script、query、url_fetch、credential 等被禁止的键。
8. visibility = public 时必须 review.status = approved。
9. 界面必须能产出全部标准字段：事项、部门、岗位、状态、依据、时间、提交人。

任一项不通过，返回**具体失败原因**，不做模糊报错、不降级渲染。

## 每日进化输入

底座每晚扫描所有界面描述，统计：

- field.map_to = display_only 且实际有值的字段，按使用的部门数统计。
- module.type = custom 的模块，按使用的部门数统计。

**≥2 个部门使用即生成候选提案**（跨部门共识），进入公司审核队列；通过则升级为标准字段或默认模块，写入素材库。
