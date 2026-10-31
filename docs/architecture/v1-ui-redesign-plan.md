# v1 前端重做方案（推倒重来版）

> **状态：已作废（2026-10-22）。** 用户要求整个项目推倒重来并转向「部门插件宿主」思路，当前主线见 [plugin-host-concept.md](plugin-host-concept.md) 与 [p0-interface-contract.md](p0-interface-contract.md)。本文件保留仅作历史参考，其中的 Figma 五屏与三缺口结论不再作为施工依据。

> 原文状态：方案待裁决　·　依据：Figma「02 · 组件库」页五屏定稿（3:1086 / 3:1087 / 3:1309 / 3:1472 / 3:1652）+ 现有后端实现盘点

> **画布指针说明（2026-11-01）**：本文的 Figma 页名与节点 id 指内部设计画布，该画布已随内部件搬出本仓（`316cf3a`），这些名字只作历史记录；本文件本身也已作废，界面口径以 `docs/design/design-system-v1.md` 为准。
> 结论一句话：**后端语义够用，前端表现层整体重写；唯一硬缺口是顶部指标条的后端聚合，和登录口径。**

---

## 一、先划清「推倒重来」的边界

现在的 1509 行模板 + 255 行内联 CSS（全部挤在 `app/oams/templates/base.html:8-262`）是「能用但不像产品」的形态：无独立样式表、`.topbar` 定义了两遍（base.html:28 与 251）、`:root` 定义了两遍（9 与 232）、`button` 与 `.btn` 两套 padding、9 张表缺 `<th scope>`、flash 无 `role=status`、进度条无 `progressbar` 角色、无 skip-link。

重做的定义：

| 层 | 处置 |
|---|---|
| 视觉（颜色/字号/圆角/间距） | **全部替换**为 Figma 变量体系，产出 `static/app.css` |
| 布局骨架（TopBar + Rail + Main） | **重写**，但保留 `request.state.nav / dept_rail / dept_active`（api.py:1129-1167）这套注入契约 |
| 页面模板 22 个 | **逐个重写**，保留每个模板消费的 Jinja 上下文键名（下表列出） |
| 路由与 handler（web.py 1530 行 / 42 路由） | **不动**，只补缺口 |
| 业务语义（build_board 返回结构、descriptor 校验、审核状态机） | **不动** |

判据：重做完成后，`grep -c 'style=' templates/*.html` 应为 0，`base.html` 只留结构与 `{% block %}`，样式 100% 在 `static/app.css`。

---

## 二、设计系统落地：Figma 变量 → CSS 自定义属性

Figma 侧已建 Color 43 / Space 12 / Radius 6 / Type 11 / Breakpoint 5。代码侧对应建 `static/app.css` 顶部的 `:root`（**只此一处**，杜绝现在两处 `:root` 打架）。

核心色（shadcn/ui 语义，已与 Figma 变量绑定）：

```css
:root {
  --bg-app: #FAFAFA;            /* VariableID:3:5 同族 */
  --bg-surface: #FFFFFF;        /* VariableID:3:5 */
  --bg-muted: #F5F5F5;          /* VariableID:3:17 · 导航/rail 高亮底色 */
  --text-primary: #0A0A0A;      /* VariableID:3:18 */
  --text-secondary: #525252;    /* VariableID:3:19 */
  --text-muted: #737373;        /* VariableID:3:20 */
  --border-default: #E5E5E5;
  --accent: #171717;
  --status-success: #16A34A;
  --status-warning: #D97706;
  --status-danger: #DC2626;
  --status-info: #2563EB;
  --data-agent: #7C3AED;
  --radius-xs: 4px; --radius-sm: 6px; --radius-md: 8px;
  --radius-lg: 10px; --radius-xl: 14px; --radius-pill: 999px;
}
```

字体：PingFang SC，Regular / Medium / Semibold 三档（TEXT 节点无 fontWeight，靠 fontName 区分，映射到 `font-weight: 400 / 500 / 600`）。字号阶：12 / 13 / 14 / 16 / 20 / 24 / 32。

断点沿用变量：sm 640 / md 768 / lg 1024 / xl 1280 / xxl 1440。现有媒体查询只有 `max-width:760px` 一档（base.html:252-261），重做后按 md 为主断点。

---

## 三、组件 → Jinja macro 映射

Figma 组件库（0:1 页）与代码的对应关系，重做时一对一落成 `templates/_ui.html` 的 macro：

