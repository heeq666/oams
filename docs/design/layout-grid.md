# 内容区栅格规范 v1

> 状态：**已落地**（2026-10-28 拍板并执行；代码、页面模板、验收脚本三处都已就位）。
> 适用范围：应用外壳里的内容区（`.app-frame > main`）。
> 不适用：顶部导航、左侧 rail、表格内部列宽、弹层内部排版——它们不在栅格里。

## 1. 为什么需要这层规范

落地前内容区同时存在**三套栅格语言**，任何一套都能写页面，于是新页面必然不齐：

| 落地前用的 | 位置 | 怎么算列数 | 出过的事 |
|---|---|---|---|
| auto-fit 自适应 | `app/oams/templates/base.html:37` `.grid` | `repeat(auto-fit, minmax(140px, 1fr))` | 1024 宽指标行排成 5+1 孤儿行 |
| 等分 N 列 | `app/oams/static/oams-modules.css:103-118` `.wh-columns-1..6` | `repeat(N, 1fr)` | 只能等分，表达不了 2 : 1 |
| 手写比例 | `app/oams/templates/base.html:543` `.settings-cols` `1.45fr`、`:508` `88px 1fr 72px` | 任意 fr / 任意 px | 设置页两卡顶边差 16、底边参差 |

结论：**规则收敛到一套**，且必须能表达"主栏 + 侧栏"。现在页面级多列只有 `.grid-12` 一套。

## 2. 12 栅格

写在 `app/oams/templates/base.html:541-567`：

```css
.grid-12 { display: grid; grid-template-columns: repeat(12, minmax(0, 1fr)); gap: 16px; }
.span-12 { grid-column: span 12; }
.span-8  { grid-column: span 8; }
.span-6  { grid-column: span 6; }
.span-4  { grid-column: span 4; }
.span-3  { grid-column: span 3; }
```

- 容器宽度**按容器算，不按视口算**：`.app-frame > main` 是 `padding: 72px 32px 40px`（`base.html:638`），≤900 收到 `64px 16px 40px`（`:672`）。有 rail 时内容宽 = 视口 − 127 − 64（窄屏 − 127 − 32）。
- gap **固定 16**（`space/5`），不随断点变化。
- 跨度白名单 **只有 12 / 8 / 6 / 4 / 3**；禁止其它数值，禁止任意 `fr` 比例。
  12 能被 1 / 2 / 3 / 4 / 6 整除，所以等分全部落在栅格上。

实测宽度（内容宽 = 1440 时 1249、1024 时 833、768 时 609）：

| 跨度 | 1440 | 1024 | 768 | 用途 |
|---|---|---|---|---|
| 12 | 1249 | 833 | 609 | 通栏：页面头、单表、时间线 |
| 8 | 827 | 550 | 401 | 主栏（配 4 用 = 2 : 1） |
| 6 | 617 | 409 | 297 | 两等分 |
| 4 | 406 | 267 | 192 | 三等分 / 侧栏 |
| 3 | 300 | 196 | 140 | 指标行内部（四等分） |

## 3. 三类排版对象，两种表面

| 层级 | 名字 | 长什么样 | 对齐行为 |
|---|---|---|---|
| L1 | 分区 Section | 标题 + 说明，**无底色** | `align-self: start`，不参与撑高 |
| L2 | 面板 Panel | 白面 + `radius/card`(26，= `--r-card`) + 内距 16 | 同一行 **stretch 等高** |
| L3 | 区块 Block | 面板内部分组，浅底 + `radius/control`(18) | 不进外层栅格，只在 Panel 内部排 |

只有这两种表面，**不允许"半卡片"**（浅底 + 圆角 + 阴影的那种）。

代码里这条绑定是两条选择器，不是靠各页面自觉：

```css
.grid-12 > .card { align-self: stretch; }
.grid-12 > :not(.card) { align-self: start; }
```

2026-10-28 设置页那次错，就是把 `align-items: start` 一把套给了两种表面。

元件级（Button / Input / Badge / 表格行内控件）**不参与栅格**，宽度由内容或控件档位决定。

## 4. 断点折叠

| 视口 | 行为 |
|---|---|
| ≥ 1090 | 严格按跨度 |
| 900 – 1089 | `8` / `4` → `12`（8+4 拆成上下）；`6` 保持 6，所以 `6 + 6` 仍并排（409 + 409）；`3` → `6` |
| < 900 | 全部 12，单列 |

只给两档折叠。每多一档，就多一类"这个页面在这个宽度没测过"的缺陷。

`4` 在 1024 以上退成 12 而不是 6：三张 4 栏并排若退成 6，会留下 2+1 的孤儿行；4 栏在 1024 只剩 267px，也放不下带职责串的卡片（设置页岗位卡实测就折不动）。

任何跨度折叠后都不许窄于 3 栅格（140px）；窄于它就说明该堆叠了。

## 5. 高度：三个数 + 一条规则，不做档位表

固定数（全站已一致，写死为规范）：

