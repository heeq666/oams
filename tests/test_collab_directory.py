"""通讯录：分哪一组由授权决定，不由岗位名决定。

通讯录是页面上唯一「列出全公司的人」的地方，所以它只要分错一次组，就会让人
以为某个人能改别人的进度（或者反过来，把真正能改的人埋进「同事」里）。这里
锁住三件事：一个人只出现一次、自己不在名单里、分组结果与 admin_level 完全
一致——后者是权限判断的唯一口径，通讯录不许另起一份。

顺带锁住搜索口径：计数是过滤之前的总数，所以搜完之后右栏说明里的「N 位部门
管理员 · M 位同事」不会跟着变小。
"""
import pytest

import oams.api  # noqa: F401  -- web imports api; importing web alone is a half-built module
from oams import collab
from oams.security import ROLE_DEPARTMENT, ROLE_SYSTEM, admin_level


def _active_members(db):
    return db.all(
        """SELECT member_id, display_name FROM oams.workforce_members
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
@pytest.mark.db
def test_windows_carry_on_without_repeating_the_group_title(db_ready):
    """通讯录分片：一窗 20 行，第二窗接着第一窗时不重画分组标题。

    锁三件事：窗口里行数不超过 limit、计数仍是全公司（不跟着窗口缩）、
    上一窗最后一行属于哪组就由 after 带下去，滚动加载处不会冒出第二个「同事」。

    用 limit=1 而不是 20：种子库只有一两个人，写死 20 这条用例就永远是跳过的。
    """
    members = _active_members(db_ready)
    if len(members) < 2:
        pytest.skip("the seed has fewer than two active members")

    with db_ready.connect() as conn:
        me = members[0]["member_id"]
        first = collab.directory(conn, me, limit=1)
        assert len(_rows(first)) == 1
        # 只发一窗：全公司还有别人时才留着「加载更多」。
        assert first["has_more"] == (len(members) - 1 > 1)
        assert first["next_offset"] == 1
        assert first["total_admins"] + first["total_peers"] == len(members) - 1

        second = collab.directory(conn, me, limit=1, offset=1, after=first["after"])
        rows = _rows(second)
        if rows:
            assert rows[0]["member_id"] not in [r["member_id"] for r in _rows(first)]
            if second["groups"][0]["key"] == first["after"]:
                # 上一窗最后一行属于「同事」，这一窗的第一行就不能再画一次「同事」。
                assert second["groups"][0]["continued"] is True
        else:
            assert second["has_more"] is False

        # 走到最后一窗：has_more 收掉，next_offset 不再涨。
        tail = collab.directory(conn, me, limit=200)
        assert tail["has_more"] is False
        assert len(_rows(tail)) == len(members) - 1
