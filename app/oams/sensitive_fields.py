"""敏感字段登记表（`oams.sensitive_field_registry`）的种子与读取。

**这张表是「哪些字段算敏感」的唯一口径**，不是一份可选的说明。判据放在这里、由代码读取，
而不是散在接口里写 `if field == "salary"`：散着写，第一个人加字段时会记得，第三个人就忘了。

六个字段来自 `docs/architecture/v1-hr-department-scope.md` 第 1 节的矩阵——身份证、银行卡、
薪资、合同、绩效、入职材料。那节同时钉了两条与这里直接相关的规矩：

1. **人事敏感字段不随「管理层」自动放开**，要单独授权；
2. **管理员能看全部 ≠ 管理员该看薪资**——管理员是运维角色，技术权限不该自动换成业务字段。

所以第 2 条在这里落成一个具体后果：`requires_explicit_grant` 全部为真，**系统管理员的管理权
不能顶替敏感字段的读权**。

为什么种子放在代码里、由 `oams db init` 幂等写入，而不是写进 `schema.sql`：那份文件是
`pg_dump --schema-only` 的产物（文件头写着），只该有 DDL。数据一旦混进去，「改种子」和
「改结构」就再也分不开，重导 schema 时还会把别人改过的登记行冲掉。

幂等口径：`ON CONFLICT (field_key) DO NOTHING`。**不覆盖已有行**——label 与 description
可能被管理员按自己公司的说法改过，那是他们的口径，重跑 `db init` 不该改回去。
"""

from __future__ import annotations

from typing import Any

#: 承认的敏感字段。`field_key` 是稳定标识，`label` 是给人看的名字，
#: `description` 说清**这一格涵盖哪些内容**——它能挡住的错误是「只想到了身份证号、
#: 没想到证件影像」这类半覆盖。
SENSITIVE_FIELDS: tuple[dict[str, Any], ...] = (
    {
        "field_key": "identity_card",
        "label": "身份证",
        "description": "身份证号及证件影像",
    },
    {
        "field_key": "bank_account",
        "label": "银行卡",
        "description": "工资卡号与开户信息",
    },
    {
        "field_key": "salary",
        "label": "薪资",
        "description": "薪资结构、调薪记录与发放明细",
    },
    {
        "field_key": "contract",
        "label": "合同",
        "description": "劳动合同与附件",
    },
    {
        "field_key": "performance",
        "label": "绩效",
        "description": "考核结果与绩效面谈记录",
    },
    {
        "field_key": "onboarding_docs",
        "label": "入职材料",
        "description": "入职登记表、学历与背调材料",
    },
)

_REGISTERED_KEYS: tuple[str, ...] = tuple(row["field_key"] for row in SENSITIVE_FIELDS)


def seed_registry(conn) -> int:
    """把承认的敏感字段写进登记表，返回**这次新插入**的行数。

    重复执行返回 0：已经登记的行原样不动（见模块开头「幂等口径」）。
    """
    inserted = 0
    for row in SENSITIVE_FIELDS:
        result = conn.execute(
            """INSERT INTO oams.sensitive_field_registry
                   (field_key, label, description, requires_explicit_grant)
               VALUES (%s, %s, %s, true)
               ON CONFLICT (field_key) DO NOTHING
               RETURNING field_key""",
            (row["field_key"], row["label"], row["description"]),
        ).fetchone()
        if result is not None:
            inserted += 1
    return inserted


def declared_keys() -> tuple[str, ...]:
    """代码里承认的敏感字段名，顺序同 `SENSITIVE_FIELDS`（给校验与测试用，不查库）。"""
    return _REGISTERED_KEYS


def registry_rows(conn) -> dict[str, dict[str, Any]]:
    """登记表现在的内容，按 `field_key` 索引。

    读的是**库**而不是常量：管理员可以按自己公司的说法改 label，也可以把一条改成
    `requires_explicit_grant = false`（例如这家公司不把绩效当敏感）。判据必须跟着库走，
    否则改了库、代码还在按出厂设置判。
    """
    rows = conn.execute(
        """SELECT field_key, label, description, requires_explicit_grant
             FROM oams.sensitive_field_registry ORDER BY field_key"""
    ).fetchall()
    return {row["field_key"]: row for row in rows}
