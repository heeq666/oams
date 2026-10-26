# Workhub 设计规范 v1（界面口径 / 供人 + AI 共用）

> 这份文档是**唯一口径**。Figma 文件是**唯一真源**，本文与 `docs/design/tokens.json` 是它的镜像，代码 `app/workhub/static/workhub-modules.css` 是它的下游实现。三者冲突时：以 Figma 变量为准，然后回来改本文和执照代码。

## 0. 一句话

shadcn 的语义色 + **白底 / hairline 分界 / 五档圆角 / 无阴影 / 一个强调色**。不做玻璃拟态、不做渐变按钮、不做彩色大面积。

## 1. Figma 落点（改稿先看这里）

| 页面 | node id | 内容 |
| --- | --- | --- |
| `01 · 设计令牌 Tokens` | `3:89` | 变量与文本样式 |
| `02 · 组件库 Components` | `0:1` | 23 个组件组 + 5 个整页稿 |
| `04 · 窄屏 · 状态 · 覆盖` | `3:88` | 窄屏与状态覆盖 |

组件库分两块（都在本页）：

- `128:3402 组件库 · Components Library` —— 外壳层：TopBar、Rail、PageHead、Segmented、Tabs、FlowStrip、Metrics、Metric card、ItemCard、Button、Badge。
- `140:3438 组件库 · 容器内 / 容器外` —— 表单 8 / 反馈 6 / 数据 6 + 容器外 3（Tooltip / Menu / Modal）。

整页稿（1440 宽）：汇报中心 `3:1086`、进度中心 `3:1087`、登录 `111:818`、页面审核 `111:844`、设置 `111:1148`。

## 2. 颜色令牌（47 个，只用名字，不写 hex）

| 用途 | 令牌 | 值 |
| --- | --- | --- |
| 页面底 | `bg/app` | #FAFAFA |
| 卡/面板底 | `bg/surface` | #FFFFFF |
| 次级底、表格悬停 | `bg/subtle` | #F5F5F5 |
| 左侧导航底 | `bg/rail` | #FFFFFF |
| 控件内底（输入框、搜索框） | `fill/chrome` | #FFFFFF |
| 幽灵按钮悬停底 | `fill/hover` | #E5E5E5 |
| 正文 / 次正文 / 辅助 / 禁用 | `text/primary` `text/secondary` `text/muted` `text/aside` | #0A0A0A / #525252 / #737373 / #A3A3A3 |
| 深色底上的字 | `text/inverse` | #FAFAFA |
| 分界线 | `border/default` | #E5E5E5 |
| 强分界（选中态边） | `border/strong` | #D4D4D4 |
| 焦点环 | `focus/ring` | #A1A1A1 |
| 主动作 | `accent/default` / `accent/hover` / `accent/on` | #171717 / #404040 / #FAFAFA |

状态色一律三件套 `status/{语义}` + `-subtle`（底）+ `-line`（边）：success #16A34A / warning #D97706 / danger #DC2626 / info #2563EB / neutral #525252。

`heat/*` 是看板热度格；`data/agent` #7C3AED 只用来区分「AI 产出」与「人产出」，别拿它当品牌色。

## 3. 圆角：五档，不许发明第六档

| 档位 | 令牌 | CSS 变量 | 值 | 用在哪 |
| --- | --- | --- | --- | --- |
| 微 | `radius/xs` | `--r-micro` / `--wh-radius-micro` | 4 | 色标、极小的内嵌块、焦点环 |
| 小控件 | `radius/lg` | `--r-small` / `--wh-radius-sm` | 12 | 分段控件的段、筛选 chip、菜单项、骨架条、表格内文本按钮、分页页码、流程条容器 |
| 控件 | `radius/control` | `--r-control` / `--wh-radius` | 20 | 输入框、下拉框、搜索框、按钮（40 高时 20 正好半高 = 胶囊）、分段控件外框、流程条阶段块 |
| 卡片 | `radius/card` | `--r-card` / `--wh-radius-card` | 28 | 卡片、面板、弹窗、空状态、骨架卡、菜单浮层、提示浮层 |
| 胶囊 | `radius/pill` | `--r-pill` / `--wh-radius-pill` | 999 | 徽章、主按钮、开关轨与圆钮、进度条、指标卡内的小标签 |

写进代码前先记住三条例外：

- **表格本体圆角 0**：行与行靠 hairline 分隔，与 Figma `140:3902` / `140:4194` 一致。
- 按钮在 36/40 高时用 `--r-pill`（=半高，视觉即胶囊）；其它高度的控件走 `--r-control`。
- `radius/md`(8) 与 `radius/xl`(14) 是历史变量，**新代码不用**。

## 4. 间距（`space/0..11`）

0 / 2 / 4 / 8 / 12 / 16 / 20 / 24 / 32 / 40 / 48 / 64。

常用：卡片 padding 20、卡内元素间距 10~12、卡片之间 16、分区之间 32~48、页面左右 24。别写 5、7、11、13 这种数。

## 5. 字号（12 个文本样式，字号只有 11/12/13/14/15/16/18/20/24/28/32）

