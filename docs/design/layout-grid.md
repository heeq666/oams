# 内容区栅格规范 v1

> 状态：**已落地**（2026-10-28 拍板并执行；代码、Figma 模板、验收脚本三处都已就位）。
> 适用范围：应用外壳里的内容区（`.app-frame > main`）。
> 不适用：顶部导航、左侧 rail、表格内部列宽、弹层内部排版——它们不在栅格里。

## 1. 为什么需要这层规范

落地前内容区同时存在**三套栅格语言**，任何一套都能写页面，于是新页面必然不齐：

| 落地前用的 | 位置 | 怎么算列数 | 出过的事 |
|---|---|---|---|
| auto-fit 自适应 | `app/workhub/templates/base.html:37` `.grid` | `repeat(auto-fit, minmax(140px, 1fr))` | 1024 宽指标行排成 5+1 孤儿行 |
| 等分 N 列 | `app/workhub/static/workhub-modules.css:103-118` `.wh-columns-1..6` | `repeat(N, 1fr)` | 只能等分，表达不了 2 : 1 |
| 手写比例 | `app/workhub/templates/base.html:543` `.settings-cols` `1.45fr`、`:508` `88px 1fr 72px` | 任意 fr / 任意 px | 设置页两卡顶边差 16、底边参差 |

结论：**规则收敛到一套**，且必须能表达"主栏 + 侧栏"。现在页面级多列只有 `.grid-12` 一套。

## 2. 12 栅格

写在 `app/workhub/templates/base.html:541-567`：

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

所以模块层保留 `repeat(N)`，只是把 N 收进 CSS 变量（`workhub-modules.css:97-124`）：

```css
.wh-columns, .wh-grid { grid-template-columns: repeat(var(--wh-cols, 1), minmax(0, 1fr)); }
.wh-columns-1 { --wh-cols: 1; } … .wh-columns-6 { --wh-cols: 6; }
.wh-columns-5 { --wh-cols: 1; } /* 5 列弃用：显式退回单列，不靠默认值兜底 */
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
| `templates/reviews.html:17` | `.stack-20` → `.stack-16`（间距回 16 的令牌档） |
| `templates/board_body.html:52,68`、`progress.html:7` `.grid.metrics` | 不动（例外 1） |
| `.app-frame > main` 直接子元素 | 要并排就必须包在 `.grid-12` 里并标跨度 |

新页面照抄 Figma 的「页面模板」frame，不自己写 `grid-template-columns`。

## 9. 验收（已可执行）

用 ego 浏览器跑：按给定宽度逐页量「同一行子元素 top 是否相等 / 跨度宽度 / 横向溢出」，同时可选落盘截图（命令、视口写法与断言见 `AGENTS.md`「浏览器自动化」）。

```sh
# 量测脚本写到 /tmp/grid-check.mjs，内部按 1440 / 1024 / 768 循环，截图落 assets/screenshots/
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
| 2 | 模块层收进 `--wh-cols`、`wh-columns-5` 弃用 | ✅ | `static/workhub-modules.css:97-124` |
| 3 | 设置页迁到 8 + 4 + 12 | ✅ | `templates/settings.html:11-60` |
| 4 | 间距 `.stack-20` → `.stack-16` | ✅ | `templates/base.html:487`、`templates/reviews.html:17` |
| 5 | `AGENTS.md` 硬规则 | ✅ | 见「做界面之前必读」第三条 |
| 6 | Figma 三张页面模板（1440 / 1024 / 768） | ✅ | 页面 `03 · 页面稿`，frame `219:13187` / `219:13273` / `219:13359` |
| 7 | 验收方式 | ✅ | ego 浏览器三档量测（见 `AGENTS.md`「浏览器自动化」） |

实测（2026-10-28 量得，结论仍有效）：1440 → 8+4 = 827 + 406，gap 16，行内等高 346；1024 / 768 → 全部单列；三档 `overflowX` 全为 0。

Figma 三张模板放在 `03 · 页面稿` 页面下方 y≈5100 处（Starter 计划只有 3 页，不能新建页面），每张都画了 rail、内容列、12 列标尺、跨度白名单与 8+4 / 12 的样例行。1024 与 768 的样例行按实际行为画成**上下堆叠**，不是并排——模板必须画实现，不能画愿望。

## 11. 未决

- 指标行是否也纳入栅格（当前判断：不纳入，数量可变）。
- 三段式行 `.row-3cell` 是否要抽类、以及"左栏可变宽"是否支持（当前登记为例外 4）。
- 无。落地时已核对：`radius/card` 在 Figma 变量、`tokens.json:83`、`--r-card` 三处都是 **26**。
