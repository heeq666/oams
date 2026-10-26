# Workhub 设计规范 v1（界面口径 / 供人 + AI 共用）

> 这份文档是**唯一口径**。Figma 文件是**唯一真源**，本文与 `docs/design/tokens.json` 是它的镜像，代码 `app/workhub/static/workhub-modules.css` 是它的下游实现。三者冲突时：以 Figma 变量为准，然后回来改本文和执照代码。

## 0. 一句话

shadcn 的语义色 + **白底 / hairline 分界 / 五档圆角 / 一个强调色**。页面内容（卡片、表格、面板）实色、无阴影；**只有浮层用玻璃态**（顶栏胶囊、菜单、弹窗、Tooltip）——同一份加白 + 模糊 + 一层淡投影，见 §7.3。不做渐变按钮、不做彩色大面积。

## 1. Figma 落点（改稿先看这里）

文件只有 3 个页面（Figma Starter 计划上限，别再 `createPage`）：

| 页面 | node id | 内容 |
| --- | --- | --- |
| `01 · 设计令牌` | `3:89` | 文件级变量与文本样式（83 项） |
| `02 · 组件库` | `0:1` | 唯一真源：28 个组件组 + 14 个模块组件，按三层结构分段 |
| `03 · 页面稿` | `3:88` | 11 张整页稿（1440 / 1024 / 768）+ 滚动时序 + `148:9383` 状态矩阵 |

组件库页只有两个 frame：

- `140:3438 组件库 · 组件与模块` —— **主库**，自上而下六段：
  1. 标题 / 说明（三层结构总述）
  2. `148:5905 骨架层 Shell` —— TopBar `148:5927`（变体=通栏 / 滚动后）、Rail `119:2165`、RailItem `148:9283`（变体=默认 / 选中）、PageHead `3:327`、Segmented `3:328`、Tabs `3:329`、FlowStrip `3:330`、账号菜单 `140:3563`、ItemCard `3:331`、Metrics `58:689`
  3. `140:4064 组件层 · 控件 / 反馈 / 数据` —— `148:5928 基础件`（Button `148:5936` / Badge `3:159`）、`140:3571 表单控件`、`140:3691 反馈与状态`、`140:3842 数据展示`、两张 `用法` 实例拼装稿
  4. `140:4060 浮层 · 气泡 / 菜单 / 弹窗` —— Tooltip `140:3759`、Menu `140:3953`、Modal `140:3839`
  5. `146:4401 协作室 · 底部两栏弹层` —— 入口按钮 `146:4418`、消息条 `146:4445`、会话行 `148:6356`、两栏面板 `148:7054`
  6. `148:5948 模块层 · 与 MODULE_REGISTRY 对齐` —— 14 个模块组件（容器 4 / 录入 6 / 展示 4），另 3 个复用「数据展示」里的 Metric card / Metrics / FlowStrip，见 §6
- `128:3402 归档 · 旧骨架说明` —— 早期外壳画布的说明文字，**不要引用、不要复制**（组件已全部迁入主库）。

整页稿都在 `03 · 页面稿`（`3:88`）：汇报中心 `3:1086`（收起态，含未读按钮）、协作室展开 `148:6554`（1440）、协作室到顶 `148:7055`（1440）、协作室私聊 `148:8843`（1440）、协作室 `148:8086`（1024）、协作室 `148:7737`（768）、进度中心 `3:1087`、登录 `111:818`、页面审核 `111:844`、设置 `111:1148`、滚动时序 `128:3298`。

状态矩阵在 `3:88` 右侧（x=4560）：`148:9383 状态矩阵 · 列表与卡片（空 / 加载 / 错误 / 长内容）` —— 四行，每行左侧 220 宽标签列（14 Semibold 名称 + 12 `text/muted` 说明），右侧 `bg/surface` / 圆角 28 / 内距 28 的白面板，面板里全部是组件实例：Empty `140:3804`（拉满 + `counterAxisAlignItems: CENTER`）、Skeleton `140:3797` 文字行 ×2 + 卡片（拉满，内部条 `layoutSizingHorizontal: FILL`）、Alert `140:3754` 变体=危险（拉满）+ Button `148:5936` 变体=次要「重新加载」、Table 弹性列 `140:4194` 表头 + 3 数据行（行 30 高、第二行首格 `textTruncation: ENDING` 演示省略）。**新增页面状态先看这块，别每次重画。**

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

