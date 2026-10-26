# AGENTS.md — Company Workhub 协作约定

全公司工作进度与岗位协同系统。Python 3.11 + FastAPI + Jinja2 + htmx 服务端渲染，PostgreSQL。**没有前端构建链**（无 package.json / 无 tailwind / 无 React）：前端资产只有 `app/workhub/static/vendor/htmx.min.js`、`static/workhub-modules.css` 和各模板里的内联样式。

## 做界面（UI / 样式 / 组件）之前必读

1. `docs/design/design-system-v1.md` —— **界面唯一口径**：颜色/圆角/间距/字号令牌、23 个组件清单与 Figma 节点 id、八条硬规则、验收清单。
2. `docs/design/tokens.json` —— 令牌机器可读镜像（从 Figma 变量导出 83 项）。
3. Figma：页面 `02 · 组件库 Components`（`0:1`）是**组件与视觉的唯一真源**；改视觉先改 Figma，再改代码。

三条最容易犯的错：

- **别引 shadcn/ui 的代码**。它要求 React + Tailwind + npx，本项目没有也不该有；我们只借它的**视觉语言**（白底、hairline、语义色、五档圆角、无阴影、一个强调色）。
- **别写字面颜色/圆角**。一律用 `--wh-*` 变量，值与 Figma 变量对齐；新增值先加令牌。
- **圆角五档：微 4 / 小控件 12 / 控件 20 / 卡片 28 / 胶囊 999**（表格本体 0），不要出现第六种值（见 `docs/design/design-system-v1.md` §3）。

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
- `docs/architecture/` —— 契约与方案（`plugin-host-concept.md` 是现行主线，`v1-ui-redesign-plan.md` 已作废）
- `docs/design/` —— 界面规范与令牌
- `db/migrations/` —— SQL 迁移；`examples/` —— 能过校验的 descriptor 样本；`skills/` —— 给外部 AI 的 skill

## 约定

- 中文优先：注释、文档、界面文案都用中文。
- 提交前跑 `pytest`；改 UI 要截图自检（1440 / 1024 / 768 三档，窄屏不许横向滚动）。
- 不往生产库写、不连公网暴露 PostgreSQL；改动 `.env` 前先看 `.env.example`。
