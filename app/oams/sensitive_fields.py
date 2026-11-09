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


# ---------------------------------------------------------------------------
# 人员表的列分类：闸门靠它判断「这个字段能不能出去」
# ---------------------------------------------------------------------------

#: 人员表。脱敏闸门只认这一张表——成员返回的每个字段都从这里的分类过一遍。
PERSONNEL_TABLE = "workforce_members"

#: 列 → 分类。**每一列都必须在这里有一行**：加列的人躲不过去，用例拿
#: `information_schema` 双向核对（列没分类要红；分类里有已删的列也要红）。
#:
#: - `public`：不是个人信息，可以出现在返回里；
#: - `internal`：内部字段（会话作废计数、时间戳），**永不返回**，连打码都不出现；
#: - 其余：必须是一个**登记过的敏感键**（`sensitive_field_registry.field_key`）。
#:
#: 为什么由宿主分类、不由写页面的人声明：写页面的人是被闸的人。让他标「这一列敏感」，
#: 他一个都不标就能拿明文——等于把钥匙交给被锁的人。
PERSONNEL_COLUMNS: dict[str, str] = {
    "member_id": "public",
    "employee_code": "public",
    "display_name": "public",
    "employment_status": "public",
    "token_version": "internal",
    "joined_at": "public",
    "departed_at": "public",
    "created_at": "internal",
    "updated_at": "internal",
}

#: `GET /v1/members` 返回的字段。它回答的是「我读得着谁」，不含任何人事敏感项。
MEMBER_READ_FIELDS: tuple[str, ...] = (
    "member_id",
    "employee_code",
    "display_name",
    "employment_status",
    "joined_at",
    "departed_at",
)

_PUBLIC = "public"
_INTERNAL = "internal"

#: 打码后的样子。**不做「留前几位 / 后四位」的部分遮蔽**：薪资、身份证的后几位本身
#: 就是可识别信息，部分遮蔽给的是「看起来安全」的假象。
MASKED_VALUE = "***"


class UnclassifiedPersonnelColumn(RuntimeError):
    """人员表里有一列没分类——闸门不知道该不该放它出去，所以宁可报错也不返回。"""


def sensitive_keys_in(columns) -> frozenset[str]:
    """这些列里用到的敏感键（`public` / `internal` 不算）。列没分类直接抛错。"""
    keys: set[str] = set()
    for column in columns:
        classification = PERSONNEL_COLUMNS.get(column)
        if classification is None:
            raise UnclassifiedPersonnelColumn(column)
        if classification not in (_PUBLIC, _INTERNAL):
            keys.add(classification)
    return frozenset(keys)


def mask_value(value):
    """打码。`None` 保持 `None`——字段本来就没有值，打码不会多说明什么。"""
    return None if value is None else MASKED_VALUE


def project_member_row(row, *, readable=frozenset()) -> dict:
    """把查出来的一行按分类投影成可以返回的样子。

    这是**唯一的读取口**：`GET /v1/members` 的每一行都从这里出去，别处不再各写一遍
    「要不要打码」。规则只有三条：

    - 没分类的列 → 抛错（宁可 500，也不放一个没人判过的字段出去）；
    - `internal` → 不出现（查询多选了也不出去）；
    - 敏感键且不在 `readable` 里 → 打码。
    """
    out: dict = {}
    for column, value in dict(row).items():
        classification = PERSONNEL_COLUMNS.get(column)
        if classification is None:
            raise UnclassifiedPersonnelColumn(column)
        if classification == _INTERNAL:
            continue
        if classification != _PUBLIC and classification not in readable:
            out[column] = mask_value(value)
        else:
            out[column] = value
    return out