`heat/*` 是看板热度格；`data/agent` #7C3AED 只用来区分「AI 产出」与「人产出」，别拿它当品牌色。配套的 `data/agent-subtle` #F5F3FF 与 `data/agent-line` #DDD6FE 是 `model` 徽章的底与边（浅紫底紫字），别单独拿去刷卡片。

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

## 6. 组件清单（三层结构：骨架层 / 组件层 / 模块层）

组件库按**资产层级**分段，不按建设阶段（不存在 v2 页）。命名与变体属性/值一律中文；变体属性名统一写作 `变体`。

骨架层（`148:5905`，页面外壳，一页一套）：TopBar `148:5927`（变体=通栏 / 滚动后）、Rail `119:2165`、RailItem `148:9283`（变体=默认 / 选中，68×32、圆角 12、Mark 3×16，代码类 `.rail-item` / `.rail-item.on`）、PageHead `3:327`、Segmented `3:328`、Tabs `3:329`、FlowStrip `3:330`、账号菜单 `140:3563`、ItemCard `3:331`、Metrics 指标区 `148:9583`（变体=两行三等分 / 一行六等分 / 一行五等分，行高 80、卡宽 FILL；1440 看板取六等分、1440 进度中心取五等分、≤1100 取两行三等分，与 `.grid.metrics` 的 3 列断点一致）。

基础件（`148:5928`）：Button `148:5936`（变体=主要 / 主要·悬停 / 次要 / 危险 / 链接，高 40 胶囊）、Badge `3:159`（9 变体=中性 / 成功 / 警告 / 危险 / 信息 / 智能体 / 人工 / 描边 / 模型）、Metric card `3:169`（变体=常态 / 警告 / 告警）。

表单控件（`140:3571`）：`140:3585` Input（默认 / 已填 / 聚焦 / 错误 / 禁用）、`140:3594` Textarea（默认 / 聚焦 / 禁用）、`140:3612` Select（默认 / 聚焦 / 禁用）、`140:3625` Search（默认 / 已填）、`140:3643` Checkbox（未选 / 已选 / 半选 / 禁用）、`140:3656` Radio（未选 / 选中 / 禁用）、`140:3671` Switch（关 / 开 / 禁用）、`140:3688` Field（默认 / 错误）。

- **Select 的展开层归我们管**（`static/wh-select.js`，约 150 行）：原生 `<select>` 仍是表单主人——值、`name`、`required`、`disabled`、无脚本兜底全归它；脚本只在 `mousedown` 上拦掉系统弹出层，自己画一个 `.wh-list` 面板（面板档浮层：白 92% + blur 16 + `radius/card` 28，行高 32、行圆角 `radius/small` 12、选中行 500 字重 + ✓，`z-index` 45）。20 处 `<select>` 不用改一个标记就统一了；脚本挂了页面退化成系统下拉，不会坏——这是它敢用事件委托而不是初始化扫描的原因（htmx 会整块换表、界面里的表格可以由脚本重建，逐个绑定必漏）。

反馈与状态（`140:3691`）：`140:3754` Alert（成功 / 警告 / 危险 / 信息）、`140:3783` Progress（常态 / 成功 / 危险）、`140:3797` Skeleton（文字行 / 卡片）、`140:3804` Empty（默认）。

数据展示（`140:3842`）：`140:4378` Link 操作（默认 / 悬停）、`140:3902` Table 行（表头 / 数据行 / 悬停行 / 选中行）、`140:4194` Table 弹性列（同上四态）、`140:3918` Pagination、`140:3973` Avatar（中 / 大 / 状态点）、`140:3940` Breadcrumb。

浮层（`140:4060`）：`140:3759` Tooltip、`140:3953` Menu（面板 200 宽、圆角 28）、`140:3839` Modal（默认 / 危险，面板圆角 28）。