- 面板内距 **16**
- 卡片头高 **40**
- 表格行高 **41**

规则：

1. 同一个栅格行里的 Panel 一律等高（stretch，已由 §3 的两条选择器保证）。
2. Section 不参与撑高（`align-self: start`）。
3. 只给 `min-height` 三档：**120**（空态 / 骨架屏占位）、**200**（需要固定视觉重量的小面板），其余不给。**不给 max-height。**

不给"高度档位"的原因是两个真实尺寸：设置页两张卡 208 高，审核页那张卡 951 高。任何固定档位都套不住，结果不是溢出就是大片空白。高度由内容行数决定：表格行 41 × 行数 + 头部 + 内距。

## 6. 模块层：没改成 12 栅格，收进变量（理由）

原草案打算把 `.wh-columns-N` 内部改写成 12 栅格 + 跨度。落地时放弃，原因是**真实耦合**：

模块的 `span` 参数（`templates/modules/metric_card.html:4` 的 `wh-span-{{1..4}}`，父级是 `metric_grid.html` 的 `wh-columns-{{columns}}`）语义是"跨父级列数的几列"。父级列数 N 由 descriptor 决定，若父级变成 12 栏，`span-1` 就从"占满 1/N"变成"占 1/12"，同一份 descriptor 在 `columns: 3` 和 `columns: 4` 下会得到完全不同的视觉。要修就得写 N×k 组上下文选择器（`3 列时的 span-1 = 4`、`2 列时的 span-1 = 6`……），16 条以上的规则去维护一个本可以没有的问题。

所以模块层保留 `repeat(N)`，只是把 N 收进 CSS 变量（`oams-modules.css:97-124`）：

```css
.wh-columns, .wh-grid { grid-template-columns: repeat(var(--oams-cols, 1), minmax(0, 1fr)); }
.wh-columns-1 { --oams-cols: 1; } … .wh-columns-6 { --oams-cols: 6; }
.wh-columns-5 { --oams-cols: 1; } /* 5 列弃用：显式退回单列，不靠默认值兜底 */
```

对外语义**零变化**：descriptor 的 `columns: 1..6` 照旧。唯一变化是 `columns: 5` 不再有五列布局（全库 0 处使用，只有定义），显式退回单列；真要写 5 会看得见，不会"看起来生效了"。

模块容器宽度由 descriptor 决定，栅格按百分比算，因此不受 `main` 内距变化影响。

## 7. 例外清单（新增必须登记，否则不许写）

1. **指标行**：`base.html:37` `.grid` 的 `auto-fit minmax(140px, 1fr)` —— 指标数量可变（6 / 5 / 4 个），栅格表达不了，保留自适应 + 断点（`:418-419`：≤1100 三列、≤620 两列）。仅限指标行。
2. **外壳**：rail 固定 127 + `main` 弹性，不是栅格。
3. **表格内部列宽**：由内容决定，不用栅格；单行省略号另议。
4. **三段式行**：`base.html:508` `88px 1fr 72px`、`:589` `96px 1fr auto`（标签 + 内容 + 操作）——当前**登记为例外，未抽类**；要抽就抽成语义类 `.row-3cell`（左固定标签、中间弹性、右固定操作），不是可调比例。
5. **模块层容器**：见 §6，保留 `repeat(N)`，不属于页面栅格。

## 8. 现有页面怎么用

| 位置 | 落地后 |
|---|---|
| `templates/settings.html:11-60` | 部门 `span-8` / 岗位 `span-4` / 最近改动 `span-12`，整块包在 `.grid-12` 里；≤1089 自动变 12+12+12 |
| `templates/progress.html` | 公司概览 `span-12`；每张部门卡一整行 `span-12`，卡内再分 8（事情）/ 4（数字）；空态 `span-12` |
| `templates/board_body.html` | 公司指标 `span-12`；部门六指标 `span-8` + 渠道计分卡 `span-4` 同排；图层切换、流程图、事项清单各 `span-12` |
| `templates/reviews.html` | 每张待审核卡 `span-12`，卡内分 8（这一版改了什么）/ 4（通过 · 退回）；最近决定 `span-12`；空态 `span-12` |
| `templates/board_body.html`、`progress.html` 的 `.grid.metrics` | 指标条本身仍是 auto-fit（例外 1），只是被放进一格 `span-12` / `span-8` |
| `.app-frame > main` 直接子元素 | 要并排就必须包在 `.grid-12` 里并标跨度 |

### 8.1 三页的跨度分配与「有底等高」的落法（2026-10-31）

| 页面 | 行 | 左 · 右 | 哪两块必须等高 |
|---|---|---|---|
| `/progress` | 公司概览 | `span-12`（内含 5 个指标） | — |
| | 部门卡 ×N | `span-12` → 卡内 8 / 4 | 卡内左右都无底，不撑高（右栏是浅底小面板，跟着自己内容走） |
| `/board` | 公司指标 | `span-12`（指标 / 不可用卡同格） | — |
| | 部门数字 + 渠道 | 六指标 `span-8` · 计分卡 `span-4` | **是**：两块都有底，行高 172 齐平 |
| | 图层切换 / 流程图 / 清单 | 各 `span-12` | — |
| `/reviews` | 待审核卡 ×N | `span-12` → 卡内 8 / 4 | **是**：diff 与决定区都有底，两块拉满行高（实测 709 / 709、236 / 236） |

