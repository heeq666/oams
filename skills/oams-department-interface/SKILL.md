---
name: oams-department-interface
description: 你来把部门这一页做出来——问清几件事、自己查底、自己写、自己验、自己提交，人只回答问题、点一次允许、点一次通过
---

# 你来做这一页

这一页的活是你干的，不是人干的。分工只有三句：

- **你做**：问清、查底、写描述、自检、提交、按反馈改。全流程你自己跑完，不要中途把人拉来一起做。
- **人做三件**：回答你的问题（你一次问完）、点一次「允许」（把票给你）、点一次「通过」（审核上线）。
- **不要给人加任务**：不要让他读文档、不要让他学格式、不要让他去查数据、不要给他布置作业。他不该知道 descriptor 是什么词——那是你的内部叫法。

## 第一步：一次问完，别一问一答

把下面一屏问完，用他的话问（别说字段、别说模块）：

1. 这一页记的是什么事？平时谁填？
2. 一条记录上大概有哪些信息？（让他用嘴说，你自己归纳成字段）
3. 现在怎么同步？群里报、表格里填、还是开会说？
4. 这件事从开始到结束，中间会经过哪几个状态？（照他的话记，别翻译）
5. 卡住的时候一般卡在哪、在等谁？
6. 说「这条完成了」的时候，拿什么当依据：单号、链接、还是照片？
7. 一周或者一个波次大概几条？
8. 需要跟手上已经在跟的某件事挂上吗？

问完**复述一遍**给他确认，两三句话，例如：「我理解这一页是行政部的差旅申请台账：谁申请、去哪、几天、什么状态、卡在哪、批文链接；状态是待批/已批/已出发/已回来。对吗？」——他点头之前不要动手写。

问不到的，按默认走并在提交时说一句，不要为了问清把人卡住：状态没有就 `待办/进行中/卡住/已完成`；依据没有就留空可填。

## 第二步：先自己查底，不要问他

他答不出「你属于哪个部门、有哪些岗位」这类事，工具知道。按顺序调：

1. `oams_departments` —— 你是谁（代表哪位同事）、能给哪个部门建页、那个部门的岗位码、别人开给你的可参考页、票什么时候到期。
2. 有可参考的页 → `oams_page_definition` 取那份定义（字段、模块、版式、指标全在里头）。
3. `oams_my_work_items` —— 他手上有哪些在跟的事，能挂就挂上（挂上才会进公司看板）。

判读：`departments` 里没有你要的部门 → 停，走「必须停下来找人的三件事」第 1 条；有 `reference_pages` → 优先照它做（见第五步）。

## 第三步：定字段

字段 key 用英文小写加下划线，label 用他的原话。**三件套必做**，缺一个公司汇总就读不出这一行：

| 他要说的 | key 举例 | map_to |
| --- | --- | --- |
| 这条是什么（一句话能认出是哪件事） | `subject` | `title` |
| 现在什么状态 | `stage` | `status` |
| 依据 | `approval_no` | `evidence`（放 URL 就 `evidence_url`） |

其余按意思对号入座（37 个标准名，常用的）：`blocked_reason` 卡在哪、`needs_decision` 等谁拍板、`progress_value` 完成度、`note` 备注、`due_at` 期限、`count`/`quantity`/`amount` 数量金额、`supplier_code`/`order_no`/`style_code` 这类单据号。

对不上标准名的**部门私有信息**（比如「会议室是否已订」）：`map_to: "display_only"` + `aggregatable: false`。值会存进 `extra`，页面照样显示、照样能查件，只是不进汇总。

每个字段给 `required`（真正必须的才 true，否则人会嫌烦）、`example`（填一个真样品，人和 AI 后续都照着填）。

## 第四步：状态词映射

他怎么说就怎么记，逐个映射到四个标准态之一：

`status_map` 就长这样：

```json
{"待批": "planned", "已批": "in_progress", "卡住": "blocked", "已回来": "done"}
```

规矩：`status` 字段 `options` 里出现的每一个词都必须在这张表里；`blocked` 那一档要能写清卡在哪、在等谁（`blocked_reason` / `needs_decision` 两个字段）。

## 第五步：拼版面——用默认组合，不要问他选哪个模块

默认三块就够，绝大多数部门页照这个做：

- 录入：`progress_row`（一条一行的进度）或 `table`（信息多、纯记录）
- 卡点：`blocker_list`
- 汇总：`metric_grid` 或 `status_chart`，加 `note_block` 写一句怎么填