协作室（`146:4401`）：`146:4418` 协作室 · 入口按钮（状态=默认 / 悬停 / 未读）、`146:4445` 协作消息条（发言=我的 AI / 本人）、`148:6356` 协作室 · 会话行（类型=群 / 私聊 × 状态=默认 / 悬停 / 选中，6 变体）、`148:7054` 协作室 · 两栏面板（宽度=宽屏 1313 · 紧凑 960 · 单栏 704 × 高度=默认 560 · 到顶 891 × 类型=群 · 私聊）、`148:9391` 协作室 · 加载更早（状态=加载中 / 已到最早）。

模块层（`148:5948`，与 `app/workhub/modules.py` 的 20 个 key 一一对应；14 个新组件 + 3 个复用 = 20/20 覆盖）：容器 7（columns / rows / grid 是排版结构，靠规范不建组件；card `148:5953`、tabs `148:5967`、divider `148:5980`、spacer `148:5985`）、录入 6（progress_row `148:6238`、table `148:6183`、timeline `148:6223`、blocker_list `148:6079`、metric_input `148:5989`、evidence_gallery `148:6088`）、展示 7（metric_card `3:169`、metric_grid `58:689`、flow_strip `3:330` 三者复用「数据展示」下的既有件；status_chart `148:6249`、rank_list `148:6279`、activity_feed `148:6301`、note_block `148:6322`）。

模块层的规格**逐条照 `static/workhub-modules.css` 落**，不是凭设计感画的：模块块面圆角 20（`radius/control`——它套在 28 的卡里，同层嵌套不重复用 28）、内距 16、子模块间距 8～10，字号 / 行高 / 列宽都对着 CSS 抄；每个组件的 description 里写了对应的 `wh-*` 类名，改代码时对着看。

- **表格只有一种密度（2026-10-26 统一）：行高 30 + 单元格内距 6/8 + 字号 13（表头 12）。** 外壳表（`base.html:505` 的 `th/td`）与模块表（`.wh-table`）现在同值：`padding:6px 8px; line-height:18px`，加 1px 分隔线实测 31；表头底色 `bg/subtle`。Figma 组件层 Table 行 `140:3902`（固定列）与 `140:4194`（弹性列）的 8 个变体都已改成 30，引用它们的用法卡（`140:4184` / `140:4253`）自动回落。
- 单行规则只作用于**页面数据表**：`white-space:nowrap + overflow:hidden + text-overflow:ellipsis + max-width:320px`，超宽省略不折行；模块内的明细表在窄栏里允许折行（行高随之变大），别为了不折行去缩字号。
- `.wh-input` 同名冲突**已修（2026-10-26）**：`static/workhub-modules.css:289` 原先的 `.wh-input{...}` 会连模块外壳类 `.wh-m.wh-input` 一起命中，把 input 类模块（metric_input、progress_row 编辑态）的外壳内距压成 6/8、圆角压成 12。现在面板那一组改成 `.wh-m.wh-input` 双类名（.wh-container / .wh-display 单列一组）：两个类名同时命中外壳，优先级 0,2,0 高于真控件的单类名，控件仍是 `.wh-input` 的 6/8 + 12 圆角，外壳回到 padding 18/16/14 + 圆角 20。别改成 `input.wh-input` 那种写法：namespace 测试（`tests/test_render_modules.py`）要求每个选择器以 `.wh-` 开头。

协作室（`148:7054` 两栏面板，浮层，走 §7.3 的面板状浮层：`bg/surface` + 唯一投影 + `radius/card`）：

