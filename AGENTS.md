# AGENTS.md — OAMS 协作约定

OAMS（Organizational App Management System，组织应用中台）：组织内 AI 应用的宿主与应用商店——应用由部门用 AI 写，跑在同一套底座上。Python 3.11 + FastAPI + Jinja2 + htmx 服务端渲染，PostgreSQL。**没有前端构建链**（无 package.json / 无 tailwind / 无 React）：前端资产只有 `app/oams/static/vendor/htmx.min.js`、`static/oams-modules.css` 和各模板里的内联样式。

## 做界面（UI / 样式 / 组件）之前必读

1. `docs/design/design-system-v1.md` —— **界面唯一口径**：颜色/圆角/间距/字号令牌、三层结构（骨架层 / 组件层 / 模块层）与 38 个组件组 + 14 个模块组件的节点 id、八条硬规则、验收清单。
2. `docs/design/tokens.json` —— 令牌机器可读镜像（85 项）。
3. **界面规范以 `docs/design/design-system-v1.md` 为唯一口径，本文即真源**（组件与视觉都按它）；内部画布（Figma 那类本地文件）不在本仓，画布规则与归档问界面。改规范先改这份文档，再改代码。
4. `docs/design/brand-assets.md` —— 品牌标志：开源版用的中性标、9 张回归图、以及怎么换回内部版。

六条最容易犯的错：

- **别引 shadcn/ui 的代码**。它要求 React + Tailwind + npx，本项目没有也不该有；我们只借它的**视觉语言**（白底、hairline、语义色、五档圆角、无阴影、一个强调色）。
- **别写字面颜色/圆角**。仓库是**两套令牌、两层作用域，故意的**：**外壳**（`app/oams/templates/base.html` 的 `:root`）用短名 `--bg`/`--card`/`--ink`/`--muted`/`--line`/`--accent`（另有一批派生值），**模块层**（`app/oams/static/oams-modules.css`，作用域 `.wh-interface` 等）用 `--oams-*`（如 `--oams-ink`、`--oams-radius-card`）。**两层不互相借**——模块层取不到 `--ink-2`，外壳也取不到 `--oams-*`。值与 `docs/design/tokens.json` 对齐，新增值先加令牌。
- **圆角按元件尺寸分档（学苹果）**：容器 **26**（卡片/弹窗/外壳浮层：账号菜单、开发模式菜单）、中等块 **18**（分段外框/组织导航/提示条/模块块/字段行）、行与高亮 **9**（菜单行/chip/侧栏行/骨架条）、**输入框/下拉/文本域走 18**（它们都在卡片里，是二级控件；30 高时被截到 15，看起来接近胶囊）、方框 **4**（复选框）；**卡片里拉出来的浮层（下拉框展开层、卡片内菜单）走 18**，不要跟着卡片再用 26；**999 只给按钮、搜索框、徽章、开关、页码；输入框/下拉/文本域别写 999**（它们写 18，30 高的盒子上自然渲染成胶囊，那是要的效果）；表格本体 0。不要出现第六种值（见 `docs/design/design-system-v1.md` §3）。连续曲率（`cornerSmoothing`）只在本地画布上成立，CSS 用圆弧、不做连续曲率。
- **排多列只用内容区 12 栅格**：模板里包 `.grid-12`，子元素标 `.span-12/8/6/4/3`（白名单五档，gap 固定 16），**不许手写 `grid-template-columns` / 任意 `fr` 比例**；有底的面板同行等高、无底的区块不撑高（两条选择器已写在外壳里）。口径、断点与例外见 `docs/design/layout-grid.md`，验收用 ego 浏览器跑（见「浏览器自动化」）。
- **间距三个数**：块与块 16、元素与元素 12、行内 8（**标签↔控件也是 8**，没有 6 这一档）。别写 10 / 14 / 18 这种（见同文件 §4）。依据：`Field 表单行` 两个变体 `itemSpacing` 都是 8，`Label Row` 高 20 → `Input` 顶 28、`Input` 底 58 → `Hint` 顶 66，全是 8；代码 `.field` 本来就是 8，2026-10-31 撤销「6」这一档，改的是文档不是代码。
- **控件基准线 30**：`input` / `select` / `button` 都是 30 高、内距 0·12（顶栏导航胶囊也是 30），高度靠 `height` 撑，不要用 `padding` 凑（会算出 35.5 / 36 这种档，跟按钮排一行就错位）；`textarea` 最小高 120。
- **嵌套圆角走同心值**：贴边内层（内距 ≥ 8）取 `内层 = 外层圆角 − 内边距`（卡片 26 + 内距 16 → 行 9；分段 18 + 内距 8 → 段 9）；内层圆角比它**小**反而会「咬角」，别靠调小内层解决（见同文件 §3.1，日常不用算）。

