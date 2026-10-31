"""守住「新库跑完 ``oams db init`` 之后 db 用例真的会跑」这条。

背景：``tests/conftest.py`` 判断「这个库建过表了吗」原来用的是
``to_regclass('oams.schema_migrations')``。``f9fae3d`` 用单文件 ``schema.sql`` +
``oams db init`` 取代增量迁移，那张台账表被删掉，判据却没跟着改 —— 后果是**任何新库
跑完 db init 之后，所有 db 用例全 skip**，pytest 仍然显示 ``0 failed`` 一片绿。

这是最危险的一类回归：不红、只是安静地不跑。所以用两条钉住：

1. 判据指向的对象必须在 ``app/oams/schema.sql`` 里真的被创建（无库也跑，谁删了它当场红）；
2. 真造一个空库 → 判据先认「没建表」→ 跑 ``db init`` → 判据必须认「建好了」（有库时跑）。

第二条跑出来的就是「2 passed / 0 skipped」而不是「2 skipped」这件事本身。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import conftest as suite  # pytest 会把 tests/ 放进 sys.path，与 rootdir 约定一致

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_SQL = REPO_ROOT / "app" / "oams" / "schema.sql"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_marker_object_is_created_by_schema_sql() -> None:
    """判据必须指向 schema.sql 真建出来的对象，否则新库永远被判「没迁移」。"""

    marker = suite._SCHEMA_MARKER
    assert marker.startswith("oams."), f"判据应带 schema 前缀：{marker!r}"
    object_name = marker.split(".", 1)[1]

    schema = _read(SCHEMA_SQL)
    created = re.search(
        rf"CREATE\s+(?:TABLE|VIEW)\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:OR\s+REPLACE\s+)?"
        rf"oams\.{re.escape(object_name)}\b",
        schema,
        re.IGNORECASE,
    )
    assert created is not None, (
        f"conftest 拿 {marker} 判断库是否建过表，但 {SCHEMA_SQL} 里没有创建它。"
        "改判据或改 schema.sql，别让新库被判成「没迁移」——那样全部 db 用例会静默 skip。"
    )


@pytest.mark.db
def test_fresh_database_after_db_init_is_recognised(db_ready) -> None:
    """空库 → db init → 判据必须认；这条用真库跑，就是「db 用例不会静默 skip」的自动化。"""

    name, url = suite._create_database(db_ready)
    try:
        assert suite.schema_initialised(url) is False, (
            "刚建出来的空库被判成「已建表」，判据不可信"
        )
        suite._cli(["db", "init"], url)
        assert suite.schema_initialised(url) is True, (
            f"跑完 db init 之后 {suite._SCHEMA_MARKER} 仍然不存在："
            "db 用例会在这个库上全部 skip"
        )
    finally:
        suite._drop_database(db_ready, name)