- **它是全局的讨论层，不是导航项**：不新增顶级菜单，全站只有右下角一颗 48 圆的按钮（`146:4418`，右下 24 边距）呼出。协作室是独立的一层，**和当前停在哪个页面无关**——左栏会话列表就是全部会话，不分「本页群 / 其他群」。
- **形态 = 底部弹层 + 两栏**：铺满主区宽、从底边升起，**上两角 28 下两角 0**（贴窗口底边）。左栏会话列表 264 定宽（`bg/subtle`，按「管理群 / 汇报群 / 私聊」三段分组）；右栏对话自适应，最窄 520。窗宽 < 900 时只留右栏，会话列表收成对话头里的「‹」返回。
- **三类会话**：管理群（部门管理员之间，谈「这页该不该长这样」）、汇报群（填这页的岗位 + 部门管理员，谈「这格填什么」）、私聊（两个人之间，答疑与口径澄清用）。私聊头像走**圆形 + hairline 描边**，群头像走圆角 12 方块——一眼能分出对话还是群。私聊同样**不授予任何权限**，只是两个人的对话。
- **会话行 264×56，三态走令牌**：默认无填充（透出左栏 `bg/subtle`）、悬停 `fill/hover`、选中 `bg/surface`。头像 32（群 = `radius/lg` 12 方块 + `bg/subtle` 底；私聊 = 圆形 + `bg/surface` 底 + 1px `border/default` 描边）+ 名称 13 Medium + 最后一句 11 `text/muted` 截断 + 右侧时间 11 / 未读 16 圆 `status/danger`。**群与私聊只靠头像形状区分，不新增第二个语义色**。
- **三档宽度，跟着视口走**：宽屏 1313（1440 档，与 Main 对齐）/ 紧凑 960（1024 档）/ 单栏 704（768 档）。
- **高度两档**：默认 560（内容少时不占满屏）、到顶 = 视口高 − TopBar 与边距（1440×987 时 891）。消息一律 `primaryAxisAlignItems: MAX` 贴着输入框排，矮面板看不出差别，拉高才不露空。头部右侧 `⌄` 收起、`⤢` 到顶。
- **发言人标注 = 用户名 + 模型名**：AI 代发时在人名后跟一枚 `model` 变体徽章（写 Codex / WorkBuddy / Claude…），本人发言不带徽章。走**浅紫底紫字**：`data/agent-subtle` 底 + 1px `data/agent-line` 描边 + `data/agent` 紫字，高 20、`radius/pill`。不用实心紫底——徽章的视觉重量不该超过它标注的正文（一屏 3 枚实心紫块会把消息压下去）。不复用「AI 代笔」这四个字当发言人标签——看板上「AI 代笔占比」是统计口径，两者别混。
- **加载更早就在消息区顶部**（`148:9391`，2 变体）：往上滚到顶自动拉下一批，出现「正在加载更早的消息…」（12 号 `text/muted`，居中等宽，不加底色与按钮边框）；全部拉完换成「已经是最早的消息了」，之后不再有动作。**不做整页历史**——协作室永远只装最近的一批，要长历史就是另一个形态，现在不画。
- **不做「采纳到页面」**：协作室只讨论、只解决口径互通，真改页面回员工自己的 AI 软件里做。留一个「转成条目」的入口，协作室就会长成车间入口。

> 分层判断：**骨架层** = 页面外壳（一页一套，全站复用）；**组件层** = 卡片 / 面板内部用的控件与数据件；**浮层** = 浮在整页之上的东西；**协作室** = 右下角圆按钮呼出的全局讨论层；**模块层** = 与 `MODULE_REGISTRY` 对齐、由容器 + 组件拼成的内容块。新组件先判断属于哪一层，再进对应分区。

## 7. 八条硬规则

