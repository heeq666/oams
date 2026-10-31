# AGENTS.md — Company Workhub 协作约定

全公司工作进度与岗位协同系统。Python 3.11 + FastAPI + Jinja2 + htmx 服务端渲染，PostgreSQL。**没有前端构建链**（无 package.json / 无 tailwind / 无 React）：前端资产只有 `app/workhub/static/vendor/htmx.min.js`、`static/workhub-modules.css` 和各模板里的内联样式。

## 做界面（UI / 样式 / 组件）之前必读

1. `docs/design/design-system-v1.md` —— **界面唯一口径**：颜色/圆角/间距/字号令牌、三层结构（骨架层 / 组件层 / 模块层）与 38 个组件组 + 14 个模块组件的 Figma 节点 id、八条硬规则、验收清单。
2. `docs/design/tokens.json` —— 令牌机器可读镜像（从 Figma 变量导出 85 项）。
3. Figma：页面 `02 · 组件 Components`（`0:1`）是**组件与视觉的唯一真源**（主库 frame `140:3438`，自上而下：骨架层 → 图层组（基础件 / 表单 / 反馈 / 数据）→ 浮层 → 协作室 → 模块层）；令牌表与规格板在 `01 · 规范 Specs`（`3:89`），整页稿在 `03 · 页面 Pages`（`3:88`）。**三页分工：规则 / 积木 / 成品；Starter 计划只有 3 页，别再新建页面**，命名与归档规则见 `docs/design/figma-workspace-rules.md`。改视觉先改 Figma，再改代码。

六条最容易犯的错：

- **别引 shadcn/ui 的代码**。它要求 React + Tailwind + npx，本项目没有也不该有；我们只借它的**视觉语言**（白底、hairline、语义色、五档圆角、无阴影、一个强调色）。
- **别写字面颜色/圆角**。一律用 `--wh-*` 变量，值与 Figma 变量对齐；新增值先加令牌。
- **圆角按元件尺寸分档（学苹果）**：容器 **26**（卡片/弹窗/外壳浮层：账号菜单、开发模式菜单）、中等块 **18**（分段外框/组织导航/提示条/模块块/字段行）、行与高亮 **9**（菜单行/chip/侧栏行/骨架条）、**输入框/下拉/文本域走 18**（它们都在卡片里，是二级控件；30 高时被截到 15，看起来接近胶囊）、方框 **4**（复选框）；**卡片里拉出来的浮层（下拉框展开层、卡片内菜单）走 18**，不要跟着卡片再用 26；**999 只给按钮、搜索框、徽章、开关、页码；输入框/下拉/文本域别写 999**（它们写 18，30 高的盒子上自然渲染成胶囊，那是要的效果）；表格本体 0。不要出现第六种值（见 `docs/design/design-system-v1.md` §3）。连续曲率（`cornerSmoothing 0.6`）只有 Figma 有，CSS 保持圆弧。
- **排多列只用内容区 12 栅格**：模板里包 `.grid-12`，子元素标 `.span-12/8/6/4/3`（白名单五档，gap 固定 16），**不许手写 `grid-template-columns` / 任意 `fr` 比例**；有底的面板同行等高、无底的区块不撑高（两条选择器已写在外壳里）。口径、断点与例外见 `docs/design/layout-grid.md`，验收用 ego 浏览器跑（见「浏览器自动化」）。
- **间距三个数**：块与块 16、元素与元素 12、行内 8（**标签↔控件也是 8**，没有 6 这一档）。别写 10 / 14 / 18 这种（见同文件 §4）。依据：Figma `Field 表单行`（`140:3688`）两个变体 `itemSpacing` 都是 8，`Label Row` 高 20 → `Input` 顶 28、`Input` 底 58 → `Hint` 顶 66，全是 8；代码 `.field` 本来就是 8，2026-10-31 撤销「6」这一档，改的是文档不是代码。
- **控件基准线 30**：`input` / `select` / `button` 都是 30 高、内距 0·12（顶栏导航胶囊也是 30），高度靠 `height` 撑，不要用 `padding` 凑（会算出 35.5 / 36 这种档，跟按钮排一行就错位）；`textarea` 最小高 120。
- **嵌套圆角走同心值**：贴边内层（内距 ≥ 8）取 `内层 = 外层圆角 − 内边距`（卡片 26 + 内距 16 → 行 9；分段 18 + 内距 8 → 段 9）；内层圆角比它**小**反而会「咬角」，别靠调小内层解决（见同文件 §3.1，日常不用算）。