| 样式 | 字号/字重 | 用途 |
| --- | --- | --- |
| `Display/Metric-LG` | 32 Semibold | 大号数值（首屏 KPI） |
| `Display/Metric` | 28 Semibold | 指标数值 |
| `Heading/H1` | 20 Semibold | 页面标题 |
| `Heading/H2` | 16 Semibold | 分区标题 |
| `Heading/H3` | 14 Semibold | 卡片标题 |
| `Body/Strong` | 14 Semibold | 正文强调（表头、人名） |
| `Body/Default` | 14 Regular | 正文 |
| `Body/Small` | 13 Regular | 辅助正文 |
| `Nav/Item` | 14 Medium | 导航项 |
| `Label/Caption` | 12 Medium | 标签、说明文字 |
| `Label/Metric` | 12 Regular | 指标名 |
| `Mono/Code` | 13 Regular | 单号、时间戳 |

字重只有 Regular / Medium / Semibold；中文用 PingFang SC，数字与单号可走 Mono。

## 6. 组件清单（23 个带变体的组件组 + 5 个独立组件，全部在 `0:1` 页）

外壳层（`128:3402`）：`3:327` PageHead、`3:328` Segmented、`3:329` Tabs、`3:330` FlowStrip、`58:689` Metrics、`3:169` Metric card、`3:331` ItemCard、`3:132` Button（primary / primary-hover / secondary / danger / link）、`3:159` Badge（8 变体）、`113:1912` TopBar、`119:2165` Rail。

表单（`140:3571`）：`140:3585` Input 5v、`140:3594` Textarea 3v、`140:3612` Select 3v、`140:3625` Search 2v、`140:3643` Checkbox 4v、`140:3656` Radio 3v、`140:3671` Switch 3v、`140:3688` Field 2v。

反馈（`140:3691`）：`140:3754` Alert 4v、`140:3783` Progress 3v、`140:3797` Skeleton 2v、`140:3804` Empty 1v。

数据（`140:3842`）：`140:4378` Link 操作 2v、`140:3902` Table 行 4v、`140:4194` Table 弹性列 4v、`140:3918` Pagination、`140:3973` Avatar 3v、`140:3940` Breadcrumb。

容器外（`140:4060`）：`140:3759` Tooltip、`140:3953` Menu（面板 200 宽、圆角 28）、`140:3839` Modal 2v（面板圆角 28）。

> 容器内 = 卡片/面板里的东西；容器外 = 浮在整个页面之上的东西。新组件先判断属于哪一类，再进对应分区。

## 7. 八条硬规则

1. **只用令牌**：颜色、圆角、间距、字号一律引用变量/`--wh-*`，不写字面值。
2. **不发明圆角**：五档里挑一个 —— 微 4、小控件 12、控件 20、卡片 28、胶囊 999；表格本体 0。挑不到就说明该加组件，别加圆角。
3. **表面无阴影**：分层靠底色差与 1px `border/default`。只有浮层（Modal / Menu / Tooltip）允许一层极淡投影。没有描边的面板之间必须留 `space/5`（16px）——两块白面板贴在一起会粘成一整块，28 的圆角还会互相啃出缺口。
4. **一个强调色**：`accent/default` 一屏只出现一次主动作；次要动作走 `secondary`（白底 + hairline）。
5. **状态色只表达状态**：不许拿 `status/danger` 做装饰，也不许拿 `data/agent` 做品牌色。
6. **表格单行**：行高 41、单元格不折行；内容超宽用省略号（`text-overflow: ellipsis`），不靠缩字号。
7. **控件四态**：每个可交互组件都要有 default / hover / focus / disabled；表格行要 default / hover / selected。
8. **数字优先**：指标数值用 `Display/Metric`，单位小一号且 `text/muted`；金额与数量右对齐。

## 8. 与代码的一致性（2026-10-26 已统一）

代码已对齐 Figma。外壳 `base.html` 与模块层 `workhub-modules.css` 各有一套令牌、值相同，下表是「这个变量叫什么」的对照表。改视觉的顺序：先改 Figma 变量，再改这两处。

### 8.1 令牌 ↔ CSS 变量