1. **只用令牌**：颜色、圆角、间距、字号一律引用变量/`--wh-*`，不写字面值。
2. **不发明圆角**：五档里挑一个 —— 微 4、小控件 12、控件 20、卡片 28、胶囊 999；表格本体 0。挑不到就说明该加组件，别加圆角。
3. **内容实色、浮层玻璃**：页面里的卡片 / 表格 / 面板靠底色差与 1px `border/default` 分层，**不加投影、不加模糊**。凡是浮在内容之上的一层——顶栏胶囊（含静止态中间那颗导航胶囊）、账号菜单、开发模式身份菜单、下拉框展开层、Menu、Tooltip——一律「**本体玻璃**」，只有这一套：

   ```css
   /* 条状浮层：顶栏胶囊、导航胶囊 */
   background: rgba(255, 255, 255, .70);
   backdrop-filter: saturate(1.2) blur(10px);
   box-shadow: 0 4px 12px -2px rgba(15, 23, 42, .10);   /* 唯一的投影，别加第二层 */

   /* 面板状浮层：菜单、下拉框展开层、Tooltip —— 白 78% + blur 16，比条状浮层更雾 */
   background: rgba(255, 255, 255, .78);
   backdrop-filter: saturate(1.3) blur(16px);
   box-shadow: 0 4px 12px -2px rgba(15, 23, 42, .10);
   ```

   为什么面板比条状浮层更雾：菜单里要读字，底下正文必须退到雾里，否则两层文字叠在一起谁也读不清；顶栏只承载导航，看得见底下流动的内容才是它存在的意义。两者的投影**同一值**，只差粒度。

   **浮层不能长在 `<details>` 里**（2026-10-26 定，踩过）：账号菜单与身份菜单一度把面板塞在 `<details>` 内部，结果是「菜单透得比顶栏胶囊明显得多」。原因不在数值——`<details>` 是一个 backdrop root，里面的 `backdrop-filter` 只能采样这个 root 内部的绘制内容，拿不到页面背景，玻璃就退化成一层平白，底下的字照样清晰可读。同一份配方挂到 `<body>` 上立刻正常（同一块黑白条纹实测：`<details>` 里条纹残留 p2p 94，`<body>` 上 p2p 4）。所以浮层一律挂 `<body>`、`position: fixed`；触发器只负责「按一下」，面板挂在哪儿、摆在哪个坐标、什么时候收起来，全归**浮层载体** `static/wh-popover.js`（见下）。

   `<details>` 之外，也别把浮层塞进带 `transform` / `filter` / `opacity < 1` / `mask` 的容器：它们同样可能成为 backdrop root，玻璃就只剩一层白。

   **不落遮罩**（2026-10-26 定）：菜单和下拉框底下一度盖过一层 `rgba(10,10,10,.12)` 的薄雾，撤掉了——blur 已经把正文糊开，再压一层灰只会让整页发闷、面板显脏。代价是**「点到别处要关掉」得由脚本兜住**——这一条统一由载体做（document `mousedown` / `Esc` / `focusout` / `scroll` / `resize` 五路），加新浮层不用重写一遍，但**也别把这几路监听删掉**，删了就没有遮罩替你收尾。将来的 Modal 要不要遮罩，等真做时单独定，不要顺手抄菜单。

   圆角按位置取：条状浮层（顶栏胶囊、导航胶囊、徽标胶囊）用 `radius/pill`；面板状浮层（菜单、下拉框展开层、弹窗、Tooltip）用 `radius/card` 28。浮层不加描边——投影 + 明度差已经够分离，再描边会在玻璃上出现一道硬线。

   上面这些值和 `backdrop-filter` 是**唯一允许写字面值的地方**：玻璃是「叠在什么上面」的效果，不是一种颜色，进不了颜色令牌。要改就几处一起改（见 §8.3 的落点清单），别只改一处。

   **浮层只有一个载体**（2026-10-26 定）：三处浮层原先各写各的——顶栏胶囊是纯 CSS，账号菜单与身份菜单是一段 `wireMenu()`，下拉框是 `wh-select.js` 自带的定位。外观统一了，但**挂载 / 摆放 / 关闭**三件事各做了一遍，加第四个浮层就得抄第四遍。现在只有 `static/wh-popover.js`：

   - **声明式**：触发器写 `data-wh-popover` + `aria-controls="<面板 id>"`，面板写 `class="wh-popover"`。可选用 `data-wh-align`（start/end）/ `data-wh-side`（below/above/auto）/ `data-wh-gap` / `data-wh-min-width` 调位置。载体自动把面板 append 到 `<body>`、置 `hidden`、维护 `aria-expanded`、按视口夹取坐标（8px 边距、下方不够翻上方），htmx 换完内容自动重扫。
   - **长相只有一条规则**：`.wh-popover`（玻璃 + 28 圆角 + 唯一投影 + `--z-popover: 70`）。`.account-menu` / `.dev-menu` / `.wh-list` 只声明宽度与内距，**不许再写 background / box-shadow / backdrop-filter**。
   - **要自己管开关的**（比如下拉框按需构建选项行）用 `whPopover.attach({... , manual: true})` 拿实例，其余一律声明式。
   - **契约有测试**：`tests/test_popover_contract.py` 盯着这条——面板跑进 `<details>`、皮肤被抄第二遍、`wh-popover.js` 下到 `wh-select.js` 后面，都会红。

   目前的落点：顶栏（滚动后的窄胶囊；静止态下玻璃在中间的导航胶囊上，条形本身透明；玻璃做在 `.bar-glass` 这一层上，因为带 `backdrop-filter` 的元素会成为 backdrop root，嵌在里面的下拉就取不到页面背景）、账号菜单 `.account-menu`、开发模式身份菜单 `.dev-menu`、下拉框展开层 `.wh-list`。以后加 Modal / Tooltip / Popover 不要另调一套数值——直接照抄上面，再把新落点补进 §8.3。没有描边的实色面板之间必须留 `space/5`（16px）——两块白面板贴在一起会粘成一整块，28 的圆角还会互相啃出缺口。
