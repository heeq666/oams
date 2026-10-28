# AGENTS.md — Company Workhub 协作约定

全公司工作进度与岗位协同系统。Python 3.11 + FastAPI + Jinja2 + htmx 服务端渲染，PostgreSQL。**没有前端构建链**（无 package.json / 无 tailwind / 无 React）：前端资产只有 `app/workhub/static/vendor/htmx.min.js`、`static/workhub-modules.css` 和各模板里的内联样式。

## 做界面（UI / 样式 / 组件）之前必读

1. `docs/design/design-system-v1.md` —— **界面唯一口径**：颜色/圆角/间距/字号令牌、三层结构（骨架层 / 组件层 / 模块层）与 38 个组件组 + 14 个模块组件的 Figma 节点 id、八条硬规则、验收清单。
2. `docs/design/tokens.json` —— 令牌机器可读镜像（从 Figma 变量导出 85 项）。
3. Figma：页面 `02 · 组件库`（`0:1`）是**组件与视觉的唯一真源**（主库 frame `140:3438`，自上而下：骨架层 → 组件层（基础件 / 表单 / 反馈 / 数据）→ 浮层 → 协作室 → 模块层）；页面稿在 `03 · 页面稿`（`3:88`，12 张整页稿 + 滚动时序 + `148:9383` 状态矩阵）。**组件库页只放组件，页面稿不许留在组件库页。** 改视觉先改 Figma，再改代码。

六条最容易犯的错：

- **别引 shadcn/ui 的代码**。它要求 React + Tailwind + npx，本项目没有也不该有；我们只借它的**视觉语言**（白底、hairline、语义色、五档圆角、无阴影、一个强调色）。
- **别写字面颜色/圆角**。一律用 `--wh-*` 变量，值与 Figma 变量对齐；新增值先加令牌。
- **圆角按元件尺寸分档（学苹果）**：容器 **26**（卡片/弹窗/外壳浮层：账号菜单、开发模式菜单）、中等块 **18**（分段外框/组织导航/提示条/模块块/字段行）、行与高亮 **9**（菜单行/chip/侧栏行/骨架条）、**输入框/下拉/文本域走 18**（它们都在卡片里，是二级控件；30 高时被截到 15，看起来接近胶囊）、方框 **4**（复选框）；**卡片里拉出来的浮层（下拉框展开层、卡片内菜单）走 18**，不要跟着卡片再用 26；**999 只给按钮、搜索框、徽章、开关、页码；输入框/下拉/文本域别写 999**（它们写 18，30 高的盒子上自然渲染成胶囊，那是要的效果）；表格本体 0。不要出现第六种值（见 `docs/design/design-system-v1.md` §3）。连续曲率（`cornerSmoothing 0.6`）只有 Figma 有，CSS 保持圆弧。
- **排多列只用内容区 12 栅格**：模板里包 `.grid-12`，子元素标 `.span-12/8/6/4/3`（白名单五档，gap 固定 16），**不许手写 `grid-template-columns` / 任意 `fr` 比例**；有底的面板同行等高、无底的区块不撑高（两条选择器已写在外壳里）。口径、断点与例外见 `docs/design/layout-grid.md`，验收跑 `node scripts/cdp-grid-check.mjs`。
- **间距三个数**：块与块 16、元素与元素 12、行内 8（标签↔控件 6）。别写 10 / 14 / 18 这种（见同文件 §4）。
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

## 目录

- `app/workhub/` —— 应用本体：`modules.py`（模块注册表）、`templates/`、`static/`、`mcp/`（12 个 MCP 工具）、`cli.py`
- `docs/architecture/` —— 契约与方案（`plugin-host-concept.md` 是现行主线，`v1-ui-redesign-plan.md` 已作废）；`collab-room-implementation-plan.md` 是协作室的实现清单（设计稿见 Figma 与 `docs/design/design-system-v1.md` §7）
- `docs/design/` —— 界面规范与令牌
- `db/migrations/` —— SQL 迁移；`examples/` —— 能过校验的 descriptor 样本；`skills/` —— 给外部 AI 的 skill

## 约定

- 中文优先：注释、文档、界面文案都用中文。
- 提交前跑 `pytest`；改 UI 要截图自检（1440 / 1024 / 768 三档，窄屏不许横向滚动）。
- 不往生产库写、不连公网暴露 PostgreSQL；改动 `.env` 前先看 `.env.example`。