## 做部门界面（descriptor）之前必读

`skills/workhub-department-interface/SKILL.md` + `docs/architecture/p0-interface-contract.md`。硬约束：容器只排版不做数据；字段必须映射标准字段（至少 `title` / `status` / `evidence`）；状态必须映射到 `planned / in_progress / blocked / done`；模块只能从 `app/workhub/modules.py` 的 `MODULE_REGISTRY` 白名单（20 个 key）里选。

## 运行

```sh
python -m venv .venv && . .venv/bin/activate && pip install -e .
workhub migrate                       # 需要 WORKHUB_DATABASE_URL
workhub bootstrap-admin --name ... --employee-code ... --org-code ... --org-name ...
workhub serve
scripts/start-local-trial.sh          # 本机试用，默认开 WORKHUB_DEV_AUTOLOGIN=1
pytest                                # 带 db 标记的用例在没数据库时跳过
```

## 浏览器自动化

需要浏览器自动化的地方（三档截图、栅格检查、断网重发验收）**一律用 ego 浏览器**，不再新写自研 CDP / Playwright 脚本。

- 命令：`ego-browser`（按你自己的安装路径，一般已在 PATH 里）；动手前先读 skill（`ego-browser skill`）。
- 脚本写到 `/tmp/xxx.mjs`，用 `ego-browser nodejs < /tmp/xxx.mjs` 执行；**不要**用 `-e` 内联，遇引号会被截断。
- 视口没有 API，用 `page.cdp("Emulation.setDeviceMetricsOverride", {width, height: 900, deviceScaleFactor: 1, mobile: false})`。
- 三档验收（1440 / 1024 / 768）：断言 `document.documentElement.scrollWidth === clientWidth`，出图用 `page.screenshot({path})`。
- 验收脚本三个，都 ego 驱动，跑法 `ego-browser nodejs < scripts/xxx.mjs`：`ego-grid-check.mjs`（12 栅格三档量测）、`ego-collab-failure-check.mjs`（断网重发）、`ego-measure.mjs`（布局量测，服务三个布局用例）。自研 CDP 脚本已全部删除，**不许再新增**同类自研脚本。

## 目录

- `app/workhub/` —— 应用本体：`modules.py`（模块注册表）、`templates/`、`static/`、`mcp/`（12 个 MCP 工具）、`cli.py`
- `docs/architecture/` —— 契约与方案（`plugin-host-concept.md` 是现行主线，`v1-ui-redesign-plan.md` 已作废）；`collab-room-implementation-plan.md` 是协作室的实现清单（设计稿见 Figma 与 `docs/design/design-system-v1.md` §7）
- `docs/design/` —— 界面规范与令牌
- `db/migrations/` —— SQL 迁移；`examples/` —— 能过校验的 descriptor 样本；`skills/` —— 给外部 AI 的 skill

## 约定