4. **一个强调色**：`accent/default` 一屏只出现一次主动作；次要动作走 `secondary`（白底 + hairline）。
5. **状态色只表达状态**：不许拿 `status/danger` 做装饰，也不许拿 `data/agent` 做品牌色。
6. **表格单行**：行高 30（6/8 内距 + 18 行盒 + 1px 分隔线），页面数据表的单元格不折行，超宽用省略号（`text-overflow: ellipsis`），不靠缩字号；模块内明细表在窄栏里可以折行。
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
| `data/agent-subtle` | `--agent-soft` | — | #F5F3FF（`model` 徽章底） |
| `data/agent-line` | `--agent-line` | — | #DDD6FE（`model` 徽章边） |
| `radius/card` | `--r-card` | `--wh-radius-card` | 28 |
| `radius/control` | `--r-control` | `--wh-radius` | 20 |
| `radius/lg` | `--r-small` | `--wh-radius-sm` | 12 |
| `radius/xs` | `--r-micro` | `--wh-radius-micro` | 4 |
| `radius/pill` | `--r-pill` | `--wh-radius-pill` | 999 |

### 8.2 这次改了什么

- `workhub-modules.css`：令牌换成 Figma 的中性灰（`--wh-line` #E5E5E5、`--wh-line-strong` #D4D4D4、`--wh-ink` #0A0A0A、`--wh-muted` #737373、`--wh-accent` #171717、`--wh-blocked` #DC2626、`--wh-decision` #D97706、`--wh-done` #16A34A）；新增 `--wh-hover`、`--wh-fill`、`--wh-info`、`--wh-radius-card`、`--wh-radius-pill` 与整套 status soft/line；正文里的 `#f4f4f5`、`#2563eb`、`#1d4ed8`、`999px` 全部改成变量。
- `base.html`：删掉被 shadcn 皮肤块完全覆盖的旧 `:root`（旧暗色系 9 个变量），全文件只留一套令牌；新增 `--ink-2`、`--on-accent`、`--accent-hover`、`--hover`、`--blocked-strong`、`--warning-ink`、`--info-soft` 与 status line 四色；所有字面 hex / 字面圆角改成 `var(--*)`。
- Figma 侧同步：Segmented 容器 8→20、段 6→12；Tabs 6→12；FlowStrip 容器 8→12、阶段块 6→20；RailItem 6→12。Figma 里不再有 6/8 两个档外值。
- 浮层统一成玻璃态：账号菜单与开发模式身份菜单从「纯白 + 投影」改成玻璃 + `0 4px 12px -2px rgba(15,23,42,.10)`；此前只有顶栏有玻璃，菜单还是白纸，点开时像贴了一张卡片上去。
- 浮层分两档：条状浮层 70% 白 + blur(10)，面板状浮层 78% 白 + blur(16)（菜单里要读字，所以面板档更雾）。
- 面板档一度加到 92% 白（同日，先加后撤）：当时误判成「白不够厚」，真因是 `<details>` 是 backdrop root，`backdrop-filter` 压根没生效。把面板挪到 `<body>` 后 blur 回来了，白退回 78%，两个档位重新只差粒度。详见 §7.3。
- 浮层收成一个载体（同日）：新增 `static/wh-popover.js`（约 8KB），账号菜单 / 身份菜单 / 下拉框展开层全部改走它；触发器从 `<details>`/`<summary>` 换成 `<button data-wh-popover>`，`wireMenu()` 与 `wh-select.js` 自带的定位/关闭逻辑删除。原来的三套写法保留的原因只是「各自能用」，代价是同一个 bug 要修三遍（`<details>` 那个 backdrop root 就是这么漏掉的）。面板原来 92% 白的残留也一并清掉，现在与菜单同为 78%。
- 遮罩撤掉（同日）：`.menu-scrim`、`--scrim-soft`、`--scrim`、`window.whScrim` 全部删除。理由见 §7.3——12% 的灰叠在 blur 之上只是让整页发闷，玻璃本身已经够分离；代价是关闭浮层必须靠脚本（document click / mousedown / scroll / Esc）兜住。
- 顶栏玻璃搬家：`backdrop-filter` 从 `header.topbar` 移到内部 `.bar-glass` 层。带 `backdrop-filter` 的元素自己就是 backdrop root，玻璃长在顶栏上时，嵌在里面的账号菜单取不到页面背景，雾只剩一层白。
- 字号与行高收口：徽章 `11.5px → 12px`、开发角标箭头 `10px → 11px`、模块层指标 `.wh-metric-value` `22px → 24px`（与外壳 `.metric .n` 同号）；表格 `th/td` 行盒一度固定 `line-height: 21px` + 上下 10px（行高 41，见下文 2026-10-26 统一到 30）。
- 下拉框统一（同一轮）：20 处原生 `<select>` 的展开层改成 `.wh-list` 面板，收起态加自绘 12×12 细线箭头（`appearance:none` + `padding-right:34px`）；关闭靠 document 的 mousedown + scroll + Esc（当时的 `.menu-scrim` 后来撤了）。
- 看板「AI 代笔 / 人工」图例的两个内联色（`#4c51bf`、`#4a5568`）换成 `data/agent` 与 `text/muted`：它们是令牌化时漏掉的旧调色板，也是模板里最后一处文档外颜色。
- **表格密度统一到 30（2026-10-26）**：外壳表（`base.html:505` 的 `th/td`）从 `padding:10px 12px; line-height:21px; font-size:14px`（行高 41）改成 `padding:6px 8px; line-height:18px`（`td` 13 / `th` 12），加 1px 分隔线实测 31；`.wh-table` 补 `line-height:18px`。同轮给页面数据表加 `white-space:nowrap + overflow:hidden + text-overflow:ellipsis + max-width:320px`（超宽省略，不再靠折行把行撑高）。Figma 侧同步：Table 固定列 `140:3902` 与弹性列 `140:4194` 的 8 个变体全部改 30（padding 6/5 + 8/8、单元格 TEXT 行高 18、复选框框 28×18），固定列首格 244→256 补齐 760−16 的缺口；两张用法卡（`140:4184` / `140:4253`）随之自动回落到 120 / 150 高。
- **`.wh-input` 同名冲突修掉（2026-10-26）**：模块外壳类 `.wh-m.wh-input`（metric_input / progress_row / table / timeline / blocker_list / evidence_gallery 六个模板的面板类）被 `static/workhub-modules.css:289` 的 `.wh-input` 一起命中，内距被压成 6/8、圆角被压成 12。面板那一组改用双类名 `.wh-m.wh-input`（外壳自带两个类），真控件保留单类名 `.wh-input` 的 6/8 + 12 圆角，外壳回到 padding 18/16/14 + 圆角 20。
- **P3 页面稿组件化（2026-10-26）**：`03 · 页面稿` 的手搓件换成组件实例。① 骨架层新增 RailItem 组件组 `148:9283`（10 个 Item 壳里的第 10 个），9 条 Rail 里 25 个手搓导航项全换实例（圆角从档外的 6 归到 12）；② 登录页 Input / Button、审核页 Input ×2 + Select + Button ×2、设置页 Button 全部换实例（父容器交叉轴改 `AUTO`，否则实例撑不开容器）；③ 设置页两张表 + 审核页最近决定表按 30 密度收口：把 TableHead + Row 装进 VERTICAL gap 0 的 Table 壳（`148:9378` / `148:9379` / `148:9382`），删掉原先夹在行之间的 1px Divider，行 30 高 + 只留底部发丝线，表头 `bg/subtle`。**坑**：VERTICAL 壳 `resize(w,10)` 会把 `primaryAxisSizingMode` 打成 FIXED，必须在塞完子节点之后再设 `AUTO`，否则表高卡在 10。
- **窄屏稿的溢出修掉（2026-10-26）**：`148:7737`（768）与 `148:8086`（1024）两张协作室稿的 Main 有 32 内距，但四个子块仍是 Main 整宽（704 / 960），右侧统一溢出 32；改成 `layoutSizingHorizontal: FILL` 后落到内容宽 640 / 896。同两页的 `SplitBar`（AI 代笔 / 人工 占比条）还是宽屏档的 962，超出渠道卡 640，改成 FILL + `SegHuman` 定宽 22%（133 / 190）。复核：12 个 frame 全部无横向溢出。
- **状态矩阵建好（2026-10-26）**：`3:88` 右侧 `148:9383`，见 §1 的说明。四条状态全部由现有组件实例拼成，没有新画零件；新增页面前先对照它。
- **Metrics 指标区分成三个变体（2026-10-26）**：原先只有一个「两行三等分」（`58:689`），但 1440 宽的看板真实页面是 6 等分一行、进度中心 5 等分一行——页面稿与实现分叉。现在 `combineAsVariants` 成组件组 `148:9583`：变体=两行三等分（1024 / 768，对应 `.grid.metrics` 的 3+3 断点）、变体=一行六等分（1226×80，1440 看板）、变体=一行五等分（1249×80，1440 进度中心）。行高 80、卡宽 FILL；汇报中心 `3:1086`、协作室展开 / 到顶两稿换成六等分实例，进度中心的手搓 `MetricsStrip`（1249×100、5 个无底卡）也换成五等分实例。注意：`combineAsVariants` 之后原有实例仍指向原变体，不用重挂；组件组不支持 `layoutWrap`，只能单向排列，撑宽后要把它所在的壳设为 `counterAxisSizingMode: AUTO`。
- 截图自检发现并修掉一处**竖向节奏缺失**：看板页「指标暂不可用」卡片 ↔ 指标行、指标行 ↔ 渠道条之间是 0 间距，白卡片粘成一整块（换成 28 圆角后格外明显）。补 `.card + .grid`、`.grid + .grid`、`.grid + .channel-scorecard`、`.card + .channel-scorecard`、`.grid + .card` 的 `margin-top: 16px`，实测 16px。

