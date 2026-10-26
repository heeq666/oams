# Company Workhub

全公司工作进度与岗位协同系统，定位为可进化的全公司协同底座。各部门负责人借助 Skill 让 AI 使用公司统一的模块与素材制作本部门进度汇报界面；界面在规则约束下生成，数据回到统一进度中心，管理层据此跨部门把控业务。底座负责身份、岗位、任职、权限、审计、字段映射校验与公司汇总；规则层负责约束任何部门界面都可以自定义但必须映射到底座标准字段；未映射字段仅部门内可见并每日汇总为候选标准字段提案，经公司审核后升级为底座能力，实现自我进化。

## 项目边界

- 本项目负责员工身份映射、组织与岗位、任职历史、岗位工作配置、业务权限、工作项、审计和公司级进度看板。
- 后续由独立账号中心负责登录认证和账号生命周期；本系统负责账号主体映射、岗位归属、业务授权、数据范围和权限审计。
- 商品中心继续作为商品业务事实来源。本系统通过商品中心 API 获取经确认的只读进度数据，不直接读写商品中心数据库。
- 工作进度数据库与商品中心数据库逻辑隔离；初期可部署在同一台 Windows 局域网服务器和同一 PostgreSQL 服务上，但使用独立数据库、账号和备份策略。数据库端口不对公网开放。
- 各部门 Skill 后续负责解释统一规则、辅助表格映射和检查数据；实际认证、授权和数据写入由本系统服务端完成。

## 架构草图

```text
独立账号中心 ──> Company Workhub ──> 工作进度数据库（独立）
                       │
                       ├── 企业人员/岗位/任职/授权/审计
                       ├── 部门工作项与公司级看板
                       └── 只读 API ──> 商品中心 API（商品业务事实）
```

## 当前资料

- [部门插件宿主总体构想（现行主线）](docs/architecture/plugin-host-concept.md)
- [P0 接口契约（施工图：模块注册表 / 指标目录 / descriptor v0.2 / 渲染 / 取数）](docs/architecture/p0-interface-contract.md)
- [界面规则与模块规范（约束层）](docs/architecture/interface-rules-and-modules.md)
- [界面描述格式（JSON Schema 草案）](docs/architecture/interface-descriptor-schema.md)
- [部门界面使用说明](docs/runbooks/department-interface-guide.md)
- [部门页面：制作、试填、审核、启用](docs/runbooks/department-page-lifecycle.md)
- [在你电脑上跑起来（本地试用）](docs/runbooks/local-trial.md)
- [商品中心只读数据接入](docs/runbooks/product-hub-data-access.md)
- [商品中心 MCP 接入 DSH 经过（含踩坑记录）](docs/runbooks/product-hub-mcp-setup-journey.md)
- [身份、岗位与权限底座方案](docs/architecture/workforce-identity-position-permission-plan.md)
- [身份、岗位与权限底座首期实现契约](docs/architecture/workforce-identity-position-permission-implementation.md)
- [Frappe HR / ERPNext 适配评估](docs/architecture/frappe-erpnext-adaptation-assessment.md)
- [PostgreSQL 初始迁移](db/migrations/0001_workforce_identity_position_permission.sql)
- [Windows 服务器部署准备手册](docs/runbooks/windows-server-deployment.md)
- [独立账号中心接入契约（待定）](docs/runbooks/account-center-integration.md)

## 建设顺序

1. 评审岗位目录权责、一人多岗/代理、换岗交接和人事数据分级规则。
2. 与独立账号中心确认认证协议、稳定主体 ID、账号停用同步和会话撤销边界。
3. 设计并实现身份、岗位、任职、配置版本、权限和审计底座。
4. 以招聘为首个部门试点，验证换人、调岗、代理到期、新增岗位和数据权限。
5. 接入商品中心只读进度，再推广到其他部门并完善管理看板和 Skill。


## 当前可运行后端

要求 Python 3.11+。复制 `.env.example` 为 `.env`。先通过受控配置机制将 `WORKHUB_DATABASE_URL` 指向迁移账号运行 `workhub migrate`；随后切换到运行时账号，再创建管理员并启动服务：

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e .
workhub bootstrap-admin --name "系统管理员" --employee-code "ADMIN001" --org-code "COMPANY" --org-name "公司"
workhub serve
```

独立账号中心接入前，应通过受控运维通道使用 `workhub issue-token --member-id ...` 签发短时令牌。当前 MVP 的工作项列表、汇报历史、部门看板、进度中心和汇报提交已按有效岗位任职关系限制部门/岗位范围；公司总览（全公司汇总）、自己所属部门的明细对所有角色可见，部门明细的可见范围按三级（系统管理员 / 部门管理员 / 普通员工）决定，汇报中心按「自己的 / 本部门的 / 全部」分档。其他 workforce 管理资源仍沿用 `all` 权限语义，不应据此向普通用户开放。真实部门试用可直接用管理员的「试用设置」页建部门、岗位和成员，并生成一次性登录链接；详细说明见 [MVP 小范围试用说明](docs/runbooks/mvp-pilot-guide.md)。

设计开发阶段可以在本机打开免登录，省掉反复签令牌：`WORKHUB_DEV_AUTOLOGIN=1`（可写工号认人，页面右下角会常驻「开发模式 · 免登录」角标，点开即可在系统管理员 / 部门管理员 / 普通员工之间切换身份看同一页）。`scripts/start-local-trial.sh` 默认打开它，`WORKHUB_DEV_AUTOLOGIN=0` 即恢复真实登录；它只决定身份，权限、数据范围和审计不变。部署到任何共享环境前必须去掉。

## 数据库部署原则

生产数据库计划安装在现有 Windows 局域网服务器上的 PostgreSQL 服务中，但必须创建独立数据库和专用最小权限账号。已确认可通过 SSH 别名 `win` 只读连接到服务器，PostgreSQL 16 服务运行于 `D:\pgdata`，当前监听端口为 `5433`（本机 TCP 探测成功）。实际数据库清单、账号权限、备份/恢复能力和远程访问范围尚未核验。账号中心的认证回调与 Workhub 服务通过受控 HTTPS 接口对接；PostgreSQL 不直接暴露给客户端或公网。

当前已有 FastAPI 后端、身份/岗位/任职/授权/审计数据库迁移、管理员引导 CLI、部门进度汇报页、按有效任职隔离的工作项/看板/汇报读取、公司进度中心和 Windows 部署准备手册。服务器上的数据库尚未创建或修改；独立账号中心接入、其他 workforce 管理资源的细粒度范围、招聘专用界面和商品中心只读 API 仍待实施。
