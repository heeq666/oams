---
name: oams-department-interface
description: 按 OAMS 插件契约（descriptor v0.2）帮部门负责人制作本部门进度汇报界面，可离线校验、预览、提审
---

# 部门插件制作（OAMS 协同底座）

你协助部门负责人制作**本部门的进度汇报界面**。这不是设计自由网页，而是在底座契约内用**预设模块**拼装界面。你产出的是 **descriptor v0.2**（一份 YAML/JSON），由底座校验、渲染、汇总。

## 开工前必读（按顺序）

1. `docs/architecture/plugin-host-concept.md` —— 三条共识、六个名词、三张契约。
2. `docs/architecture/p0-interface-contract.md` —— **模块注册表（20 个，key 冻结）、指标目录、descriptor v0.2 结构、能力集合、标准字段词汇表（37 项）**。
3. `examples/production-dept.yaml` —— 一份能过校验的真实样本，照它的骨架改最快。

## 三条硬约束（违反即被校验器拒绝）

1. **容器只做排版，不做数据。** 容器（`columns` / `rows` / `grid` / `card` / `tabs` / `divider` / `spacer`）里不能直接写字、不能直连数据；任何数值和文字都必须由**预设模块**产生。容器最多嵌套两层。
2. **字段必须映射到标准字段或显式 `display_only`。** 界面必须能产出公司汇总需要的每个字段（至少 `title` / `status` / `evidence` 三件套），否则报 `aggregate: no field maps to 'title', so this interface cannot be summarised`。`map_to=display_only` 的字段必须写 `aggregatable: false`。
3. **状态必须映射。** 部门行话经 `status_map` 映射到 `planned / in_progress / blocked / done`，未映射的状态词不许存在。阻塞必须写清卡在哪、在等谁（`blocked_reason`）。

## 用法

写完后先自检，不要直接提审：

```sh
python -m oams.cli plugin check  my-dept.yaml          # 校验，逐条列出问题与错误码
python -m oams.cli plugin preview my-dept.yaml --out /tmp/p.html   # 渲染预览
python -m oams.cli plugin upgrade old.yaml             # 存量 0.1 描述自动升级
```

也可以走 MCP 通道让 AI 直接写草稿（`app/oams/mcp/`，12 个工具）：只读 `list_modules` / `list_canonical_fields` / `list_metric_catalog` / `get_plugin` / `list_plugins` / `preview_descriptor`；草稿写入 `create_draft_plugin` / `patch_draft_module` / `set_field_mapping` / `set_status_map` / `get_draft` / `submit_for_review`。

**AI 令牌只绑 `plugin:write:draft`，永远拿不到 `plugin:approve`。** 上架必须人工在审核页点通过，AI 不能自己让自己上线。

## 工作流程

1. **问清部门与主事项。** 部门名、岗位、每周或每波段重复汇报的事项、现在怎么同步（群 / 表 / 会）。
2. **列出部门自己的字段与黑话。** 每个字段决定 ``map_to``（标准字段 / `display_only`）与 `aggregatable`。
3. **选模块。** 只能从 20 个预设模块里选（7 容器 / 6 录入 / 7 展示）。默认优先 `progress_row` + `blocker_list` 做录入、`metric_grid` + `flow_strip` 做展示。
4. **写状态映射表。** 部门每个状态词 → 四个标准状态之一。
5. **声明能力与指标。** `capabilities` 没有声明就没有权限（`entries:write` / `entries:read:dept` / `entries:read:self` / `aggregate:read:dept` / `aggregate:read:company` / `entries:link:work_item` / `export:dept`）；部门自建指标命名必须是 `dept.<ORG_CODE>.<slug>`，平台指标只能引用不能改口径。
6. **check → preview → 试填一遍。** 用一个代表性事项走完整流程：能不能 5 分钟填完；缺依据时是否被拦住；填完能不能进公司汇总（需要关联到工作项）。
7. **提审。** 每一版都要过审，但审核看的是 **diff**；`pending` 期间旧版照常服务，不会中断部门使用。

## 反面模式

- 只做一个漂亮表单但字段没映射 → 汇不进公司看板。
- 用部门行话当状态且不映射 → 管理层看不出阻塞。
- 在容器里硬塞文字 → 校验器直接拒绝；文字要放进 `note_block`。
- 把进度挂在人名上 → 换人即断，责任人写岗位。
- 依据写主观描述（「已确认」）→ 不可信，比没有更危险；要写单号/文件名/时间。
- 指望 AI 自己上架 → 拿不到 `plugin:approve`，设计上做不到。
