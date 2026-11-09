"""敏感字段登记表：`oams db init` 之后这张表就得是能用的。

脱敏的整条链路（哪些字段算敏感 → 谁能读 → 读时打码、逐条记审计）都从这张表出发，
所以它不能是空的。这张表在 `schema.sql` 里建了，可**建表不等于有数据**——2026-11-09
查过一次：表在、行数 0、代码里 `grep sensitive_field` 也是 0 处，等于一条从没通电的链路。

这里的用例只钉「种子」这一层，而且**走文档上的那条命令**（`oams db init`）在一个独立新库上
验，不是在本机那个「碰巧被人灌过」的开发库上验——那正是上一轮读权收敛踩过的坑：
用例造什么数据就只验什么数据。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import conftest as suite  # noqa: E402  pytest 会把 tests/ 放进 sys.path

pytestmark = pytest.mark.db

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from oams.sensitive_fields import SENSITIVE_FIELDS, declared_keys  # noqa: E402


@pytest.fixture
def fresh(db_ready):
    """一条用例一个刚建出来的空库——`db init` 之外什么都没做过。

    不给 module 作用域：`test_db_init_is_idempotent_and_does_not_overwrite` 会故意把
    `identity_card` 的 label 改掉，共用一个库会让后面的用例看到别人改过的状态，
    那种红是测试自己串味，不是代码有问题。
    """

    name, url = suite._create_database(db_ready)
    try:
        yield url
    finally:
        suite._drop_database(db_ready, name)


def _rows(url: str) -> dict[str, dict]:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
        rows = conn.execute(
            """SELECT field_key, label, description, requires_explicit_grant
                 FROM oams.sensitive_field_registry"""
        ).fetchall()
    return {row["field_key"]: row for row in rows}


def test_db_init_seeds_the_registry(fresh):
    """建完表就得有六条登记——空表等于脱敏链路没通电。"""

    suite._cli(["db", "init"], fresh)

    rows = _rows(fresh)
    assert set(rows) == set(declared_keys()), (
        f"登记表应有 {sorted(declared_keys())}，实际 {sorted(rows)}"
    )
    for field_key in declared_keys():
        row = rows[field_key]
        assert row["label"], f"{field_key} 缺 label"
        assert row["description"], f"{field_key} 缺 description——它要能挡住「只想到身份证号、没想到证件影像」这种半覆盖"
        assert row["requires_explicit_grant"] is True, (
            f"{field_key} 必须要求单独授权：人事敏感字段不随「管理层」自动放开"
        )


def test_db_init_is_idempotent_and_does_not_overwrite(fresh):
    """重跑 `db init` 不加行、也不把管理员改过的说法改回出厂设置。"""

    suite._cli(["db", "init"], fresh)

    import psycopg

    with psycopg.connect(fresh, autocommit=True) as conn:
        conn.execute(
            "UPDATE oams.sensitive_field_registry SET label = '身份证件' WHERE field_key = 'identity_card'"
        )

    suite._cli(["db", "init"], fresh)

    rows = _rows(fresh)
    assert len(rows) == len(declared_keys()), "重跑 db init 不该多出登记行"
    assert rows["identity_card"]["label"] == "身份证件", (
        "重跑 db init 把管理员改过的 label 冲回了出厂值——那等于每次建库都覆盖别人的口径"
    )


def test_the_seed_rows_are_the_ones_the_hr_matrix_names(fresh):
    """六个字段与人事矩阵对得上：身份证、银行卡、薪资、合同、绩效、入职材料。"""

    suite._cli(["db", "init"], fresh)

    labels = {row["label"] for row in _rows(fresh).values()}
    assert labels == {"身份证", "银行卡", "薪资", "合同", "绩效", "入职材料"}
    assert [row["field_key"] for row in SENSITIVE_FIELDS] == list(declared_keys())