有参考页时：**直接照抄它的模块结构**，只换字段、状态、文案、指标前缀，别自己重新设计。

两条死规矩：容器（`columns`/`rows`/`grid`/`card`/`tabs`/`divider`/`spacer`）只排版，**里头一个字都不能直接写**——要写话用 `note_block`；容器最多套两层（`columns` 里放 `card` 里再放叶子就是三层，会被拒）。

## 第六步：写成 JSON（别写 YAML）

工作台只收 JSON。你是 AI，直接产出 JSON，不需要转换工具、也不需要让人去转。

```json
{
  "schema_version": "0.2",
  "interface_id": "travel-request",
  "name": "差旅申请",
  "org_unit_code": "OA",
  "position_codes": ["OA_LEAD"],
  "capabilities": ["entries:write", "entries:read:dept", "aggregate:read:dept"],
  "modules": [
    {"key": "board", "type": "columns", "order": 1, "params": {"columns": 2, "gap": "md"},
     "children": [
       {"key": "left", "type": "card", "order": 1, "params": {"title": "本周申请"},
        "children": [
          {"key": "rows", "type": "progress_row", "order": 1, "module_version": "^1.0",
           "source_fields": ["subject", "stage", "progress", "blocker", "approval_no"],
           "aggregatable": true},
          {"key": "blockers", "type": "blocker_list", "order": 2, "module_version": "^1.0",
           "source_fields": ["subject", "blocker", "owner"], "aggregatable": true}
        ]},
       {"key": "right", "type": "card", "order": 2, "params": {"title": "汇总"},
        "children": [
          {"key": "spread", "type": "status_chart", "order": 1, "module_version": "^1.0",
           "source_fields": ["stage"], "params": {"dimension": "status"}},
          {"key": "howto", "type": "note_block", "order": 2, "module_version": "^1.0",
           "params": {"text": "每笔一条；有卡点就写清卡在哪、在等谁"}}
        ]}
     ]}
  ],
  "fields": [
    {"key": "subject", "label": "申请事由", "type": "text", "required": true,
     "map_to": "title", "aggregatable": true, "example": "深圳客户回访"},
    {"key": "stage", "label": "状态", "type": "enum", "required": true, "map_to": "status",
     "aggregatable": true, "options": ["待批", "已批", "卡住", "已回来"], "example": "已批"},
    {"key": "progress", "label": "进度", "type": "percent", "required": false,
     "map_to": "progress_value", "aggregatable": true, "example": "60"},
    {"key": "blocker", "label": "卡在哪", "type": "text", "required": false,
     "map_to": "blocked_reason", "aggregatable": true, "example": "等财务预支"},
    {"key": "owner", "label": "等谁批", "type": "text", "required": false,
     "map_to": "needs_decision", "aggregatable": true, "example": "财务部负责人"},
    {"key": "approval_no", "label": "审批单号或链接", "type": "text", "required": true,
     "map_to": "evidence", "aggregatable": true, "example": "OA-2026-1031"}
  ],
  "status_map": {"待批": "planned", "已批": "in_progress", "卡住": "blocked", "已回来": "done"},
  "rules": {"evidence_required": true, "blocked_requires_reason": true},
  "meta": {"author_position_code": "OA_LEAD", "version": 1,
           "visibility": "department", "review": {"status": "draft"}}
}
```

必须换成你自己的：`interface_id`（新页编号）、`name`、`org_unit_code`、`position_codes`、`meta.author_position_code`——这四个是「这一页属于谁」的印记，照抄别人的等于做出一页挂错部门的残页。

## 第七步：能力与指标

`capabilities` 没声明就没有权限。能写的只有六条：`entries:write`（填数据）、`entries:read:dept`（看本部门条目）、`entries:read:self`（只看自己填的）、`aggregate:read:dept`（本部门汇总）、`aggregate:read:company`（进公司汇总）、`entries:link:work_item`（挂到工作项）。常见组合就是示例里那三条。

**`export:dept` 别写**：词表里有这个名字，但 v1 没有实现，写了直接被拒（`CAPABILITY_NOT_IMPLEMENTED`）。

要指标就自己 emit，命名必须是 `dept.<本部门CODE>.<名字>`：

```json
[
  {"metric_key": "dept.OA.blocked_count", "mode": "emit", "agg": "count",
   "from_field": "stage", "where_status": "blocked"}
]
```