## 做部门界面（descriptor）之前必读

`skills/oams-department-interface/SKILL.md` + `docs/architecture/p0-interface-contract.md`。硬约束：容器只排版不做数据；字段必须映射标准字段（至少 `title` / `status` / `evidence`）；状态必须映射到 `planned / in_progress / blocked / done`；模块只能从 `app/oams/modules.py` 的 `MODULE_REGISTRY` 白名单（21 个 key）里选。
验收 descriptor / 页面能不能上手时，**要按第三方开发者视角跑**：只给它 `skills/` 里的说明书和一个真 AI，让它从一句话需求写出一页。拿仓库里自己写的样例和自己造的数据当验收不算数——自己出题自己考，验不出外人卡在哪。

## 运行

```sh
# 一条命令：挑依赖路（有 uv 走 uv，没有才 pip）、装依赖、建表、建管理员、起服务并打印登录地址
bash scripts/quickstart.sh --database-url "postgresql://<账号>:<口令>@127.0.0.1:<端口>/<库名>"

# 要单独操作时（都只用 .venv 里的命令，不依赖激活）
.venv/bin/oams db init               # 需要 OAMS_DATABASE_URL；建表，重复跑安全
.venv/bin/oams bootstrap-admin --name ... --employee-code ... --org-code ... --org-name ...
.venv/bin/oams serve                 # 端口跟 OAMS_BIND_PORT
scripts/start-local-trial.sh          # 本机试用，默认开 OAMS_DEV_AUTOLOGIN=1
# 跑测试先装开发依赖（[dev] 里有 pytest / httpx；只跑服务不需要它们）
.venv/bin/pip install -e ".[dev]"    # uv 环境：VIRTUAL_ENV=.venv uv pip install -e ".[dev]"
OAMS_DATABASE_URL=... .venv/bin/python -m pytest   # 不给库地址 = 整批 db 用例静默跳过，不是绿
# skip 数怎么读见 README「想跑测试」：主仓刚建库 4、灌了种子 0
```

## 浏览器自动化

需要浏览器自动化的地方（三档截图、栅格检查、断网重发验收）**一律用 ego 浏览器**，不再新写自研 CDP / Playwright 脚本。

- 命令：`ego-browser`（按你自己的安装路径，一般已在 PATH 里）；动手前先读 skill（`ego-browser skill`）。
- 脚本写到 `/tmp/xxx.mjs`，用 `ego-browser nodejs < /tmp/xxx.mjs` 执行；**不要**用 `-e` 内联，遇引号会被截断。
- 视口没有 API，用 `page.cdp("Emulation.setDeviceMetricsOverride", {width, height: 900, deviceScaleFactor: 1, mobile: false})`。
- 三档验收（1440 / 1024 / 768）：断言 `document.documentElement.scrollWidth === clientWidth`，出图用 `page.screenshot({path})`。
- 验收脚本三个，都 ego 驱动，跑法 `ego-browser nodejs < scripts/xxx.mjs`：`ego-grid-check.mjs`（12 栅格三档量测）、`ego-collab-failure-check.mjs`（断网重发）、`ego-measure.mjs`（布局量测，服务三个布局用例）。自研 CDP 脚本已全部删除，**不许再新增**同类自研脚本。

## 目录

