# Frappe HR / ERPNext 适配评估

评估日期：2026-10-09  
结论状态：完成 MariaDB 隔离安装与 Employee Transfer 上游测试；PostgreSQL 16 下 HRMS 安装失败。

## 隔离原型验证记录（2026-10-09）

在本机 Apple Silicon Docker Desktop 上使用官方 ERPNext v16.50.0 镜像和 `frappe_docker` PWD Compose，分别创建隔离 MariaDB 11.8.9 与 PostgreSQL 16.14 站点。固定 Frappe/ERPNext/HRMS v16.50.0，HRMS 发布提交为 `7c037698`。只用虚构空白站点，不连接 Windows 服务器、不导入真实员工数据。

- **MariaDB 路径通过安装门槛。** HRMS 前端依赖、应用迁移及 after-install 补丁全部完成。HRMS 官方 `hrms.hr.doctype.employee_transfer.test_employee_transfer` 的 4 个测试全部通过，覆盖调岗历史、撤销恢复、新员工编号和未来转岗日期拒绝。
- 已从安装后的 DocType 元数据确认：Employee 有当前 Department、Designation、Reports To 和 `internal_work_history`；任职历史子表记录 Department/Designation 的 `from_date`、`to_date`；Employee Transfer 用可提交/撤销单据和 `current/new/fieldname` 明细记录人员属性变更。Designation、Department、User Permission、Task、Project 的元数据也存在。
- **PostgreSQL 16 路径未通过 HRMS 安装。** PostgreSQL 16.14 站点和 ERPNext v16.50.0 均安装成功，Frappe 明确提示 PostgreSQL 支持仍属实验状态。安装 HRMS v16.50.0 时，after-install 补丁 `update_employee_advance_status` 生成条件 `AND "return_amount"`；PostgreSQL 报 `DatatypeMismatch`，因为数值列不能直接作为布尔条件。HRMS issue [#5033](https://github.com/frappe/hrms/issues/5033) 报告了同一补丁、同一 SQL 错误；本次使用固定版本实测复现。HRMS 未能完成安装，后续场景不能在此 PostgreSQL 站点运行。
- 岗位权限只完成元数据检查，没有跑行级越权/API 场景。原生 Employee 是单一当前 Department/Designation 加工作历史；没有验证到稳定 Position ID、同岗多人任职、兼岗/代理及到期撤权的标准模型，这些仍需自定义应用。Task/Project 仅核对了字段结构，未做真实任务流或 REST 验收。
- 本机最初 Docker PoC 曾因可用磁盘空间不足使 Docker Desktop 停机；恢复后在本轮末尾按两个 Compose 项目标记删除剩余的 6 个常驻容器、8 个卷、2 个网络及本轮新增镜像标签。未执行全局 prune；原有 4.39 GB Docker 卷与预装 PostgreSQL 16 镜像保留。Docker 引擎已恢复，Windows 服务器及其 PostgreSQL 服务未修改。

### 补充运行记录（同日第二批，独立容器组）

以独立 Compose 项目 `workhub-poc`（frontend 绑定本机 `18080`）重跑 MariaDB 路径，版本同为 frappe/erpnext/hrms 16.50.0、HRMS `version-16` 提交 `7c037698`。结果与上文一致，形成交叉验证：`create-site` 退出 0，`install-app hrms` 干净完成，未修改上游代码。

补充的实测点：

- **DocType 名检索**：含 `position`、`acting`、`delegate`、`tenure` 的 DocType **均不存在**；含 `assignment` 的只有 Shift / Salary Structure / Leave Policy / Holiday List / Assignment Rule，属排班与薪资，不是岗位任职；含 `designation` 的只有 Designation 与 Designation Skill。
- **Employee 字段（共 109 个）**：岗位相关只有 `designation` 一个字段，代理/委派类字段为空。Employee Transfer 的字段为 `employee`、`company`、`department`、`new_company`、`transfer_date`、`new_employee_id`、`create_new_employee_id`、`reallocate_leaves`、`transfer_details`，**不含 designation**，属性变更由 `transfer_details` 明细承载。
- **同岗位名称多人**：HR-EMP-00001 与 HR-EMP-00002 挂同一 Designation「招聘专员」、分属不同部门，REST 按 designation 过滤返回 2 条。即"同名岗位多人"可表达，但 Designation 是名称字典而非岗位实例，没有岗位 ID、任期与交接记录。
- **REST 建数据链路已通**：Company / Department / Designation / Employee 均可经 REST 创建并查询。

操作要点（本机环境）：

- `docker.io` 与 `auth.docker.io` 直连不通，镜像经 `m.daocloud.io` 拉取后重新 tag；GitHub 直连不通，`frappe_docker` 与 `hrms` 源码经 `gh-proxy.com` 获取。
- HRMS 若手动拷入 `apps/` 再 `pip install -e`，需先装 `flit_core`；装入后**必须重启 backend/frontend/worker 进程**，否则 REST 报 500（`ModuleNotFoundError: hrms`）。
- 建 Company 前须先存在 Warehouse Type `Transit`；Employee 强制 `gender` 与 `date_of_birth`，而 Gender 初始为空需自建；Department 的 name 带公司后缀（如「招聘部 - POC」），引用须用完整名。

清理该组容器与卷：

```sh
docker compose -p workhub-poc down -v
```

MariaDB 可作为技术上可运行的备选，但 PostgreSQL 16 是当前既定目标，且 HRMS 官方安装路径在该版本组合上可复现失败；因此本次 PoC 未通过现有数据库架构的适配门槛。

## 结论

Frappe HR + ERPNext 在 MariaDB 上能安装，Employee Transfer 的上游测试通过，员工目录、组织、调岗历史和通用任务可复用。但它不能按目前的 PostgreSQL 16 目标干净安装 HRMS；同时稳定岗位身份、多人兼岗/代理任职、岗位配置版本和按岗位授权仍需自定义。

数据库是明确的适配阻断：ERPNext 官方部署说明要求 MariaDB；本次 PostgreSQL 16.14 实测在 HRMS after-install 补丁失败，并与 HRMS issue #5033 一致。不能把 PostgreSQL 作为此组合的可用方案，也不应绕过补丁后把当前未完成的站点当作可部署结果。

本轮没有在服务器安装或改动任何东西。Workhub FastAPI 底座和迁移仍只在本地仓库、尚未部署；若继续使用既定 PostgreSQL 16，应沿 Workhub 自有底座方向推进。

## 功能映射

| 需求 | Frappe HR / ERPNext 现成能力 | 评估 |
|---|---|---|
| 员工目录、在职状态、组织、Designation、汇报关系 | MariaDB 上的 Employee Transfer 上游测试通过；`internal_work_history` 有部门/Designation 起止日期 | 基础人事目录和单条任职变更可复用；需要岗位身份与并行任职模型时仍要定制 |
| 部门/角色访问控制 | Frappe 有角色权限、User Permissions 和文档级访问限制 | 可复用权限框架；岗位范围与“员工任职岗位”的授权联动仍需实现 |
| 部门工作任务 | ERPNext Task 支持状态、进度、模板、依赖、负责人/多人分配和 Owner Department | 适合作为通用任务试点；公司级跨部门看板的组织口径需配置验证 |
| REST API | Frappe 自动提供 DocType REST API 与自定义 RPC | 可用于外部系统集成；商品中心只读适配仍需编写并验证 |
| 独立账号中心身份 | Frappe 未提供与后续账号中心的既定集成 | 需按账号中心契约单独编写认证适配层 |
| 稳定岗位 ID、多人兼岗、代理到期、岗位交接、职责配置版本 | 未找到可直接满足的标准能力 | 预计需要自定义 DocType / 应用，这是 Workhub 的核心差异层 |

## 目标服务器适配

通过 SSH 对 Windows 服务器做了只读检查：Windows 11 Pro、约 32 GiB 内存、Docker Desktop 29.6.2 客户端已安装；Docker Linux engine 当前未运行，WSL 清单只有 `docker-desktop`，没有 Ubuntu 等用户发行版。故本轮未能在服务器上创建隔离容器或跑安装验证。

Frappe 官方安装说明支持 Debian/Ubuntu，Windows 使用者可通过 WSL；其运行栈还包括 Python、Node.js、Redis/Valkey、Yarn 等。基于当前机器状态，不能直接在 Windows 上原生部署，需要评估启用/配置 WSL Linux 或 Docker Linux engine 的运维成本。

## PostgreSQL 风险

- Frappe Framework 的配置和代码库支持 PostgreSQL 后端。
- 当前 ERPNext Helm 部署文档明确写着 ERPNext 需要 MariaDB；虽然 chart 有 PostgreSQL 选项，不能据此推断 ERPNext + HRMS 组合获得完整支持。
- HRMS issue [#5033](https://github.com/frappe/hrms/issues/5033) 和本次 v16.50.0 + PostgreSQL 16.14 实测均显示 `update_employee_advance_status` 将数值字段用于布尔条件，导致 HRMS after-install 失败。
- HRMS issue [#4408](https://github.com/frappe/hrms/issues/4408) 还记录了另一处 PostgreSQL 安装补丁问题，说明风险不只一条 SQL。

## 尚未完成的验证

- 行级 User Permission 的服务端过滤与 REST API 越权拒绝。
- 多人兼岗、限期代理/到期回收、稳定岗位 ID 和岗位配置版本。
- ERPNext Task 的负责人/多人分配、部门范围和跨部门 Project 汇总。
- 独立账号中心身份映射、商品中心只读 API，以及目标 Windows 主机部署、备份/恢复和重启恢复。

## 当前建议

若保持现有 PostgreSQL 16 规划，不建议把 Frappe HR/ERPNext 作为当前底座：HRMS 官方安装已在该组合上失败。只有在接受独立 MariaDB 及相应备份/运维成本时，才值得继续评估 MariaDB 路线；在这之前不要迁移真实数据或调整服务器数据库。若不接受 MariaDB，应继续 Workhub 自有 PostgreSQL 实现，复用本次确认可行的业务概念，而不是将 Frappe 误认为可直接替代底座。

## 参考资料

- [Frappe HR Employee](https://docs.frappe.io/hr/employee)
- [Frappe HR Employee Transfer](https://docs.frappe.io/hr/employee-transfer)
- [ERPNext Tasks](https://docs.frappe.io/erpnext/tasks)
- [ERPNext Project](https://docs.frappe.io/erpnext/project)
- [Frappe Users and Permissions](https://docs.frappe.io/framework/user/en/basics/users-and-permissions)
- [Frappe REST API](https://docs.frappe.io/framework/user/en/api/rest)
- [Frappe installation requirements](https://docs.frappe.io/framework/user/en/installation)
- [ERPNext Helm database requirements](https://github.com/frappe/helm/blob/main/erpnext/README.md#database)
- [frappe/hrms PostgreSQL install issue #5033](https://github.com/frappe/hrms/issues/5033)
- [frappe/hrms PostgreSQL installation issue #4408](https://github.com/frappe/hrms/issues/4408)
- [Frappe HRMS license](https://github.com/frappe/hrms/blob/develop/package.json)
