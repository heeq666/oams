# OAMS

> OAMS = Organizational App Management System，中文「组织应用中台」。作者：[@heeq666](https://github.com/heeq666)

## OAMS 是什么

OAMS 是组织内 AI 应用的**宿主与应用商店**。应用由各部门自己用 AI 写，但跑在同一套底座上：身份、岗位、任职、权限、数据范围、字段契约、上架审核、公司汇总都由底座统一提供。应用要过审核才能上线，上线后谁能看什么、什么能进公司汇总，也由底座裁决——**自由在形式，不自由在语义**。

技术上是一个 Python + FastAPI + PostgreSQL 的服务端渲染应用，**没有前端构建链**（不需要 npm / 打包步骤），装好就能跑。

许可采用双授权：**AGPL-3.0 或商业授权**，二选一，详见[许可与署名](#许可与署名)。

## 一条命令跑起来

需要 Python 3.11+ 和一个 PostgreSQL 数据库。机器上的 `python3` 版本不够时（macOS 自带的是 3.9）：`brew install python@3.11`，或者按下面第 1 步里的 uv 那条路来。

```sh
# 0. 先有一个库 —— db init 只建表，不替你建库
docker run -d --name oams-pg \
  -e POSTGRES_USER='<账号>' -e POSTGRES_PASSWORD='<你的口令>' -e POSTGRES_DB='<库名>' \
  -p 127.0.0.1:<你的端口>:5432 postgres:16-alpine
# 不设 POSTGRES_USER 时账号是 postgres —— 那下面连接串里的 <账号> 就写 postgres
# 机器上已经装好 PostgreSQL 的，只建库也行：createdb '<库名>'
# 等它起来（约 2–3 秒）再往下跑；立刻连会报 connection refused
```

装依赖两条路，按机器上的 Python 挑一条。

```sh
# 1a. 机器上有 3.11+ 的 python：venv + pip
python -m venv .venv
. .venv/bin/activate
pip install -e .
```

```sh
# 1b. 没有 3.11+，改用 uv 管 Python：uv venv + uv pip
uv venv --python 3.12 .venv
. .venv/bin/activate
uv pip install -e .          # uv 建的 venv 没有 pip，装依赖就用 uv pip
```

```sh
# 2. 配库地址 —— 两种写法二选一
#   A) 写进 .env（之后每条命令都读它）
cp .env.example .env
#      再把 .env 里这一行改成你自己的库：
#      OAMS_DATABASE_URL=postgresql://<账号>:<口令>@127.0.0.1:<你的端口>/<库名>
#   B) 不碰 .env，只在当前终端临时给（换个终端要重给一遍）
export OAMS_DATABASE_URL="postgresql://<账号>:<口令>@127.0.0.1:<你的端口>/<库名>"
# 口令别提交进仓库：.env 已在 .gitignore 里

# 3. 建表、建管理员、起服务
oams db init                  # 建表：把库结构建出来
oams bootstrap-admin --name "系统管理员" --employee-code "ADMIN001" --org-code "COMPANY" --org-name "公司"
oams serve                    # 默认 http://127.0.0.1:8000；端口被占就设 OAMS_BIND_PORT=<你的端口>
```

起来后打开 <http://127.0.0.1:8000>，会先停在登录页。进去有两条路：

```sh
export OAMS_DEV_AUTOLOGIN=1   # 本机试用：免登录，右下角出现身份切换角标；部署到共享环境前必须去掉

oams issue-token --member-id <bootstrap 打印的 member_id>   # 真实会话：签一张令牌
# 命令会直接打出能点开的登录地址（终端一行提示 + JSON 里的 login_url），贴进浏览器就进
# 服务不在 8000 上时加 --base-url http://127.0.0.1:<你的端口>，或先 export OAMS_BASE_URL
```

命令是 `oams`。完整步骤与常见报错见[在你电脑上跑起来（本地试用）](docs/runbooks/local-trial.md)。

## 建第一页

前提：**先有一个部门，以及那个部门里能签字的负责人**——在 `/setup` 建成员时勾上「部门负责人」，这个人才有权一审；没人能签，下面这一步会卡死。

然后三步：**建部门、岗位、成员** → **写一个页面**（一段 JSON 描述）→ **提交审核，过一审后上线**。

照[建一页、过一审（一页纸）](docs/runbooks/first-page-trial.md)走一遍即可，里面有可直接粘贴的 JSON 和三张截图。页面从草稿到上线要过部门初审；要对外公开或进公司汇总的，再过一道公司终审。

## 开发档案

方案、契约与流程手册，按需要读，不必从头看：

- [部门插件宿主总体构想](docs/architecture/plugin-host-concept.md) —— 现行主线：宿主与应用商店怎么分层
- [P0 接口契约](docs/architecture/p0-interface-contract.md) —— 施工图：模块注册表、指标目录、descriptor、渲染、取数
- [界面规则与模块规范](docs/architecture/interface-rules-and-modules.md) —— 约束层：三视图口径、模块白名单
- [界面描述格式（JSON Schema）](docs/architecture/interface-descriptor-schema.md) —— 应用描述文件长什么样
- [部门页面：制作、试填、审核、启用](docs/runbooks/department-page-lifecycle.md) —— 一页的完整生命周期
- [部门界面使用说明](docs/runbooks/department-interface-guide.md) —— 给部门写应用的人看
- [身份、岗位与权限底座方案](docs/architecture/workforce-identity-position-permission-plan.md) —— 底座为什么这么设计
- [身份、岗位与权限底座首期实现契约](docs/architecture/workforce-identity-position-permission-implementation.md) —— 对应的实现口径
- [独立账号中心接入契约（草案，尚未实现）](docs/runbooks/account-center-integration.md) —— 登录认证交给外部账号中心
- [架构与决策](docs/architecture/) —— 契约、方案与选型记录（含当时为什么这么定）

## 许可与署名

- **作者**：[@heeq666](https://github.com/heeq666)。项目自 2026 年 10 月起由作者发起并持续开发。
- **许可**：**GNU AGPL-3.0** 或**商业授权**，二选一。
  - AGPL-3.0：免费，**商用也免费**；但如果你改了它并让别人通过网络使用这个修改版，你要以 AGPL-3.0 公开修改后的源码。
  - 商业授权：想做成闭源产品售卖、或不接受 AGPL 传染的，向作者购买商业授权即可闭源。联系方式见 [LICENSING.md](LICENSING.md)。
- 许可证全文：[LICENSE](LICENSE)（AGPL-3.0 官方原文）
- 授权说明：[LICENSING.md](LICENSING.md)
- 贡献者协议：[CLA.md](CLA.md)——贡献者保留自己的版权，同时授权作者把贡献用于商业授权
- 第三方组件与许可证：[THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md)
- 项目主页：`oams.dev`（尚未上线）