| Figma 组件 | 节点 | 落成 macro | 说明 |
|---|---|---|---|
| TopBar | 3:325 | `ui.topbar(nav, active, member)` | 五点：进度中心 / 汇报中心 / 审核管理 / 设置（登录屏旧文案需同步） |
| Rail | 3:326 | `ui.rail(items, active)` | 7 项：首页 + 6 个部门；活跃项 `--bg-muted` + `radius-sm` |
| PageHead | 3:327 | `ui.pagehead(title, sub, actions)` | |
| Segmented | 3:328 | `ui.segmented(name, options, value)` | 对应现有 `.orgnav` |
| Tabs | 3:329 | `ui.tabs(items, active)` | 两种形态：`.tabs`（反色）/ `.tabs.layers`（无边框 chip） |
| FlowStrip | 3:330 | `ui.flowstrip(flow)` | 直接吃 `build_board.flow`：id/label/count/selectable/branch/note |
| ItemCard | 3:331 | `ui.itemcard(card)` | 左侧 3px 状态色条 |
| Metric card | 3:169 | `ui.metric(value, label, delta?)` | ⚠️ 数值后端暂时给不出，见第五节 |
| MetricGrid | 3:923 | `ui.metricgrid(items)` | 5–6 格 |
| Button | 3:142 | `ui.btn(label, variant, href?)` | 合并现在 `button` / `.btn` 两套 padding 为一个 |
| Badge | 3:159 | `ui.badge(kind, text)` | kind: blocked / decision / agent / human / vacant |

---

## 四、22 个模板改造清单

按重做工作量与风险排序。「上下文」列是该模板当前依赖的变量，重写时**不能改名**。

### P0 · 骨架与主路径（6 个，重做后整站可看）

| 模板 | 路由 | 现状 | 关键上下文 |
|---|---|---|---|
| base.html | 全部 | 311 行，255 行内联 CSS | `request.state.nav / dept_rail / dept_active`、`member` |
| board.html + board_body.html | /board、/board/body | 看板主视图 | `org_units, selected_id, board, stale_days, layer, stage, status_labels, kind_labels, channel_labels` |
| progress.html | /progress | 全公司进度 | `departments, totals, stale_days, status_labels, channel_labels` |
| department_workbench.html | /departments/{id} | 部门工作台（无 active 页时） | 部门、岗位、工作项 |
| reviews.html | /reviews | 页面审核 | `pending, decided, csrf, saved, error` |
| login.html | /login | 登录 | `error, next` |

### P1 · 部门自建界面链路（5 个）

`interfaces.html`、`interface_board.html`、`interface_fill.html`、`department_new_page.html`、`setup_interfaces.html`
（`setup_interfaces.html:35` 有坏链：`<a href="/static">` 指向静态目录，应指向 docs 或删掉）

### P2 · 汇报与设置（4 个）

`reports.html`、`settings.html`、`setup.html`、`denied.html`

### P2 · 管理端 4 个

`admin_audit.html`、`admin_positions.html`、`admin_spaces.html`（含 `/static/office-editor.js`）、`admin_work_items.html`

### P3 · 办公区平面图 2 个

`office.html`、`office_zoom.html`

**9 张表统一补 `<th scope="col">`**：admin_audit、admin_positions、admin_spaces、admin_work_items、interface_board、interfaces、reviews 各 1 处，另 2 处在 board/progress。

---

## 五、必须先补的后端缺口（否则页面做出来是空的）

### 缺口 1 · 顶部指标条算不出来（阻塞 MetricGrid）

Figma 五屏顶部都是 6 格指标（坑位定稿 / 定稿 / 打样中 / 待选品 / 待下单 / 已下单）。这类数字是「部门自建字段的计数」。

实际情况：`metric_card` 只在 `descriptor.py:20` 的 `DEFAULT_MODULE_TYPES` 里出现，**api.py / web.py / render.py 全仓没有任何 metric 聚合代码**。`api.build_board` 的 `summary` 只能算 open_items / blocked / needs_decision / never_reported / overdue / positions_without_holder / latest_report_by_channel 这 7 类。

要做的事：在 `interface_entries.extra`（jsonb，已有 GIN 索引 `extra_idx`）上按 descriptor 声明的 field 做 `GROUP BY` 计数，产出一层 `summary.metrics`；`mode='draft'` 的 entries 必须排除（`0006_interface_draft_entries.sql`：草稿期永不进公司汇总），`work_item_id IS NULL` 的也必须排除（`0005_department_interfaces.sql` 注释：未关联工作项不能进公司汇总）。

