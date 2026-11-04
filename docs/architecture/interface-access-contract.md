# 页面权限（谁能看 / 谁能填）契约 v1 — 2026-11-04

页面设置里要能改「谁能看、谁能填」。今天这两件事**都不是页面自己的属性**，而是从
「这个人能不能进这个部门」推出来的（`security.can_access_org_unit` 与
`web._resolve_department`）。本契约把页面权限提升为**页面自己的属性**，并保证：
**新的授权只放宽、不收紧**——存量页面与存量账号的行为一个字不变。

## 一、一句话模型

- 页面属于一个部门，并有稳定地址 `(owner_org_unit_id, slug)`（`/pages/{org_code}/{slug}`）。
  版本行（`interfaces.interface_id`）每发布一次就换一个，**权限绝不能挂在版本行上**，
  否则改个标题再送审，权限就清零了。
- **本部门永远能看、能填**，不写进表。
- 别的部门要看/要填，靠 `oams.interface_access` 里的一行；没行 = 看不到、填不了
  （与今天一致）。
- 「全公司」不是新概念，就是**给公司根节点（`org_units.unit_type='company'`）发一行**：
  它在这棵树的顶端，所有人都落在它下面。

## 二、表

```sql
CREATE TABLE IF NOT EXISTS oams.interface_access (
    owner_org_unit_id uuid NOT NULL,
    slug      text NOT NULL,
    org_unit_id uuid NOT NULL,
    can_read  boolean DEFAULT true  NOT NULL,
    can_write boolean DEFAULT false NOT NULL,
    granted_by uuid,
    granted_at timestamp with time zone DEFAULT now() NOT NULL,
    note text,
    PRIMARY KEY (owner_org_unit_id, slug, org_unit_id),
    CONSTRAINT interface_access_write_implies_read CHECK ((NOT can_write) OR can_read)
);
CREATE INDEX IF NOT EXISTS interface_access_unit_idx ON oams.interface_access (org_unit_id);
```

- 主键三段：**一页 × 一个部门一行**，重复保存是覆盖不是追加。
- 不指向 `interfaces.interface_id` —— 理由见上（版本行会换）。
- `db init` 幂等：`CREATE ... IF NOT EXISTS` 直接进 `schema.sql`，不写迁移脚本。

## 三、语义（解析）

设页面属于部门 `O`（slug 为 `s`），看的人是 `M`，`seat(M)` = `active_org_unit_ids(M)`
（岗位任职，不含被授权代管），`chain(D)` = `D` 及其所有祖先（走到根）。

- **能看** `can_view_interface` = `can_access_org_unit(M, O)`（部门内 + 公司级，今天的
  行为）**或** 存在一行满足 `row.org_unit_id ∈ ⋃chain(d), d ∈ seat(M)` 且 `row.can_read`。
- **能填** `can_fill_interface` = `can_access_org_unit(M, O)`
  **或** 存在一行满足同上且 `row.can_write`。
- `can_access_org_unit` 仍是**部门级**判据（能不能进这个部门、能不能在这个部门建页），
  页面权限**不改它**；页面级判据只回答「这一页能不能打开 / 能不能提条目」。
- 部门内的人不会因为没行而失去权限——`can_access_org_unit` 那一条先过（这是「只放宽」的
  保证，也是存量 905 条用例不该红一条的原因）。

## 四、判据函数（`app/oams/security.py`，后端写区）

```python
def interface_audience(conn, owner_org_unit_id, slug) -> list[dict]
    # [{org_unit_id, org_code, name, unit_type, can_read, can_write, granted_at}]

def can_view_interface(conn, member_id, owner_org_unit_id, slug) -> bool
def can_fill_interface(conn, member_id, owner_org_unit_id, slug) -> bool

def set_interface_audience(conn, owner_org_unit_id, slug, rows, *, granted_by, note=None) -> None
    # rows = [{"org_unit_id": UUID, "can_read": bool, "can_write": bool}]，整体覆盖
    #   先删本页所有行，再插 can_read 为真的行（can_read 与 can_write 都假的行不入库）
def grantable_org_unit_ids(conn, member_id) -> set[UUID] | None
    # None = 公司级（可授权给任何在用部门，含公司根）
    # set  = 只能授权给这些部门（含自己座位部门；**不含**公司根）
```

实现要求：

1. 只有 `status='active'` 的部门可被授权；公司根必须允许（它就是「全公司」）。
2. `chain()` 用一条递归 CTE 向上走（`parent_org_unit_id`），不要在 Python 里循环查库
   （页面打开是热路径）。
3. `can_view_interface` / `can_fill_interface` 查不到页面的行时返回 `False`，不抛异常。
4. 本契约的函数**只动 `security.py` 与 `schema.sql`**，不碰 `web.py`、不碰模板。

## 五、谁能改、什么该灰（界面规则，我实现）

- 能改这一页权限的人 = **能审这一页的人**（`_review_powers` + `_review_refusal` 已有这套
  判据）：系统管理员、该部门的部门管理员。**审核是签字动作，权限是运营动作**，两条审计
  分开记，不复用同一条事件。
- 灰态规则（服务端给数据，界面照画）：
  - 「全公司」只有公司级（`grantable_org_unit_ids` 返回 `None`）能选；否则灰，提示
    「只有系统管理员能把页面开给全公司」。
  - 某个部门的勾选框：不在可授权集合里的灰，提示「你没有 XX 部的管理范围」。
  - 本部门那一行永远显示「本部门 · 默认能看能填」，不可取消。
- **服务端保存时按人复核**：`rows` 里出现不在可授权集合里的部门 → 403 中文一句话，
  不落库。前端灰只是提示，不是权限。

## 六、审计

- 保存成功写一条 `interface.access_changed`，`metadata = {"owner_org_unit_id", "slug",
  "audience": [{"org_code","can_read","can_write"}], "before": [...]}`，`reason` 用中文一句话。
- **不改** `interfaces.visibility`：那是「别的部门能不能当模板复制」，与本表两码事。

## 七、这一版不做

- 不做按人授权（只按部门；人靠岗位进部门）。
- 不做部门组 / 区域档（需要时给一个部门一次性授权即可）。
- 不做字段级填写权限。
- 不做跨部门审核：审核权限仍按 `_review_powers`，与本表无关。
