# 手册索引（`docs/runbooks/`）

这里放**照着做就能跑通**的操作手册，不解释设计、不讨论取舍。设计口径在 `docs/architecture/`，界面规范在 `docs/design/`。

命名：`<动词>-<对象>.md`，中文优先。新加一份就补一行到这里。

| 手册 | 什么时候看 | 大概多久 |
|---|---|---|
| [local-trial.md](local-trial.md) | 在本机把整套系统跑起来：装依赖、两个库、迁移、建管理员、起服务、拿 token | 30 分钟 |
| [first-page-trial.md](first-page-trial.md) | 第一次给真人试用：部门管理员建一页、部门负责人初审通过 | 15 分钟 |
| [mvp-pilot-guide.md](mvp-pilot-guide.md) | 试点期间的日常：建人、发权限、改状态、看汇报 | 20 分钟 |
| [department-page-lifecycle.md](department-page-lifecycle.md) | 部门页面的生命周期：建、改、提交、初审、公司终审、撤回 | 20 分钟 |
| [department-interface-guide.md](department-interface-guide.md) | 写部门界面 descriptor（模块怎么挑、字段怎么映射） | 40 分钟 |
| [account-center-integration.md](account-center-integration.md) | 接公司账号中心：身份、岗位、权限怎么对上 | 30 分钟 |
| [product-hub-data-access.md](product-hub-data-access.md) | 取商品数据中心的数（只读 MCP），地址与凭据怎么配 | 20 分钟 |
| [product-hub-mcp-setup-journey.md](product-hub-mcp-setup-journey.md) | 从头接一次商品中心 MCP，含踩坑记录 | 40 分钟 |
| [windows-server-deployment.md](windows-server-deployment.md) | 部署到 Windows 工作站 | 60 分钟 |
| [opensource-publish.md](opensource-publish.md) | 把私有仓变成开源仓：冻结、重写历史、验、推私有仓、换主仓 | 90 分钟 |

## 三条通用提醒

- **数字当场取**：手册里出现的提交数、版本号、日期都是取样时的值，照着跑之前先自己取一遍。
- **库和端口要标明**：涉及数据库的手册会写清是哪一套（演示站 55460 / 本机开发库 55433），结论要带上端口才有意义。
- **手册过期比没手册更坏**：发现和代码对不上，改手册或报给文档负责人，别在旁边加注释凑。