### 8.3 已知例外（别照抄，也别改错）

- **玻璃态的五个落点**（代码事实，改之前先确认）：顶栏 `header.topbar.is-scrolled .bar-glass`（720 胶囊，静止态透明）、静止态中间的 `.bar-nav` 胶囊、账号菜单 `.account-menu`、开发模式身份菜单 `.dev-menu`、下拉框展开层 `.wh-list`。前两处是条状档（70% 白 / blur 10），后三处是面板档（78% 白 / blur 16），投影全用同一值；档位混用会看出两种玻璃。这三个面板都由 `static/wh-popover.js` 挂到 `<body>` 上（见 §7.3 的 backdrop root 那条），塞回 `<details>` 里玻璃就失效；面板皮肤只写在 `.wh-popover` 一条规则里，三个面板类只留宽度与内距。**没有遮罩**：透明到能看见底下内容但不该盖一层灰，浮层靠 blur 与投影分离。
- `--r-sm`（=微 4）与 `--r-lg`（=小控件 12）是历史别名，新代码直接用 `--r-micro` / `--r-small`。
- `radius/md`(8)、`radius/xl`(14) 留在 Figma 变量里但不再使用。

- **模块层样式表从来没被加载（2026-10-26 修）**：`static/workhub-modules.css`（662 行，模块层全部规格）此前没有任何模板引用它——部门界面渲染出的 `wh-m / wh-input / wh-table` 在浏览器里一条规则都没生效（实测：外壳 radius 0、padding 0，真控件被外壳的 `input` 规则预制成 9/12）。现在在 `base.html` 的 `<head>` 里、内联 `<style>` 之前挂 `<link rel="stylesheet" href="/static/workhub-modules.css">`：模块层先落，外壳的同名规则后落，冲突以外壳为准。

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
- [ ] 深浅色只用一个方向（当前只有浅色）；半透明玻璃**只**出现在浮层（顶栏胶囊 / 菜单 / 弹窗 / Tooltip），页面内容里没有玻璃、没有第二层投影
- [ ] 新加的浮层只写了 `data-wh-popover` + `class="wh-popover"`，没有另写定位、关闭或第二份玻璃
- [ ] 1440 / 1024 / 768 三档都截过图，窄屏不出现横向滚动条