| Figma 变量 | 外壳 `base.html` | 模块层 `workhub-modules.css` | 值 |
| --- | --- | --- | --- |
| `bg/app` | `--bg` | `--wh-bg-muted` | #FAFAFA |
| `bg/surface` | `--card` | `--wh-bg` | #FFFFFF |
| `bg/subtle` | `--subtle` | `--wh-fill` | #F5F5F5 |
| `fill/hover` | `--hover` | `--wh-hover` | #E5E5E5 |
| `text/primary` | `--ink` | `--wh-ink` | #0A0A0A |
| `text/secondary` | `--ink-2` | — | #525252 |
| `text/muted` | `--muted` | `--wh-muted` | #737373 |
| `border/default` | `--line` | `--wh-line` | #E5E5E5 |
| `border/strong` | — | `--wh-line-strong` | #D4D4D4 |
| `focus/ring` | `--ring` | — | #A1A1A1 |
| `accent/default` | `--accent` | `--wh-accent` | #171717 |
| `accent/hover` | `--accent-hover` | — | #404040 |
| `accent/on` | `--on-accent` | — | #FAFAFA |
| `status/success` | `--done` | `--wh-done` | #16A34A |
| `status/warning` | `--decision` | `--wh-decision` | #D97706 |
| `status/danger` | `--blocked` | `--wh-blocked` | #DC2626 |
| `status/info` | `--info` | `--wh-info` | #2563EB |
| `status/*-subtle` | `--success-soft` `--warning-soft` `--danger-soft` `--info-soft` | `--wh-*-soft` | 见 §2 |
| `status/*-line` | `--success-line` `--warning-line` `--danger-line` `--info-line` | `--wh-*-line` | 见 §2 |
| `data/agent` | `--agent` | — | #7C3AED |
| `radius/card` | `--r-card` | `--wh-radius-card` | 28 |
| `radius/control` | `--r-control` | `--wh-radius` | 20 |
| `radius/lg` | `--r-small` | `--wh-radius-sm` | 12 |
| `radius/xs` | `--r-micro` | `--wh-radius-micro` | 4 |
| `radius/pill` | `--r-pill` | `--wh-radius-pill` | 999 |

### 8.2 这次改了什么

- `workhub-modules.css`：令牌换成 Figma 的中性灰（`--wh-line` #E5E5E5、`--wh-line-strong` #D4D4D4、`--wh-ink` #0A0A0A、`--wh-muted` #737373、`--wh-accent` #171717、`--wh-blocked` #DC2626、`--wh-decision` #D97706、`--wh-done` #16A34A）；新增 `--wh-hover`、`--wh-fill`、`--wh-info`、`--wh-radius-card`、`--wh-radius-pill` 与整套 status soft/line；正文里的 `#f4f4f5`、`#2563eb`、`#1d4ed8`、`999px` 全部改成变量。
- `base.html`：删掉被 shadcn 皮肤块完全覆盖的旧 `:root`（旧暗色系 9 个变量），全文件只留一套令牌；新增 `--ink-2`、`--on-accent`、`--accent-hover`、`--hover`、`--blocked-strong`、`--warning-ink`、`--info-soft` 与 status line 四色；所有字面 hex / 字面圆角改成 `var(--*)`。
- Figma 侧同步：Segmented 容器 8→20、段 6→12；Tabs 6→12；FlowStrip 容器 8→12、阶段块 6→20；RailItem 6→12。Figma 里不再有 6/8 两个档外值。
- 截图自检发现并修掉一处**竖向节奏缺失**：看板页「指标暂不可用」卡片 ↔ 指标行、指标行 ↔ 渠道条之间是 0 间距，白卡片粘成一整块（换成 28 圆角后格外明显）。补 `.card + .grid`、`.grid + .grid`、`.grid + .channel-scorecard`、`.card + .channel-scorecard`、`.grid + .card` 的 `margin-top: 16px`，实测 16px。

### 8.3 已知例外（别照抄，也别改错）

- **楼层平面图 / 工位图**（`base.html` 的 `.zone` `.desk` `.floor-bg` `.editor .item`）用一组示意图专用色：#eef2ef/#cfe3d6、#fdf6e8/#e8cf94、#fdf1ef/#eeb2a8、#f7f8f9/#b8bec4、#f6f7f8/#d9dde1、#d3d9de、#fbfcfd、#98a0a8。它们是 SVG 图元填充，不进令牌；要改整组一起改。
- `--r-sm`（=微 4）与 `--r-lg`（=小控件 12）是历史别名，新代码直接用 `--r-micro` / `--r-small`。
- `radius/md`(8)、`radius/xl`(14) 留在 Figma 变量里但不再使用。

## 9. 怎么在别的对话里用

三种载体，按复用范围选：

1. **本仓库的会话**：根目录 `AGENTS.md` 已写好入口，任何新会话会自动带上硬规则和本文路径 —— 什么都不用说。
2. **别的仓库 / 别人的机器**：把 `skills/workhub-design-system/SKILL.md` 装进 `~/.dsh/skills/workhub-design-system/`，之后说「按 workhub 设计规范做这个页面」即可。
3. **只想让 AI 读一次**：直接说「读 `docs/design/design-system-v1.md` 和 `docs/design/tokens.json`，按里面的令牌和八条硬规则实现」。

Figma 侧同理：新会话里说「按 `0:1` 页的组件库补一个 xxx 组件」，它会自己读令牌、复用既有组件、写回本页。

## 10. 交付前验收

- [ ] 截图对照 `02 · 组件库 Components` 页，颜色/圆角/字号没有第六种值
- [ ] 悬停、焦点、禁用、空、加载（骨架屏）五种状态都点了看过
- [ ] 表格长文案不折行（超宽省略）
- [ ] 深浅色只用一个方向（当前只有浅色），没有半透明玻璃层
- [ ] 1440 / 1024 / 768 三档都截过图，窄屏不出现横向滚动条