- `app/oams/` —— 应用本体：`modules.py`（模块注册表）、`templates/`、`static/`、`mcp/`（12 个 MCP 工具）、`cli.py`
- `docs/architecture/` —— 契约与方案（`plugin-host-concept.md` 是现行主线，`v1-ui-redesign-plan.md` 已作废）；`collab-room-implementation-plan.md` 是协作室的实现清单（设计稿见 `docs/design/design-system-v1.md` §7）
- `docs/design/` —— 界面规范与令牌
- `app/oams/schema.sql` —— **唯一一份**建表脚本：**19 张表 + 1 个视图**（`work_item_current_state`），`oams db init` 幂等执行。按 `information_schema.tables` 数会是 **20**（视图也占一行），别当成多建了一张表；老库可能还留着旧的迁移台账 `oams.schema_migrations`（`db init` 既不再建也不删），那样会数出 21。双角色时期的建库脚本已删除（2026-11-02）；现在只有单角色，建库建表见 `oams db init`
- `examples/` —— 能过校验的 descriptor 样本；`skills/` —— 给外部 AI 的 skill（两个：`oams-department-interface` 做部门界面、`oams-design-system` 做页面与组件；引用时写真实目录名，别指向不存在的路径）
- `data/` —— 演示种子：主种子 `neutral-demo-departments.json` **别动**（现有数据与测试基线按它算），要加部门就另开一份（如 `it-department.json`），灌库走 `scripts/load_department_data.py <file> --apply`，这样下一个 clone 的人能复现演示站的前置。

## 约定

