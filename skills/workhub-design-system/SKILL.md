---
name: workhub-design-system
description: 按 Workhub 设计规范 v1 做页面/组件（shadcn 语义色、hairline、五档圆角、令牌化），含 Figma 组件库定位与八条硬规则
---

# Workhub 设计规范（做界面前加载）

你在给 Workhub（全公司工作进度与岗位协同系统）做界面。它的视觉口径是 **shadcn 的语义色 + 白底 / hairline 分界 / 五档圆角 / 无阴影 / 一个强调色**。你不需要、也不应该安装 shadcn/ui（它要 React + Tailwind，本项目是 FastAPI + Jinja2 + htmx 服务端渲染，无前端构建链）。

## 先读这两个

1. `docs/design/design-system-v1.md` —— 令牌表、23 个组件清单、八条硬规则、验收清单。
2. `docs/design/tokens.json` —— 83 个令牌的机器可读值（Figma 导出）。

Figma 是唯一真源：页面 `0:1`（`02 · 组件库 Components`），外壳组件在 `128:3402`，容器内/外组件在 `140:3438`。

## 令牌钩子（代码里用这些，不写字面值）

颜色：`--wh-bg` `--wh-bg-muted` `--wh-line` `--wh-line-strong` `--wh-ink` `--wh-muted` `--wh-accent` `--wh-hover`；状态 `--wh-done` `--wh-blocked` `--wh-decision`。
圆角：`4` 微 / `12` 小控件 / `20` 控件 / `28` 卡片 / `999` 胶囊（表格本体 0）。间距：4 的倍数（0/2/4/8/12/16/20/24/32/40/48/64）。字号：11~32，字重只有 Regular / Medium / Semibold。

## 八条硬规则（违反就是返工）

1. 只用令牌，不写字面 hex / 圆角 / 间距。
2. 圆角只从五档里取，不发明新值。
3. 表面无阴影，分层靠 1px `border/default`；只有浮层（Modal / Menu / Tooltip）允许极淡投影。
4. 一个强调色：一屏只有一处主动作（`accent/default`），次要动作白底 + hairline。
5. 状态色只表达状态，不装饰。
6. 表格单行：行高 41，超宽省略号，不缩字号。
7. 控件四态齐全：default / hover / focus / disabled；表格行 default / hover / selected。
8. 指标数值 `Display/Metric`，单位小一号且 `text/muted`，金额与数量右对齐。

## 工作流程

1. **先找现成的**：23 个组件里有的，直接复用 Figma 实例 / 对应模板片段，不要重画。
2. **判断容器内外**：卡片、面板里的叫「容器内」；浮在页面上方的叫「容器外」（Modal / Menu / Tooltip）。新组件先归类再落位。
3. **改 Figma 再改代码**：视觉改动先落到 `0:1` 页，再改 `app/workhub/static/workhub-modules.css` 与模板。
4. **三档截图自检**：1440 / 1024 / 768，窄屏不许横向滚动。
5. **对照验收清单**：见 `docs/design/design-system-v1.md` §10。

## 反面模式

- 引 shadcn/ui 的 React 组件或 tailwind 类名 → 本项目没有构建链，直接废。
- 为「好看」加渐变、玻璃拟态、彩色大面积 → 与口径冲突。
- 自己发明 6px / 16px 圆角 → 五档之外不允许。
- 卡片用 20px 圆角 → 一律 28；控件用 8 → 一律 20。
- 表格靠缩字号塞进列宽 → 应该省略号或加宽列。
