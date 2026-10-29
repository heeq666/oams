"""通讯录：分哪一组由授权决定，不由岗位名决定。

通讯录是页面上唯一「列出全公司的人」的地方，所以它只要分错一次组，就会让人
以为某个人能改别人的进度（或者反过来，把真正能改的人埋进「同事」里）。这里
锁住三件事：一个人只出现一次、自己不在名单里、分组结果与 admin_level 完全
一致——后者是权限判断的唯一口径，通讯录不许另起一份。

顺带锁住搜索口径：计数是过滤之前的总数，所以搜完之后右栏说明里的「N 位部门
管理员 · M 位同事」不会跟着变小。
"""
import pytest

import workhub.api  # noqa: F401  -- web imports api; importing web alone is a half-built module
from workhub import collab
from workhub.security import ROLE_DEPARTMENT, ROLE_SYSTEM, admin_level


def _active_members(db):
    return db.all(
        """SELECT member_id, display_name FROM workhub.workforce_members
            WHERE employment_status = 'active' ORDER BY display_name""")


def _rows(data):
    return [row for group in data["groups"] for row in group["rows"]]


@pytest.mark.db
def test_the_directory_lists_everyone_once_and_never_me(db_ready):
    members = _active_members(db_ready)
    if len(members) < 2:
        pytest.skip("the seed has fewer than two active members")

    with db_ready.connect() as conn:
        for me in members:
            listed = [row["member_id"] for row in _rows(collab.directory(conn, me["member_id"]))]
            assert len(listed) == len(set(listed)), f"{me['display_name']} 的名册里有重复的人"
            assert me["member_id"] not in listed, f"{me['display_name']} 在通讯录里看到了自己"
            assert len(listed) == len(members) - 1


@pytest.mark.db
def test_the_two_groups_follow_the_grants_not_the_job_title(db_ready):
    members = _active_members(db_ready)
    if len(members) < 2:
        pytest.skip("the seed has fewer than two active members")

    with db_ready.connect() as conn:
        for me in members:
            data = collab.directory(conn, me["member_id"])
            rows = _rows(data)
            for row in rows:
                level = admin_level(conn, row["member_id"])
                assert row["is_admin"] == (level in (ROLE_SYSTEM, ROLE_DEPARTMENT)), (
                    f"{me['display_name']} 看到的 {row['title']} 分组与授权不一致"
                    f"（admin_level={level}）")
            assert data["total_admins"] == sum(1 for row in rows if row["is_admin"])
            assert data["total_peers"] == sum(1 for row in rows if not row["is_admin"])


@pytest.mark.db
def test_searching_narrows_the_list_but_not_the_counts(db_ready):
    members = _active_members(db_ready)
    if len(members) < 2:
        pytest.skip("the seed has fewer than two active members")

    with db_ready.connect() as conn:
        everyone = collab.directory(conn, members[0]["member_id"])
        someone = _rows(everyone)[0]

        narrowed = collab.directory(conn, members[0]["member_id"], someone["title"])
        hits = _rows(narrowed)
        assert someone["member_id"] in [row["member_id"] for row in hits]
        assert all(someone["title"].lower() in row["title"].lower()
                   or someone["title"].lower() in row["subtitle"].lower() for row in hits)
        # 右侧说明说的是「全公司有多少人」，不是「搜出来多少」。
        assert narrowed["total_admins"] == everyone["total_admins"]
        assert narrowed["total_peers"] == everyone["total_peers"]

        nothing = collab.directory(conn, members[0]["member_id"], "绝不可能有人叫这个名字")
        assert nothing["groups"] == []
