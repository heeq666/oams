# 在你电脑上跑起来（本地试用）

这份说明让这套系统在你自己的电脑上跑起来，并点开几个页面。全部是本机地址，不对外网开放。

命令是 `oams`——下文出现的 `oams ...` 都是它。

## 连库之前：认准你用的是哪个库

本手册只建**一个** PostgreSQL（下一节就建）。容器名、端口、库名全由你定，下文一律写成 `<你的容器名>` / `<你的端口>` / `<你的库名>`——照抄别人机器上的数字没有意义。

连接串里的用户名和口令**一律用变量拼**：口令只放在你自己的环境变量或项目根目录的 `.env` 里，不写进文档、不提交、不贴群里。变量怎么设见下一节。

两条规矩：

- 如果你机器上不止一个库，**所有 e2e 结论必须注明用的是哪个库**（端口 + 库名，如 `<你的端口>/<你的库名>`）。数对不上时，先看是不是同一个库——串库的典型表现是「刚建的东西不见了」。
- 项目根目录的 `.env` 是 `pytest` 与本地开发的默认库；跑 `pytest` 前确认环境里没有残留的 `OAMS_DATABASE_URL`，否则会打到别的库上去。

## 一次性准备

1. 确认 Docker 已启动（桌面版打开即可）。
2. 在项目目录执行（只做一次）：

```sh
# 用户名/口令按你本机的实际值填；口令不要写进文档、不要提交
export OAMS_DB_USER=postgres
export OAMS_DB_PASSWORD="<你的口令>"
# 把 <你的端口> 与 <你的库名> 换成你自己定的：端口挑一个本机没被占用的，库名随你
export OAMS_DATABASE_URL="postgresql://${OAMS_DB_USER}:${OAMS_DB_PASSWORD}@127.0.0.1:<你的端口>/<你的库名>"

docker run -d --name <你的容器名> \
  -e POSTGRES_PASSWORD="$OAMS_DB_PASSWORD" -e POSTGRES_DB=<你的库名> \
  -p 127.0.0.1:<你的端口>:5432 postgres:16-alpine

.venv/bin/python -m oams.cli db init
.venv/bin/python -m oams.cli bootstrap-admin \
  --name "系统管理员" --employee-code ADMIN001 --org-code COMPANY --org-name "公司"
```

上面那几行 `export` 只在当前终端有效：**新开终端要重跑一遍**（口令换成你自己的，或从 `.env` 里读出来再 export）。下文所有命令都默认这几个变量还在。

把 bootstrap 打印的 `member_id` 记下来，下一步要用。

这条命令只能成功一次：库里已经有全公司管理员时，它会直接拒绝并退出（`An all-scope administrator grant already exists; refusing to bootstrap another administrator.`），这时不会有 `member_id` 打印出来——去 `/setup` 页的「现有成员」里认，或从库里查。

## 准备一个部门

```sh
# 沿用「一次性准备」里 export 的 OAMS_DATABASE_URL；新开终端就先把那几行重跑一遍
export OAMS_ADMIN_MEMBER_ID=<上一步的 member_id>
.venv/bin/python scripts/load_department_data.py data/neutral-demo-departments.json --apply
.venv/bin/python scripts/seed-live-interface.py data/neutral-demo-ops-weekly.json
```

`load_department_data.py` 不带 `--apply` 时是**只看不写**的预演（逐条打印 `[new]` / `[exists]`，末尾一行 `N new, M already present`）。先跑一遍确认要建的东西对，再加 `--apply` 落库。

