---
name: oams-department-interface
description: 按 OAMS 插件契约（descriptor v0.2）帮部门负责人制作本部门进度汇报界面，可离线校验、预览、提审
---

# 部门插件制作（OAMS 协同底座）

你协助部门负责人制作**本部门的进度汇报界面**。这不是设计自由网页，而是在底座契约内用**预设模块**拼装界面。你产出的是 **descriptor v0.2**（一份 YAML/JSON），由底座校验、渲染、汇总。

## 开工前必读（按顺序）

1. `docs/architecture/plugin-host-concept.md` —— 三条共识、六个名词、三张契约。
2. `docs/architecture/p0-interface-contract.md` —— **模块注册表（21 个，key 冻结）、指标目录、descriptor v0.2 结构、能力集合、标准字段词汇表（37 项）**。
3. `examples/production-dept.yaml` —— 一份能过校验的真实样本，照它的骨架改最快。

## 三条硬约束（违反即被校验器拒绝）

1. **容器只做排版，不做数据。** 容器（`columns` / `rows` / `grid` / `card` / `tabs` / `divider` / `spacer`）里不能直接写字、不能直连数据；任何数值和文字都必须由**预设模块**产生。容器最多嵌套两层。
2. **字段必须映射到标准字段或显式 `display_only`。** 界面必须能产出公司汇总需要的每个字段（至少 `title` / `status` / `evidence` 三件套），否则报 `aggregate: no field maps to 'title', so this interface cannot be summarised`。`map_to=display_only` 的字段必须写 `aggregatable: false`。
3. **状态必须映射。** 部门行话经 `status_map` 映射到 `planned / in_progress / blocked / done`，未映射的状态词不许存在。阻塞必须写清卡在哪、在等谁（`blocked_reason`）。

## 六道墙：先做这六件事（2026-11-02 外部 AI 真撞过）

这六条不是风格建议，是**先把前置做完**的顺序。每条后面跟一句反例——照着反例做，校验器或审核页一定拦你。

1. **先在库里把部门、岗位、成员建好，再写 descriptor。**
   先做：让管理员确认 `org_unit_code` 那个部门、`position_codes` 里的岗位、以及将来签字的人都已经存在（产品里走部门/岗位/成员管理，或 `scripts/load_department_data.py`）。
   反例：写成 `org_unit_code: IT` 但库里没有 IT 部 —— `plugin check`（不连库）照样「OK 通过」，贴进工作台才报 `[DATA_RULE_VIOLATION] org_unit_code: unknown department 'IT'`，岗位不存在同样报 `unknown position 'IT_LEAD'`。同理，`meta.review.reviewed_by` 只有在 `oams plugin check --database` 时才连库核对工号；不带这个开关，报告里会写明这条没验。

2. **部门里得有人能签：勾了「部门负责人」的那个人。**
   先做：提交前确认签字的人已经存在、并且有本部门的 `workforce.interfaces/review` 权限（部门审核人只能签本部门页面，公司终审另需 `review_company`）。
   注意：2026-11-04 起**看权限不看作者**——同一个人写完、自己签也行；系统管理员能签任何部门任何阶段。反例只剩「部门里没有任何一个人有 `review` 权限」：页面会停在「部门初审」没人能签。要公开、进公司汇总或跨部门沿用时，还会进公司终审，那一关部门里的人签不了。

3. **想让它出现在页面上的字段，必须先在 `fields` 里声明。**
   先做：每个字段给 `key` + 人话 `label`，选好 `map_to`；**没有标准列可映射的字段**（工单号、承诺完成日、照片链接）也照样声明——值会存进 `extra[字段key]`，页面按本页 `label` 显示，查件框也按它查。
   反例：把工单号写进备注正文、不声明字段 → 列表里没有这一列、查件框也查不到；声明了却没有任何模块引用它，2026-11-02 那页实测整页出现 0 次（现在整页会补一行只读显示，但前提仍然是「声明过」）。

4. **依据类字段只能放链接，底座没有上传文件的能力。**
   先做：`map_to: evidence_url` 的字段填 `http` / `https` 网址；要放截图，先上传到部门自己的地方，再把链接贴进来。
   反例：在说明书或界面里写「在此上传现场照片」→ 表单一栏就是个文本框，填不了文件；`javascript:`、`data:` 这类地址会被当普通文本渲染，不会变成可点的链接。