- 中文优先：注释、文档、界面文案都用中文。
- 提交前跑 `pytest`；改 UI 要截图自检（1440 / 1024 / 768 三档，窄屏不许横向滚动），工具与命令见「浏览器自动化」。
- **验收 pytest 要两种机器状态都跑，并且看 skip 数**：本机有库 → **0 skipped**，这才是真绿；故意指向不存在的库 → 允许 skip，那只证明「无库不炸」。skip 数一涨就说明有库没被用上，别拿 `0 failed` 当验收口径。
- **新库上最容易出的假绿：db 用例整批静默跳过。** 在一套刚建的空库上（没跑 `db init` 或没灌种子）直接 `pytest`，会得到「全绿 + 4 秒跑完」——**592 passed / 207 skipped / 0 failed**，看起来完美，实际数据库用例**一条都没跑**。原因在 `tests/conftest.py`：schema 没建时 `:259` 直接 `pytest.skip`，种子行缺时 `:237` 直接 skip（另有 `:270` 缺 `ADMIN001` 也 skip）。所以**跑全量前先确认 `db init` + `bootstrap-admin` + 两个种子脚本都跑过**，看到 `skipped` 不是 0 先查库，别急着报绿。参考（2026-11-01 实测，同一台机器）：空库无种子 → 592 passed / 207 skipped；种子齐 → 796 passed / 3 skipped（那 3 条是「内部重写脚本不随源码发布」的声明，属正常）。
- **跑全量有两条禁令**：**不许套 `timeout`**——本机没有这个命令，套上去整轮根本没跑，你却以为跑了；**也不许用管道取退出码**——`pytest … | tail` 的 `$?` 是 `tail` 的 0，测试全挂也报成功。成功与否**只看 pytest 自己最后那行汇总**（`N passed` / `N failed` / `N skipped`）；要存日志就重定向到文件（`pytest > /tmp/pt.txt 2>&1; echo "exit=$?"`，退出码要在同一行紧接命令取，别跨管道）。依据：2026-11-01 一次「跑过全量」两条禁令同时踩中，整轮其实没执行。
- **同一时间只允许一个人跑全量 pytest**：两人同时跑会互相踩出假红——同一棵树、同一个库上并行跑到 1 failed / 809 passed，串行重跑是 810 passed。并行期间看到的红先当无效结果，串行重跑过再下结论。
- db 用例不许在 import 期写 `OAMS_DATABASE_URL`，地址一律由 fixture 注入。
- **查不到条目时，先看页面是「草稿版」还是「已启用版」**：`_entry_mode_for`（`app/oams/web.py:585`）只有 `status == "active"` 才走 `entry_mode='live'`，其余一律 `draft`——条目落在 active 版上时，用一张还在审核的草稿版页面去查必然查不到，表现为「数据明明在、用例假红」。反例形状：拿库里已有的条目直接去查 `pending` 页面（`16f3e6b^` 上红过一次，出处 `08bd166` 那两条）。写用例时把目标版本真走完「提交 → 部门初审通过」，别只插数据。
- **动过 Python 模块就重启服务再验页面**：模板每次从磁盘读（Jinja loader，`app/oams/render.py:753` 的 `_ENV`），Python 模块只在进程启动时 import 一次、再把函数对象绑进模板全局（`render.py:762` 的 `_ENV.globals.update(...)`）。所以改完 `render.py` 这类模块的函数签名再刷页面，跑着的旧进程还算旧签名，报 `TypeError: input_for_key() got an unexpected keyword argument 'prefill'`（2026-11-01 演示站 500 即此）。**不重启就验的是新旧混着的组合**，那种「验过了」不算数。
- 不往生产库写、不连公网暴露 PostgreSQL；改动 `.env` 前先看 `.env.example`。
- **停服务、杀进程一律按 PID**：不许写按命令行片段宽匹配的 `pkill -f "…--port 8000"` 这种收尾——匹配范围比你以为的宽，一次验端口就把演示站进程杀了。先用 `pgrep -f` 列出来核对，再 `kill <PID>`。
- **8000 上的演示站只许用固定脚本起**：`scripts/start-local-trial.sh`（免登录 + 演示库两个开关都在里面）。裸跑 `oams serve` 会把这两个开关一起丢掉——页面变成要会话令牌，还会静默连到开发库、数据换了一套却不报错；8000 也不许随手重启（别人重启过一次，症状就是这个）。见页面异常先报，别自己点。
- **谁的写区谁提交**：改完自己 `git add` 自己写区内的文件、自己 commit、把哈希报上来。别攒着等统一提交——攒着会让线上跑旧代码。
- **提交前先看 `git diff --cached --name-only`**：暂存区只含本次改动、且全在自己写区内才提交；`git commit` 一律带路径（`git commit -m … -- <路径>`），不带 pathspec 的提交会把别人暂存的东西一起带走。归属错了就是**回滚粒度没了**——出问题时没法单独退掉一笔，只能整笔回退。
- **共享分支上不许改写别人的提交**：不用 `--amend` / rebase 去补别人已经提交的东西，要补内容就另开一笔。就算事后还原成功也不行——哈希没变，但那段时间别人拉到的历史并不存在。
- 只有确实会撞别人文件时才排队等，等的时候说清撞的是哪个文件。
- 提交前照旧跑 `pytest`；文档改动也要提交，不许只在工作区躺着。
- **开源验收一律全仓 0 命中，不排除任何目录**：写成 `git grep -n '旧名' -- .`。发现旧记录与现状不符，改现行文档（`docs/architecture/`、`docs/design/`）并在里面说明，别重写历史记录；靠排除目录、改旧记录把指标做绿，等于为了让指标好看而篡改证据。
- **文档口径先行，行为以仓库实际为准**：本轮文档统一写目标名（包 `app/oams/`、环境变量 `OAMS_*`、schema `oams`、命令 `oams db init`），代码可能还没跟上——这是排期，不是笔误。两边不一致时报给负责人，别自行把文档改回旧名；同一事实只在文档里写一次目标口径，别到处贴「暂未实现」的补丁。
- **文档里举例不写真名**：主机名、账户名、客户名一律用 `x[.]local`、`<原品牌>` 这类占位写法——成品字面量进了仓库就再也洗不干净，公开前只能整文件重写；这条坑已经在报告、手册、标志说明页踩过三次。
- **写进文档 / 说明书的命令，先在这台机器上原样跑一遍再发**：`python` 不在 PATH、系统 `python3` 没装 PyYAML，一条照抄的转换命令就把外人挡在下一堵墙前（报错还看不懂）；对外一律给产品自带的能力（如 `oams plugin convert`），别让人现装依赖。

### 共享库写操作：先锁行 id

**共享库**（演示站、团队共用的开发库等，不是只跑测试的临时库）上的写操作，动手之前必须先锁死目标行：