不想敲命令也可以跳过这一节：服务起来之后用 [`/setup` 引导页](#建部门岗位成员用-setup-引导页最快) 点几下就建好了，那是本机试用里最快的一条路。

它会自己起一个临时服务把部门、岗位、成员、授权跑一遍完整接口（跑完自动关），默认在 127.0.0.1:8126，端口被占用时用 `OAMS_LOADER_PORT` 换一个。`OAMS_DATABASE_URL` 和 `OAMS_ADMIN_MEMBER_ID` 两个都要给，缺一个会直接报错退出（双角色那套 `OAMS_MIGRATOR_URL` 已随机制移除，不用再设）。

第二个脚本会把[运维周报界面](../../data/neutral-demo-ops-weekly.json)启用，作为你点开就能看的示例。它每跑一次都是「归档上一版、启用新的一版」，所以**页面的地址（`/interfaces/<id>` 里那个 id）会变**，之前收藏的旧链接会失效、打开后被带回能看的页面。要的就是这个效果：页面描述升级过之后重跑一次，旧版自动退场。

## 启动服务

**推荐用 `oams serve`**：它读同一份配置，不用记 uvicorn 的参数。

```sh
# 同上：OAMS_DATABASE_URL 要在当前终端里有效
.venv/bin/python -m oams.cli serve
```

默认起在 `127.0.0.1:8000`。要换端口就设 `OAMS_BIND_PORT`（换监听地址设 `OAMS_BIND_HOST`），例如：

```sh
OAMS_BIND_PORT=8001 .venv/bin/python -m oams.cli serve
```

它底层就是 uvicorn，直接调 uvicorn 是等价的备选写法（不带环境变量时，下面这条与 `oams serve` 的默认值完全一样）：

```sh
.venv/bin/python -m uvicorn oams.api:app --host 127.0.0.1 --port 8000
```

保持这个终端开着。

8000 被别的进程占着时，这条会起不来（`address already in use`）。换一个端口——`oams serve` 用 `OAMS_BIND_PORT=8001`，直接调 uvicorn 用 `--port 8001`——然后**下文所有 `127.0.0.1:8000` 的地址都要跟着换成 8001**：登录链接、四个页面的地址都一样。

## 设计开发阶段：免登录

现在还在设计开发阶段，每天反复签登录链接只是挡自己。启动时带上 `OAMS_DEV_AUTOLOGIN=1` 就打开免登录：

```sh
OAMS_DEV_AUTOLOGIN=1 .venv/bin/python -m oams.cli serve
```

没有登录态时服务按 `ADMIN001` 认人，页面右下角常驻一枚「开发模式 · 免登录 · 姓名」角标，「退出登录」也会明确告诉你退不掉。

它只决定「我是谁」，不决定「我能干什么」：权限、部门范围、表单令牌、审计一条都不少，所以看到的页面和登录后完全一致。

恢复真实登录：

```sh
OAMS_DEV_AUTOLOGIN=0 .venv/bin/python -m oams.cli serve
```

想让免登录认别人就写工号，例如 `OAMS_DEV_AUTOLOGIN=DESIGN001`。工号不存在或已离职时会停在登录页并给出提示，不会静默当成管理员。

### 右下角角标可以换身份

点开那枚角标就是身份切换菜单：列出库里所有在职成员，按权限强弱排（系统管理员 → 部门管理员 → 普通员工），右边标出这个人的实际层级（由 grants 推出来，不是岗位名）。点一个就换成他看这一页，不用改环境变量、不用重启服务；切换结果记在 `oams_dev_worker` cookie 里，12 小时有效，菜单底部还能「恢复默认身份」回到启动参数指定的那个。

调试权限时很好用：同一个页面，管理员看到的导航和普通员工不一样，点两下就能对照。免登录关掉后这个菜单和 `POST /dev/identity` 一起消失（返回 404），cookie 也不会再让人登进来。

## 建部门、岗位、成员：用 `/setup` 引导页（最快）

**这是本机试用里建部门/岗位/成员最快的路径**，不用敲 bootstrap 和 loader。服务起来、以管理员身份登录（免登录时就是 `ADMIN001`）后打开：

```
http://127.0.0.1:8000/setup
```

页面按顺序引导三步：建部门 → 建岗位 → 建成员并分配岗位。点「建立成员并生成登录链接」会当场给出一次性登录链接，同时把该岗位的汇报权限一起授好（含 `workforce.reports/read` 与 `reports/submit`，所以建出来的号能直接打开填写页）。

和「准备一个部门」那节的差别：那边走种子数据（`DESIGNER` 岗位缺 `reports/read`，要按上文补），这边建号自动带全。两条路都能用，选一条就行。重复用同一个部门代号或岗位代号提交会被拒（页面提示已存在，`web.py:1774` / `:1795`），不会建出两份。

`/setup` 的入口在「设置」页左栏的「试用与开关」，也可以直接敲地址。

## 三级角色与两个页面的可见范围

只有三级，层级由 grants 推出来（`security.admin_level`），不看岗位名：

| 层级 | 判定 | 典型是谁 |
| --- | --- | --- |
| 系统管理员 | 持有 `workforce.grants/manage`（也就是能打开设置页的那条） | 全公司一名 |
| 部门管理员 | 持有任意一条 `workforce.<资源>/manage`（工作项 / 成员 / 界面 / 岗位 / 任岗 / 部门），但没有 `workforce.grants/manage` | 每个部门默认一名；系统管理员也可以单独授予某个员工 |
| 普通员工 | 其余所有人（只有 read / submit / review 这类只读或本人权限） | 大多数成员 |

「审页面」（`workforce.interfaces/review`）是**只读**权限，不会把人提到部门管理员。

两个页面对齐这套层级：

- **进度中心**：所有角色都能看到「公司总览」（全公司汇总数字）、自己所属部门的明细；管理员（系统管理员与部门管理员）能看到所有部门的明细。页面不写「你是 X，能看到 Y」这类话：看得到的部门就是答案，没显示的就是看不到。
- **汇报中心**：普通员工只看到自己提交的汇报（每个人取自己最新的一条，不会被同事更新的条目挤掉）；部门管理员看到自己负责的部门的汇报；系统管理员看到全部。

## 拿到登录链接

免登录开着时这一步可以跳过。需要真实会话时，另开一个终端：

```sh
# 同上：OAMS_DATABASE_URL 要在当前终端里有效
.venv/bin/python -m oams.cli issue-token --member-id <member_id>
```

命令会**直接打出登录地址**，把终端里那行「把这一行贴进浏览器就能登录：…」贴进浏览器就进去了（输出的 JSON 里也有 `login_url`），不用自己拼 `/login?token=`。服务不在 8000 上时加 `--base-url http://127.0.0.1:<你的端口>`；手上只有员工编号就用 `--employee-code DESIGN001` 代替 `--member-id`，两种写法签出的令牌等价。

**想一步落到部门界面**：在打出来的地址末尾自己补 `&next=/interfaces`。这一步是**可选**的，不加就先进首页再从顶部导航点过去。

## 四个页面

| 页面 | 地址 | 用途 |
|---|---|---|
| 部门界面 | /interfaces | 部门成员打开本部门界面填写 |
| 填写页 | /interfaces/<id> | 按描述渲染出的表单 |
| 界面制作 | /setup/interfaces | 粘贴 AI 产出的描述，校验并启用 |
| 汇报中心 | /interfaces/board | 管理层按部门查看最近汇报 |

这三个填写相关的页面都要求 `workforce.reports/read`：填写页要读汇总数据才知道自己填的进了哪个口径。**两条建号路径给的权限集不一样**：`/setup` 引导页建成员时会自动授予 `reports/read` 和 `reports/submit`，而「准备一个部门」那步用的种子数据里，普通设计师岗（`DESIGNER`）只有 `reports/submit`——照第 1 章建号，普通员工打开 `/interfaces`、`/departments/<id>`、`/interfaces/<id>` 会被直接带回进度中心，不给提示条。要让人能填，就把 `workforce.reports/read` 补到他的岗位上（种子数据缺这条，已按此口径补齐）。

## 停掉服务

- 停止网站：在运行 uvicorn 的终端按 Ctrl+C。
- 停止数据库：`docker stop <你的容器名>`，下次 `docker start <你的容器名>` 即可，数据保留。
- 完全重来：`docker rm -f <你的容器名>`，然后从"一次性准备"重新开始。

## 注意

- 登录链接等同临时密码，不要发到群里；有效期 8 小时（`SESSION_TTL_HOURS` 可改，`issue-token` 本身没有时长参数）。`SESSION_TTL_HOURS` 管的就是**登录会话多久过期**——签出来的链接和浏览器里的 cookie 一起到点失效；它不管请求超时，也不管任务时限，改完要重新签链接才生效。
- 这是本机试用环境，数据库只监听本机端口，不要暴露到公网。
- 现在的账号仍需管理员签发链接，账号中心尚未接入。
- 某个示例页面点开后被带回上一页，多半是库里那份描述还是旧版模块名（v0.1）：重跑 `scripts/seed-live-interface.py`，它会按当前版本重新启用这个页面。注意重跑后页面地址（`/interfaces/<id>`）会变，按新地址重开。