- 中文优先：注释、文档、界面文案都用中文。
- 提交前跑 `pytest`；改 UI 要截图自检（1440 / 1024 / 768 三档，窄屏不许横向滚动），工具与命令见「浏览器自动化」。
- **验收 pytest 要两种机器状态都跑，并且看 skip 数**：本机有库 → **0 skipped**，这才是真绿；故意指向不存在的库 → 允许 skip，那只证明「无库不炸」。skip 数一涨就说明有库没被用上，别拿 `0 failed` 当验收口径。
- db 用例不许在 import 期写 `WORKHUB_DATABASE_URL`，地址一律由 fixture 注入。
- 不往生产库写、不连公网暴露 PostgreSQL；改动 `.env` 前先看 `.env.example`。
- **谁的写区谁提交**：改完自己 `git add` 自己写区内的文件、自己 commit、把哈希报上来。别攒着等统一提交——攒着会让线上跑旧代码。
- 只有确实会撞别人文件时才排队等，等的时候说清撞的是哪个文件。
- 提交前照旧跑 `pytest`；文档改动也要提交，不许只在工作区躺着。
- **`docs/reports/` 是当时的决策记录，不回头改**：发现旧报告与现状不符，改现行文档（`docs/architecture/`、`docs/design/`）并在里面说明，别重写历史报告。

### 共享库写操作：先锁行 id

演示站（`127.0.0.1:8000`，库 `55460`）或**任何共享库**上的写操作，动手之前必须先锁死目标行：

- **先断言，再动手**：点下去之前先取到目标行的 id（或标题）并与预期比对，不一致**立即中止**、报告，不许「差不多就点」。
- **不许按屏幕上的文字挑元素**：外层文字、卡片标题、按钮文案都可能重复，`getByText` 这类选择器在多卡片页面上会点错人。
- **SQL 同理**：`UPDATE` / `DELETE` 必须带 `WHERE id = ...`，改之前先 `SELECT` 把要动的行打出来核对；不许按 `name` / `title` / 状态这类非唯一条件批量改。
- 这是**数据事故的预防，不是代码风格**。实例：2026-10-31 一个按外层文字定位的按钮点在了另一张卡上，把一条待审页误批成通过（已还原）。

### 回报与派活（省 token）

- **回报固定四行，≤150 字**：`结论：一句话`｜`改动：文件:行 或提交哈希`｜`验证：命令 → 结果`｜`待办：还剩什么、要谁拍板`。不写散文、不复述过程。
- **长内容一律落盘**：diff、日志、清单、逐条现状、截图对照 → `docs/reports/<日期>-<主题>.md`（或进 commit message），消息里只发一段摘要 + 路径。禁止在消息里贴大段 diff、代码原文、报错堆栈。
- **派活用模板，≤200 字**：`目标` / `写区（具体文件路径）` / `验收（命令 + 期望结果）` / `时限`。不抄需求原文与背景解释，对方有项目文档，让它自己读。
- **凡是「系统在什么时候做某件事」这类判断，要么写清依据哪一行代码，要么明说「你去核实」**：凭印象写的判断会被下游当成事实执行（实例：派活说「三条判据都在提交时判」，实际是部门初审点「通过」那一刻才判，下游照抄差点写错文档）。
- **不重复共识**：已拍过的事不再复述理由，只有真正有分歧才展开；同一事实一条消息里只说一遍。
- **一轮一条消息**：不发「补充说明」「另外再补充」，多条合并；只发该看的人，不全员广播。
- **Bot 间消息用单行紧凑字段**（≤60 字，`;` 分隔，没有的字段省略）：

  `S=状态;T=主题≤10字;F=文件路径;V=验证命令:结果;C=提交哈希;Q=要谁拍板什么;P=详情文件路径`

  状态只有四个：`done` / `doing` / `blocked` / `need`。例：`S=done;T=descriptor禁词;F=app/workhub/descriptor.py;V=pytest:649/0/0;C=82331b5;Q=none`
  派活用同一套：`S=task;T=任务;F=写区;V=验收;D=时限`。
  例行进度、交付回执、验收结果一律用这个；细节走 `P=docs/reports/<日期>-<主题>.md`。
  **例外**：有分歧、要在方案里选、需要解释判断的，用自然语言三句以内，别硬压成机器格式——压错返工更贵。
  **给老大的汇报绝不用这个格式**，仍然大白话（优先级最高）。