### 缺口 2 · 界面汇报 → 公司总览没打通（P0 级业务缺口）

`web.py:495` 与 `web.py:813` 写 `interface_entries` 时**从不设置 `work_item_id`**，而 `web.py:904` 的 `/progress` 只读 `work_items`。结果：部门在自己界面里填的东西，公司总览一分都看不到（`.artifacts/progress.png` 截图里计数器全 0 就是这个原因）。

### 缺口 3 · 登录口径冲突（需裁决）

- **设计稿**：登录屏 3:1472 是「公司分配的邮箱 + 密码」两字段，LoginCard 400x496。
- **后端**：`workforce_members` 表（0001 迁移）**根本没有 password 字段**，全仓 grep `password` 在 `app/oams/*.py` 与 `migrations/*.sql` 里 0 命中。现有 `login.html` 是「粘贴会话令牌」单字段，`POST /login` 收 `token: str = Form(...)` 走 `principal_for_token`。
- **文档口径**：`docs/architecture/v1-ui-scope.md:91-95` 明确 v1 用 CLI 令牌登录（`GET /login?token=` 一次性链接为首选）；`v1-hr-department-scope.md:103` 写「不做员工自助登录，独立账号中心后续接入」。

三条路，必须选一条：

| 方案 | 代价 | 建议 |
|---|---|---|
| A. 改设计稿为单令牌字段 | 半天，不改后端 | ✅ **推荐**（v1 内网，账号中心未就绪） |
| B. 后端加密码字段 + 登录 | 3–5 天，含改密/重置/锁定/审计 | 除非确定 v1 就要对外 |
| C. 邮箱 + 令牌双字段 | 2 天，邮箱只做展示匹配 | 折中 |

---

## 六、执行顺序（每阶段可独立验收）

**阶段 0 · 收口未提交工作**（0.5 天）
83 项变更（61 未跟踪 + 21 修改 + 1 删除）先落一个 commit。补 `0004_spaces.sql` 缺失、把 0005–0008 恢复为符合 `.workbuddy/memory/MEMORY.md:35` 约定的形态、`.gitignore` 补 `data/live-design-*.json`。**重做前不清理，出问题无法回滚。**

**阶段 1 · 样式地基**（1 天）
新建 `static/app.css`（只此一个 `:root`）、`templates/_ui.html` 十二个 macro、重写 `base.html` 为纯骨架。此时页面会「脱样式」，属预期。

**阶段 2 · P0 六模板重写**（2 天）
看板 / 进度中心 / 部门工作台 / 页面审核 / 登录 / 骨架。每页按 Figma 帧逐层比对。

**阶段 3 · 后端三缺口**（2–3 天）
先补齐缺口 1 的 metric 聚合，再打通缺口 2（写 entries 时按 title/position 反查或建 `work_item_id` 关联），缺口 3 按裁决结论执行。

**阶段 4 · P1/P2/P3 收尾 + 无障碍**（3 天）
11 个模板重写；`<th scope>`、flash `role=status`、进度条 `role=progressbar`、skip-link 一并做掉。

总计约 **9–10 个工作日**。

---

## 七、待裁决 4 项

1. **登录**：A / B / C 选哪个？（影响阶段 3 工期 0.5 天 vs 5 天）
2. **Rail 第 6 项**：Figma 五屏写「运营部」，文档口径是「商品部」，以哪个为准？
3. **第 6/7 屏**：汇报中心、设置两个页面 Figma 里还没有设计稿，是先在 Figma 补屏，还是直接按现有模板结构重写？
4. **阶段 0 的 83 项变更**：是否授权我先做一次提交收口？

---

## 八、这份方案的可验证性

方案里每条判断都有对应证据：
- 模板行数 / 内联 style 数 / `<th>` 缺失数 —— 遍历 `app/oams/templates/` 所得
- build_board 返回键 —— `app/oams/api.py:929-1021` 实读
- 登录无密码字段 —— 0001 迁移表定义 + 全仓 grep
- metric 无聚合 —— 全仓 grep `metric` 仅命中 `descriptor.py:20`
- entries 不写 work_item_id —— `web.py:495`、`web.py:813`

**未验证项**：docker daemon 未运行、无 PG 实例、`.venv` 无 pytest，所有 DB 级 e2e 本轮无法复跑。方案中「现状」判断均为代码/文档自洽，不含运行验证。