- **先断言，再动手**：点下去之前先取到目标行的 id（或标题）并与预期比对，不一致**立即中止**、报告，不许「差不多就点」。
- **不许按屏幕上的文字挑元素**：外层文字、卡片标题、按钮文案都可能重复，`getByText` 这类选择器在多卡片页面上会点错人。
- **SQL 同理**：`UPDATE` / `DELETE` 必须带 `WHERE id = ...`，改之前先 `SELECT` 把要动的行打出来核对；不许按 `name` / `title` / 状态这类非唯一条件批量改。
- 这是**数据事故的预防，不是代码风格**。实例：2026-10-31 一个按外层文字定位的按钮点在了另一张卡上，把一条待审页误批成通过（已还原）。
- **先看清连的是哪个库再下结论**：共享库（演示站、团队开发库）与你自己本机的库数据不一样（部门、岗位、种子都不同），同一个现象可能只在一个库里成立——把库名与端口写进结论，别把一边的报错当成另一边的事实。

### 回报与派活（省 token）

- **回报固定四行，≤150 字；单条硬上限 200 字**：`结论：一句话`｜`改动：文件:行 或提交哈希`｜`验证：命令 → 结果`｜`待办：还剩什么、要谁拍板`。不写散文、不复述过程。**超过 200 字一律重写**——包不住就是把过程写进正文了，过程该落盘。
- **一个人一条线**：一件事只派给一个人、只等这一个人的回报；要多角色协同就拆成先后两步，不搞「同时开工、等三方对齐」。手上同时进行的指派不超过两件。
- **报数必须带前提**：提交数、用例数、命中行数、量测值这些数字，一律写清是在哪个提交 / 哪棵树 / 哪个库上跑出来的；不带前提的数两边对不上，还会被下游当成事实照抄（同一件事在旧树与新树上数不一样，为这个已经返工过）。
- **长内容一律落盘**：diff、日志、清单、逐条现状、截图对照 → 落成一份内部记录（不进本仓，或进 commit message），消息里只发一段摘要 + 路径。禁止在消息里贴大段 diff、代码原文、报错堆栈。
- **派活用模板，≤200 字**：`目标` / `写区（具体文件路径）` / `验收（命令 + 期望结果）` / `时限`。不抄需求原文与背景解释，对方有项目文档，让它自己读。
- **凡是「系统在什么时候做某件事」这类判断，要么写清依据哪一行代码，要么明说「你去核实」**：凭印象写的判断会被下游当成事实执行（实例：派活说「三条判据都在提交时判」，实际是部门初审点「通过」那一刻才判，下游照抄差点写错文档）。
- **不重复共识**：已拍过的事不再复述理由，只有真正有分歧才展开；同一事实一条消息里只说一遍。
- **零信息量的回执一条都不发**：「收到」「明白」「停手」「收工」这类每发一条就是往对方队列里塞一条垃圾，对方还得花一轮去读；对方重复问同一件事，回一句「已答，见上一条」就结束，别把原答案再贴一遍。
- **单轮别跑长命令**：一条命令预估超过 2 分钟就拆开跑、每段报一次；一轮超时就先回一句进度再继续（实例：挂着一条 7m15s 的命令，收件箱堆进 19 条回声）。
- **一轮一条消息**：不发「补充说明」「另外再补充」，多条合并；只发该看的人，不全员广播。
- **Bot 间消息用单行紧凑字段**（≤60 字，`;` 分隔，没有的字段省略）：

  `S=状态;T=主题≤10字;F=文件路径;V=验证命令:结果;C=提交哈希;Q=要谁拍板什么;P=详情文件路径`

  状态只有四个：`done` / `doing` / `blocked` / `need`。例：`S=done;T=descriptor禁词;F=app/oams/descriptor.py;V=pytest:649/0/0;C=82331b5;Q=none`
  派活用同一套：`S=task;T=任务;F=写区;V=验收;D=时限`。
  例行进度、交付回执、验收结果一律用这个；细节走 `P=<内部记录路径>`。
  **例外**：有分歧、要在方案里选、需要解释判断的，用自然语言三句以内，别硬压成机器格式——压错返工更贵。
  **给老大的汇报绝不用这个格式**，仍然大白话（优先级最高）。