绑定只认七个键：`metric_key`/`mode`/`agg`/`from_field`/`where_status`/`label`/`unit`。筛选一律用 `where_status`，值只能是 `planned`/`in_progress`/`blocked`/`done`，部门自己的词先在 `status_map` 里映射。

## 第八步：自检——按错误码改，不要猜

调 `oams_check_descriptor`（有票时同步真站点校验，还能查到部门/岗位是否真存在）。报错都带码，照着改：

| 码 | 意思 | 怎么改 |
| --- | --- | --- |
| `FIELD_UNMAPPED` | 字段没定去处 | 给 `map_to`；对不上标准名就用 `display_only` + `aggregatable:false` |
| `STATUS_UNMAPPED` | 有状态词没映射 | 补进 `status_map`，或从 `options` 里删掉这个词 |
| `METRIC_UNDEFINED` | 引了不存在的指标 | 本页 emit 一个 `dept.<CODE>.<名>`，或改成引用目录里有的平台指标 |
| `CAPABILITY_ESCALATION` | 声明了词表外的能力 | 只用那六条 |
| `CAPABILITY_NOT_IMPLEMENTED` | 写了 v1 没实现的能力 | 删掉 `export:dept` |
| `MODULE_NOT_WHITELISTED` | 模块名不在白名单 | 只用那 21 个（7 容器 / 6 录入 / 8 展示） |
| `LAYOUT_TOO_DEEP` | 容器套太深 | 最多两层：容器里放容器，叶子在第二层 |
| `DATA_RULE_VIOLATION` | 数据对不上 | 看它写的路径：部门码/岗位码不存在、字段重复、`display_only` 却 `aggregatable:true` |

改到通过为止。不要用「把报错的那条删掉」当解决办法——删除会让缺口换个地方冒出来。

## 第九步：提交，然后自己回读

`oams_submit_page` 建新页要三样：`slug`、`name`、`descriptor`（只给 `interface_id` 是给已有草稿补送审）。提交后：

1. 回读一次状态（工具返回里带 `status`），**不要凭「成功了」三个字下结论**；报出来的是 `pending`、不是「已通过」，就这么说：「已经提交，等审核通过就上线」。
2. 说清一件事：审核要点的是**两版之间的差异**，不是整页重看；`pending` 期间旧版照常能用，不会中断部门干活。

## 第十步：人试填之后

他会说「这个字段太多」「这条我填不下去」。做法：只改他指出的那一处，改完自己再跑一遍自检再提交；**不要顺手加字段、不要顺手改版式**——他没提的地方改了，退回的差异会变大，人更难签。

## 必须停下来找他：只有三件事

说一句话加一个点哪就行，不解释原理、不布置作业：

1. **部门或岗位在系统里还没有**：`[DATA_RULE_VIOLATION] org_unit_code: unknown department 'OA'`。你要说：「系统里还没有 OA 这个部门（岗位 OA_LEAD 也一起缺），请管理员在「部门与岗位」里建一下，建好我立刻接着做。」
2. **要碰别的部门的数据**：你的票只在授权范围内有效。你要说：「这需要 XX 部把那一页开成「可作参考」，或者给我那边的权限——权限管理员点一下就行。」
3. **要看别人已经填过的内容**：参考权只给页面定义，不给条目。你要说：「那一页只开了「可作参考」，看不到里面填了什么；要看得请他们开「可查看」。」

## 硬边界（碰了就是事故）

- 你永远不能自己批自己：拿不到 `plugin:approve`，设计上做不到。
- 你不能自己造工作项（`POST /v1/work-items` 对票是关的）——只能在已有的工作项上填。
- 管理面（部门、成员、授权、审计）对票一律 403。
- 参考权只读定义，读条目 403。
- 不要直连数据库、不要绕过工具改库；验收一律看页面和接口的实际状态。

## 反面模式

- 给人布置作业：让他先建部门、先读契约、先学 JSON——他不需要，你做。
- 跟他讲术语：说「descriptor 里 metrics 加一条 emit」等于没说；说「我会加一个『卡住多少条』的数字」。
- 拿他的话当字段名却把 `label` 写成英文：界面是给部门同事看的。
- 在容器里硬塞文字、把备注写成字段外的段落：都会被拒或看不见。
- 只做漂亮表单不映射字段：汇不进公司看板，这一页等于没做。
- 把进度挂在人名上：换人即断，责任人写岗位。
- 依据写「已确认」这种主观话：不可信，比没有更危险，要单号/链接/时间。
- 提交完说「已上线」：它还在待审，说清楚。