两条落法（都在 `base.html:556-580`）：

- 有底的两块里，**无底的那一块仍是 `align-self: start`**（`.grid-12 > :not(.card)` 的默认），只有 `metrics` / `channel-scorecard` / `diff-block` / `decide` 四类显式拉满行高；给「有底」加了几条：`.grid-12 > .grid.metrics { align-self: stretch; grid-auto-rows: 1fr }`（6 个指标跟着同行最高的那块走）。
- 6 个指标挤进 8 列时 auto-fit 会排出「5 + 1」的孤儿行，宽屏（≥1090）固定 3 列两行：`.grid-12 > .grid.metrics.span-8 { grid-template-columns: repeat(3, minmax(0, 1fr)) }`。

公司指标与部门指标**不并排**：一个是商品漏斗（已下单 29 款），一个是这项工作项（未完成 12），计数对象不同，同一行会被读成同一把尺子。


新页面照抄 §10 那三张页面模板（1440 / 1024 / 768），不自己写 `grid-template-columns`。

## 9. 验收（已可执行）

用 ego 浏览器跑：按给定宽度逐页量「同一行子元素 top 是否相等 / 跨度宽度 / 横向溢出」，同时可选落盘截图（命令、视口写法与断言见 `AGENTS.md`「浏览器自动化」）。

```sh
# 量测脚本写到 /tmp/grid-check.mjs，内部按 1440 / 1024 / 768 循环，截图落 /tmp（自检图不进库）
ego-browser nodejs < /tmp/grid-check.mjs
```

页面内联检查（贴进 DevTools 控制台也行）：

```js
[...document.querySelectorAll('.grid-12')].forEach(g => {
  const tops = [...g.children].map(c => Math.round(c.getBoundingClientRect().top));
  if (new Set(tops).size > 1) console.warn('顶边不齐', g);
});
document.documentElement.scrollWidth > document.documentElement.clientWidth && console.warn('横向溢出');
```

## 10. 落地情况（2026-10-28 全部完成）

| # | 项 | 状态 | 位置 |
|---|---|---|---|
| 1 | 外壳栅格 + 两档折叠 + 两种表面对齐 | ✅ | `templates/base.html:541-567` |
| 2 | 模块层收进 `--oams-cols`、`wh-columns-5` 弃用 | ✅ | `static/oams-modules.css:97-124` |
| 3 | 设置页迁到 8 + 4 + 12 | ✅ | `templates/settings.html:11-60` |
| 4 | 间距 `.stack-20` → `.stack-16` | ✅ | `templates/base.html:487`、`templates/reviews.html:17` |
| 5 | `AGENTS.md` 硬规则 | ✅ | 见「做界面之前必读」第三条 |
| 6 | 三张页面模板（1440 / 1024 / 768） | ✅ | 本地画布（已搬出仓库）；规格见本节末段与 §8 |
| 7 | 验收方式 | ✅ | ego 浏览器三档量测（见 `AGENTS.md`「浏览器自动化」） |
| 8 | `/progress` `/board` `/reviews` 铺上栅格（此前是单列，栅格检查对它们是空检） | ✅ | `templates/progress.html`、`templates/board_body.html`、`templates/reviews.html`、`templates/base.html:556-580` |

实测（2026-10-28 量得，结论仍有效）：1440 → 8+4 = 827 + 406，gap 16，行内等高 346；1024 / 768 → 全部单列；三档 `overflowX` 全为 0。

2026-10-31 补测三页（`.venv` 脚本 `scripts/ego-grid-check.mjs`，三档 × 5 页共 15 页次，`offenders: []`）：1440 下 `/board` 部门数字 827 + 计分卡 406（两块 172 齐平）、`/reviews` 卡内 806 + 395（709 齐平）、`/progress` 部门卡内 806 + 395；1024 与 768 下三页全部收成单列（`span-8/4` 在 ≤1089 变 12）。

三张页面模板（1440 / 1024 / 768）各画了 rail、内容列、12 列标尺、跨度白名单与 8+4 / 12 的样例行（画布件随内部画布一起搬出仓库，不在本仓）。1024 与 768 的样例行按实际行为画成**上下堆叠**，不是并排——**模板必须画实现，不能画愿望**。

## 11. 未决

- 指标行是否也纳入栅格（当前判断：不纳入，数量可变）。
- 三段式行 `.row-3cell` 是否要抽类、以及"左栏可变宽"是否支持（当前登记为例外 4）。
- 无。落地时已核对：`radius/card` 在令牌表、`tokens.json`、`--r-card` 三处都是 **26**。