5. **指标绑定只有七个键，状态条件只收四个标准态。**
   先做：`mode: emit` 的绑定只写 `metric_key`、`mode`、`agg`、`from_field`、`where_status`、`label`、`unit`；筛选状态一律用 `where_status`，值取 `planned` / `in_progress` / `blocked` / `done` 之一。
   反例：写 `where: 紧急`（想筛部门自己的「紧急」）→ 直接报「不认识的键 'where'」并列出支持的键；部门词要先在 `status_map` 里映射到标准态，指标只认标准态。

6. **YAML 只是书写格式，工作台只收 JSON —— 用 `convert` 转，别手搓 python。**
   先做：`oams plugin check my-dept.yaml` 验过之后，`oams plugin convert my-dept.yaml -o my-dept.json`，把那份 JSON 贴进工作台「新建页面」的描述框（那里只做 `json.loads`）。不写 `-o` 就直接打到 stdout，可以接着 `plugin check my-dept.json` 再验一遍。
   反例：照旧办法手搓 `python -c "import yaml…"` —— 这台机器的 PATH 里没有 `python`（只有 `python3`），系统的 `python3` 又没装 PyYAML，会撞 `ModuleNotFoundError: No module named 'yaml'`，而且看起来像「描述写坏了」，实际什么都没坏。`convert` 用的是项目自己的解析器，你不需要装任何东西。

## 用法

写完后先自检，不要直接提审：

```sh
python -m oams.cli plugin check  my-dept.yaml          # 校验，逐条列出问题与错误码
python -m oams.cli plugin preview my-dept.yaml --out /tmp/p.html   # 渲染预览
python -m oams.cli plugin convert my-dept.yaml -o my-dept.json   # YAML → JSON（贴进工作台用）
python -m oams.cli plugin upgrade old.yaml             # 存量 0.1 描述自动升级
```

也可以走 MCP 通道让 AI 直接写草稿（`app/oams/mcp/`，12 个工具）：只读 `list_modules` / `list_canonical_fields` / `list_metric_catalog` / `get_plugin` / `list_plugins` / `preview_descriptor`；草稿写入 `create_draft_plugin` / `patch_draft_module` / `set_field_mapping` / `set_status_map` / `get_draft` / `submit_for_review`。

**AI 令牌只绑 `plugin:write:draft`，永远拿不到 `plugin:approve`。** 上架必须人工在审核页点通过，AI 不能自己让自己上线。

## 工作流程

1. **问清部门与主事项。** 部门名、岗位、每周或每波段重复汇报的事项、现在怎么同步（群 / 表 / 会）。
2. **列出部门自己的字段与黑话。** 每个字段决定 ``map_to``（标准字段 / `display_only`）与 `aggregatable`。
3. **选模块。** 只能从 21 个预设模块里选（7 容器 / 6 录入 / 8 展示）。默认优先 `progress_row` + `blocker_list` 做录入、`metric_grid` + `flow_strip` 做展示。
4. **写状态映射表。** 部门每个状态词 → 四个标准状态之一。
5. **声明能力与指标。** `capabilities` 没有声明就没有权限，能声明的只有六条：`entries:write` / `entries:read:dept` / `entries:read:self` / `aggregate:read:dept` / `aggregate:read:company` / `entries:link:work_item`。词汇表里还有第七条 `export:dept`（导出本部门明细），**v1 没有实现，别写**——写了校验直接拒，错误码 `CAPABILITY_NOT_IMPLEMENTED`；需要导出就跟部门说用页面上的现有入口。部门自建指标命名必须是 `dept.<ORG_CODE>.<slug>`，平台指标只能引用不能改口径。
6. **check → preview → 试填一遍。** 用一个代表性事项走完整流程：能不能 5 分钟填完；缺依据时是否被拦住；填完能不能进公司汇总（需要关联到工作项）。
7. **提审。** 每一版都要过审，但审核看的是 **diff**；`pending` 期间旧版照常服务，不会中断部门使用。

## 反面模式

- 只做一个漂亮表单但字段没映射 → 汇不进公司看板。
- 用部门行话当状态且不映射 → 管理层看不出阻塞。
- 在容器里硬塞文字 → 校验器直接拒绝；文字要放进 `note_block`。
- 把进度挂在人名上 → 换人即断，责任人写岗位。
- 依据写主观描述（「已确认」）→ 不可信，比没有更危险；要写单号/文件名/时间。
- 指望 AI 自己上架 → 拿不到 `plugin:approve`，设计上做不到。
- 部门只有作者一个人就提审 → 没人能签，页面卡在部门初审（见「六道墙」第 2 条）。
- 把 YAML 直接贴进工作台 → 那里的描述框只收 JSON（见「六道墙」第 6 条）。
