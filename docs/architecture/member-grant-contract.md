# 成员授权（「成员与权限」屏）契约 v1 — 2026-11-05

这一屏只做一件事：**点名给某个人几个部门**，让他能在这几个部门里干活，但碰不到系统配置。
做完之后「超级管理员」不再只能靠命令行建。

## 一、落在哪一层

三层里只落在**授权层**（`oams.permission_grants`）。

| 层 | 这一屏做什么 | 不做什么 |
| --- | --- | --- |
| 授权层 `permission_grants` | **读和写都在这一层**：谁能管哪几个部门 | —— |
| 部门范围层 `security.accessible_org_unit_ids` | 只读，用来**解释**：这个人现在实际能进哪些部门 | 不在这屏改座位、不改岗位 |
| 页面权限层 `interface_access` | 不碰 | 页面级权限在 `/reviews/settings/<org_code>/<slug>/access` 那屏，已经做完了 |

**数据来源不是聚合接口。** 这一屏直接读授权表（连成员、岗位、部门名），因为要给人看的是
「这一行是什么、谁给的、什么时候到期、为什么给」——这些都是行级事实，聚合接口只会把它压成
一句话，改的时候还得反查。**写一律走 `api.insert_grant` / `api.revoke_grant`**：审计
（`grant.created` / `grant.revoked`）只有一处写，不要在新屏里各写一套 `INSERT`。

反过来，「这个人到底能进哪些部门」那一列**必须**调 `security.accessible_org_unit_ids` 算，
不许在模板里自己把座位和授权行加一遍——两套算法迟早会有一处算漏，界面上就会写着「他能进
财务部」而点进去 403。

## 二、这一屏能发哪一种授权

表上 `scope_type` 有五种（`self_position` / `org_unit` / `selected_positions` /
`company_aggregate` / `all`），但 `security.py` **真正能解析的只有 `org_unit` 和 `all`**
（`effective_grants` 的 `company_only` 与 `org_unit_id` 两条分支）。另外三种今天能写进库、
却永远不会生效——**这一屏不提供它们**，也不拿它们做下拉项。写进去一条不生效的授权，比报错更坏。

同样，`subject_type` 只有 `member` 和 `position` 两档，它们的期限规则不一样（见下）：

- **成员档**（`subject_type='member'`）：临时例外，**必须填到期时间**。
  表上 `permission_grants_check2` 与 `GrantCreate` 的校验器都禁止 `valid_until IS NULL`。
  这一屏就是发这一档：`到期自动失效，不用手动收`。
- **岗位档**（`subject_type='position'`）：长期安排，可以不设到期。它已经由
  `/setup` 那屏（建岗位、勾「部门负责人」）在发，**这一屏不发**。
  想给某人永久管某个部门，做法是给他那个部门的岗位——岗位就是长期授权，别再发明第二种永久授权。

## 三、发与收

### 发（一行一个部门）

勾住 N 个部门就写 N 行，**一页 × 一个部门一行**，落在 `(member_id, resource, action,
'org_unit', scope_org_unit_id)` 上。重复保存同一个部门是覆盖（`revoke` 旧的 + 写新的）还是
直接拒，二者选一，**选「拒并说清已有那一行」**：同一个人同一件事同一个部门出现两行有效授权，
到期时间不一样，谁也说不清哪行算数。

字段：

- 部门：多选，只列 `status='active'` 的部门；公司根节点不在这份名单里（见下一节）。
- 能力：`resource` / `action` 从固定几项里选，**不让人手打字符串**。起步两项即可：
  `workforce.reports/submit`（在这几个部门提交汇报、建页面）、`workforce.interfaces/review`
  （审这几个部门的页面）。要加项就加常量表，别在模板里散落字面量。
- 到期日：必填，默认 90 天后的同一天；界面写清「到期自动失效」。
- 原因：必填，进 `reason` 列，也进审计。这句话以后是判断「这条授权当初为什么给」的唯一依据。

### 收

`api.revoke_grant` 把 `revoked_at` 置为 `now()`，**不删行**。删行等于把审计里那条
`grant.created` 的指向对象也一起消灭了。

## 四、谁有权发（本屏在路由层复核，不改底层）

进得来这一屏：`workforce.grants/manage`。

**但底层这道门是宽的**：`require_permission` 调 `has_permission(...)` 时不传
`org_unit_id`，而 `effective_grants` 在没有范围参数时「全公司的 + 任何部门的」都算，
所以一个只持**窄授权**（比如只在财务部有 `workforce.grants:manage`）的人也能进来，
并且能发别的部门的授权。这是 `api.py` 里那份 `SCOPE WARNING` 注释说的同一件事。

所以本屏的 POST 自己复核一遍，两道：

1. 目标部门必须在该成员自己的 `accessible_org_unit_ids` 里；
2. `scope_type='all'`（全公司）**只许系统管理员**（`has_company_scope`）发。

越界的整笔拒绝，不落库、不留半截（照 `page_access_save` 的做法）。前端灰只是提示，不是权限。

## 五、界面口径

- 一屏两栏：上「给谁开、开哪几个部门」（表单），下「现在谁管着哪几个部门」（清单 + 收回）。
- 清单每行：成员名、能力、部门、来源（`被点名` / `岗位`）、到期日、原因、收回按钮。
  **`被点名` 与 `岗位` 要分开标**：前者是这一屏发的例外，后者是长期安排，收掉前者不改变后者。
- 空态一句话：「还没有点名授权。点上面的部门，就是让他能在这几个部门里干活。」
- 到期日已过或已收回的行不列在主表（列在「已失效」折叠里），否则主表天天在教人无视它。
