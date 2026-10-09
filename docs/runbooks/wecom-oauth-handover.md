# 企业微信登录：需要你提供什么、怎么给我

状态：凭据未提供，OAuth 未开始实现。本文只解决"怎么把东西交给我"，不含代码。

## 0. 先确认这个前置条件，否则后面全白做

企业微信的 OAuth 回调地址（`redirect_uri`）**必须落在该自建应用配置的"可信域名"下**，而可信域名要求：

- 域名已完成 **ICP 备案**
- 域名**互联网可访问**，企微后台要下载校验文件放到域名根目录做归属验证（`http://域名/WW_verify_xxxx.txt` 要能打开）
- `redirect_uri` 的域名与可信域名**完全匹配**，不一致直接报 `redirect_uri 与配置的授权回调域名不一致`
- **localhost、127.0.0.1、内网 IP 都不行**，开发阶段也一样

如果 Workhub 只打算在内网跑、没有备案域名，那么只有两条路：

1. 拿一个已备案的外网域名，用反向代理（nginx / frp / 云厂商）把**只有 `/api/auth/wecom/callback` 这一个路径**转发到内网 Workhub。回调是浏览器跳转，只要这一跳能出公网即可，业务数据不必出网。
2. 放弃企微 OAuth，先用运维签发的受限令牌登录（CLI `issue-token` 已有），等有域名再接。

**先回答我：有没有可用的已备案域名？** 没有的话先别去后台建应用，建了也联调不通。

## 1. 选登录入口（影响我怎么写回调）

两条链路，回调可以复用同一个地址，但触发方式不同：

| 入口 | 场景 | 链接 | 特点 |
|---|---|---|---|
| A 扫码登录 | PC 浏览器打开网页，用企微 App 扫码 | `https://open.work.weixin.qq.com/wwopen/sso/qrConnect?appid=CORPID&agentid=AGENTID&redirect_uri=<UrlEncode>&state=<随机>` | 通用，不在企微客户端内也能登录 |
| B 客户端内免扫 | 在企微工作台打开应用 | `https://open.weixin.qq.com/connect/oauth2/authorize?appid=CORPID&redirect_uri=<UrlEncode>&response_type=code&scope=snsapi_base&state=<随机>&agentid=AGENTID#wechat_redirect` | 体验最好，静默拿到 userid，无感知 |

`scope` 只取 `snsapi_base`。**不要用 `snsapi_privateinfo`**——它会返回头像、二维码等敏感信息，需要用户手动授权，我们只要 `userid` 就够了，多拿就是多承担责任。

我的建议：**A 为主、B 顺带**，同一个 `/api/auth/wecom/callback` 收两边的 `code`。如果你只打算让员工在企微工作台里用，就只做 B，能少配一个二维码页面。

## 2. 需要你提供的清单

| # | 项 | 敏感 | 在哪拿 |
|---|---|---|---|
| 1 | 企业 CorpID | 否 | 管理后台 → 我的企业 → 企业信息 → 企业ID |
| 2 | 自建应用 AgentID | 否 | 管理后台 → 应用管理 → 自建应用 → 应用详情 |
| 3 | 自建应用 Secret | **是** | 同上，Secret 一栏（点"查看"会发短信到你手机） |
| 4 | 可信域名（已备案+已验证） | 否 | 你决定，例如 `workhub.公司域名.com` |
| 5 | 回调完整地址 | 否 | `https://<可信域名>/api/auth/wecom/callback` |
| 6 | 服务器公网出口 IP | 否 | 在服务器上访问 `https://myip.ipip.net` 或搜"我的IP"；多个出口就全给 |
| 7 | 应用可见范围 | 否 | 哪些部门/成员允许登录 Workhub |

第 6 项容易漏：**不配企业可信 IP，`gettoken` 会被拒**，表现为后台一切正常但服务端拿不到 token。
第 7 项决定"谁能登录"：不在可见范围内的成员扫码会提示无权限，这是我们希望的行为（别设成全公司，除非确实全员可用）。

可选第 8 项：如果需要 Workhub 显示姓名和部门（而不是只显示 `userid`），应用还要有**读取成员**的通讯录权限。只需要 userid 的话不必开。**建议先不开**，姓名部门由 HR 在 Workhub 里维护，避免企微通讯录成为事实上的组织数据源。

## 3. 怎么安全地把东西给我

Secret 一旦泄露，别人可以以你的应用身份调用企微接口。按这个顺序选：

**推荐：写进 `.env` 文件（已在 `.gitignore` 里，不会被提交）**

```
/Volumes/工作/aiworkspace/company-workhub/.env
```

内容照抄 `.env.example` 里新增的企微段，把值填上即可。我自己去读，不经过聊天记录。

**备选：放仓库外**

```
~/.workhub/wecom.env
```

然后告诉我路径。

**不要这样给我**：直接把 Secret 贴在聊天里、截图发给我、写进任何会被提交的文件（包括 `docs/` 和 `.env.example`）、贴到工单或群聊。

我可以接受 CorpID 和 AgentID 直接说——它们不是秘密。

## 4. 我会拿它做什么（边界）

拿到之后只做这些：

1. `GET /cgi-bin/gettoken?corpid=&corpsecret=` 取 access_token，服务端缓存，不频繁请求
2. `GET /cgi-bin/auth/getuserinfo?access_token=&code=` 把 `code` 换成 `userid`
3. 把 `provider='wecom'`、`tenant_id=CorpID`、`subject_id=userid` 写进 `member_external_identities`，签会话
4. 写审计事件

**不会做**：全量同步通讯录、拉取成员手机/邮箱、以应用身份发消息、修改企微后台任何配置、把 Secret 写进代码或日志。

联调我会用一次性 state 防 CSRF，code 只用一次（企微侧本来也是一次性的，5 分钟过期）。

## 5. 你那边的操作顺序

1. 确认有已备案域名（第 0 节）
2. 管理后台 → 应用管理 → 自建 → 创建应用（名字建议 `Company Workhub`）
3. 记下 AgentID 和 Secret
4. 应用详情 → 开发者接口 → **网页授权及 JS-SDK** → 设置可信域名 → 下载校验文件放到域名根目录 → 验证通过
5. 应用详情 → **企业微信授权登录** → 设置授权回调域 → 填同一个域名
6. 管理后台 → 我的企业 → 安全与管理 → **企业可信 IP** → 填第 6 项的出口 IP
7. 设置应用可见范围（第 7 项）
8. 把第 2 节清单交给我

## 6. 验收标准

联调通过的标志，缺一条都不算完：

- [ ] 用 A 链路扫码，能跳回回调地址并带上 `code`
- [ ] 用 B 链路在企微工作台打开，静默拿到 `code`
- [ ] 服务端能用 code 换到 `userid`
- [ ] 该 `userid` 在 Workhub 有身份映射时能登录，没有时明确拒绝（不自动建号）
- [ ] 成员离职或 `token_version` 递增后，旧会话立即失效
- [ ] 登录、失败、拒绝三次都写审计事件
