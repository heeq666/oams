"""`/setup` 上「部门负责人」勾选：回执、幂等、宽授权窄行为。

后端 `8f3b98f` 把勾选接进了 `/setup/pilot`，模板 `5f5cae9` 加了回执句
「已授本部门页面初审权。」。这两处此前一条用例都没有——把模板那句删了、或者把
`if as_department_lead:` 去掉，全量照样绿。所以这里不测样式，断的是 URL 上的
`&lead=1` 与 DOM 里那句话本身。

第三条钉的是这份授权最容易被「顺手修正」的地方：行上必须 `scope_type='all'`
（缩成 `org_unit` 之后 `security.effective_grants` 一条都不认，等于没授），
而行为必须窄——本部门别人的页签得动、别部门签不了、自己提交的签不了。
"""
from __future__ import annotations

import contextlib
import datetime
import hashlib
import hmac
import urllib.parse
import uuid

import pytest

import oams.api  # noqa: F401  (import order: web imports api back)
import oams.security
from oams import web
from test_review_stages import DESIGN_AUTHOR, PLATFORM, _approve, room  # noqa: F401  (fixture)

pytestmark = pytest.mark.db

#: room fixture 里 TestClient 的那个会话 cookie（见 test_review_stages 的 `state["cookie"]`）。
COOKIE = "review-stage-test"


@pytest.fixture()
def pilot_room(room, monkeypatch):
    """room + 把会话签发也接进同一笔事务。

    `/setup/pilot` 建完成员会 `issue_session()`，它走的是 `oams.security.connection`；
    room 只替换了 `web` 与 `api` 两处，不补这一处，签发会在另一条连接上看不到
    刚建的那个成员（`member must exist and be active`），用例就永远红。
    """
    monkeypatch.setattr(oams.security, "connection",
                        lambda: contextlib.nullcontext(room["conn"]))
    return room


def _token(purpose: str) -> str:
    return hmac.new(COOKIE.encode(), purpose.encode(), hashlib.sha256).hexdigest()


def _new_position(room, code: str):
    """从真实入口建一个岗位：授权挂在岗位上，先得有岗位。"""
    room["as_"](PLATFORM)
    made = room["client"]().post(
        "/setup/positions",
        data={"csrf": _token("oams:position-form:v1"), "position_code": code,
              "title": "试点负责人", "org_unit_id": str(room["design"])},
        follow_redirects=False)
    assert made.status_code == 303, made.text
    assert "saved=position" in made.headers["location"], made.headers["location"]
    return room["conn"].execute(
        "SELECT position_id FROM oams.positions WHERE position_code = %s",
        (code,)).fetchone()["position_id"]


def _create_pilot(room, position_id, *, employee_code: str, lead: bool | None = None):
    """同一个入口建试用成员；`lead` 为 None 就是不勾那一栏。"""
    room["as_"](PLATFORM)
    data = {"csrf": _token("oams:pilot-form:v1"), "display_name": "试点负责人",
            "employee_code": employee_code, "position_id": str(position_id)}
    if lead is not None:
        data["as_department_lead"] = "true" if lead else ""
    made = room["client"]().post("/setup/pilot", data=data, follow_redirects=False)
    assert made.status_code == 303, made.text
    location = made.headers["location"]
    query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    return location, query


def test_the_receipt_is_only_there_when_the_box_was_ticked(pilot_room):
    """勾与不勾，URL 与页面上都得说得不一样。

    `lead=1` 是「这个岗位现在真的有初审权」的回执，不是「用户点了勾」的回执——
    模板那句同理，只在授权表真有这一行时才出现。
    """
    position = _new_position(pilot_room, "PILOTLEAD01")

    location, query = _create_pilot(pilot_room, position, employee_code="PILOT101", lead=True)
    assert query.get("lead") == ["1"], location
    assert "&lead=1" in location, location
    ticket = pilot_room["client"]().get(location, follow_redirects=False)
    assert ticket.status_code == 200, ticket.text
    assert "已授本部门页面初审权。" in ticket.text

    other_position = _new_position(pilot_room, "PILOTLEAD01B")
    untouched, plain = _create_pilot(pilot_room, other_position, employee_code="PILOT102")
    assert "lead" not in plain, untouched
    assert "&lead=1" not in untouched, untouched
    quiet = pilot_room["client"]().get(untouched, follow_redirects=False)
    assert quiet.status_code == 200, quiet.text
    assert "已授本部门页面初审权。" not in quiet.text


def test_the_same_position_never_gets_a_second_review_grant(pilot_room):
    """回执说的是「现在有这条权限」，所以同一岗位第二次仍是同一份授权、不长第二行。

    这里**不能**靠「同一个岗位再提交一次表单」来测：`position_assignments` 上有
    `position_assignments_single_primary_per_position`（一个岗位同时只能有一个主任职），
    第二次提交会得到 `?error=pilot-duplicate`，请求回滚、什么都不会写——那证明的是
    产品不让一个岗位坐两个人，不是授权写了几行。所以第二段直接调那个授权函数：
    它的契约就是「第二次调用返回 True 且不写第二行」。
    """
    position = _new_position(pilot_room, "PILOTLEAD02")
    first, query = _create_pilot(pilot_room, position, employee_code="PILOT201", lead=True)
    assert query.get("lead") == ["1"], first

    again = web._grant_pilot_department_lead(
        pilot_room["conn"], position, pilot_room["people"][PLATFORM],
        datetime.datetime.now(datetime.timezone.utc).isoformat())
    assert again is True, "第二次调用应该回答「现在有这条权限」，而不是 False"

    rows = pilot_room["conn"].execute(
        """SELECT count(*) AS n FROM oams.permission_grants
            WHERE subject_type = 'position' AND position_id = %s
              AND resource = 'workforce.interfaces' AND action = 'review'
              AND scope_type = 'all' AND effect = 'allow' AND revoked_at IS NULL""",
        (position,)).fetchone()["n"]
    assert rows == 1, f"同一岗位应该只有一行初审授权，实际 {rows} 行"


def test_the_row_is_wide_and_the_behaviour_is_narrow(pilot_room):
    """`scope_type='all'` 是有意的：缩窄它等于没授；边界在路由那一层。

    只断 `all` 不够——那只能证明有人写了这个值，证明不了它能用、也证明不了它
    不越界。所以三条行为一起断：签得动本部门别人的页，签不了别部门，签不了自己交的。
    """
    position = _new_position(pilot_room, "PILOTLEAD03")
    location, query = _create_pilot(pilot_room, position, employee_code="PILOT301", lead=True)
    assert query.get("lead") == ["1"], location
    pilot = query["member_id"][0]

    row = pilot_room["conn"].execute(
        """SELECT scope_type FROM oams.permission_grants
            WHERE subject_type = 'position' AND position_id = %s
              AND resource = 'workforce.interfaces' AND action = 'review'
              AND revoked_at IS NULL""", (position,)).fetchone()
    assert row is not None, "勾了却没写下授权行"
    assert row["scope_type"] == "all", row

    # `pilot_room["as_"]` 按工号在 people 里查 member_id；新成员是刚建的，得先登记进去。
    pilot_room["people"]["PILOT301"] = uuid.UUID(pilot)

    # 授权与任职的时间戳取的是「建这一笔的那一刻」，而 `effective_grants` 拿事务的
    # `now()` 去比——事务的 now() 冻结在 BEGIN，比它早几毫秒。真实里下一步是另一个
    # 请求（新事务），自然成立；这条用例整条跑在同一笔事务里，只能把这两个时间戳
    # 对齐到「一秒前」，才等价于跨请求。改的是时间戳，不是业务状态。
    pilot_room["conn"].execute(
        """UPDATE oams.position_assignments SET starts_at = now() - interval '1 second'
            WHERE member_id = %s AND position_id = %s""", (uuid.UUID(pilot), position))
    pilot_room["conn"].execute(
        """UPDATE oams.permission_grants SET valid_from = now() - interval '1 second'
            WHERE position_id = %s AND revoked_at IS NULL""", (position,))

    # 一、本部门、别人提交的页：签得动（初审判通过）。
    other_page = pilot_room["page"](pilot_room["design"], author=DESIGN_AUTHOR)
    pilot_room["as_"]("PILOT301")
    passed = _approve(pilot_room, other_page, note="部门初审过")
    assert passed.status_code == 303, passed.text
    assert passed.headers["location"] == "/reviews?saved=approved", passed.headers["location"]

    # 二、别部门的页：签不了。
    hr_page = pilot_room["page"](pilot_room["hr"])
    pilot_room["as_"]("PILOT301")
    refused = _approve(pilot_room, hr_page, note="想越界")
    # 审核这条路的拒绝是「跳回列表带 ?error=」，不是 403 页面——和预览路由不同。
    assert refused.status_code == 303, refused.text
    assert "这个页面不在你所在部门的范围内" in urllib.parse.unquote(refused.headers["location"])

    # 三、自己提交的页：签不了——有审核权也不等于能自审。
    pilot_room["as_"]("PILOT301")
    own_page = pilot_room["create"](pilot_room["design"], author="PILOT301", name="自己交的那一页")
    pilot_room["submit"](pilot_room["design"], own_page)
    pilot_room["as_"]("PILOT301")
    self_refused = _approve(pilot_room, own_page, note="想自己签")
    assert self_refused.status_code == 303, self_refused.text
    assert "自己提交的页面要由别人审" in urllib.parse.unquote(self_refused.headers["location"])


# --------------------------------------------------------------------------- #
# 409 的两面：岗位已占 / 编号重复（api.CONFLICT_HEADER 那两个稳定标记）
# --------------------------------------------------------------------------- #


def test_the_two_conflicts_are_told_apart_on_the_page(pilot_room):
    """两句话不能互换：按「编号重复」去改编号，改十次也建不进去。

    `?error=` 的标记由 web 按 `api.CONFLICT_HEADER` 分出来，用户看到的是模板
    这两行——所以两行都要在，且谁也不能出现在对方那一支里。
    """
    pilot_room["as_"](PLATFORM)

    taken = pilot_room["client"]().get("/setup?error=pilot-position-taken")
    assert taken.status_code == 200
    assert "这个岗位已经有人了" in taken.text
    assert "这个员工编号已经存在" not in taken.text

    duplicate = pilot_room["client"]().get("/setup?error=pilot-duplicate")
    assert duplicate.status_code == 200
    assert "这个员工编号已经存在" in duplicate.text
    assert "这个岗位已经有人了" not in duplicate.text


def test_a_taken_position_is_not_reported_as_a_duplicate_code(pilot_room):
    position = _new_position(pilot_room, "PILOTTAKEN")
    first, query = _create_pilot(pilot_room, position, employee_code="PILOT401")
    assert "error" not in query, first

    location, query = _create_pilot(pilot_room, position, employee_code="PILOT402")
    assert query["error"] == ["pilot-position-taken"], location


def test_a_duplicate_code_is_not_reported_as_a_taken_position(pilot_room):
    position = _new_position(pilot_room, "PILOTDUP01")
    other = _new_position(pilot_room, "PILOTDUP02")
    first, query = _create_pilot(pilot_room, position, employee_code="PILOT501")
    assert "error" not in query, first

    location, query = _create_pilot(pilot_room, other, employee_code="PILOT501")
    assert query["error"] == ["pilot-duplicate"], location


def test_a_taken_position_carries_the_marker_that_says_so(pilot_room):
    """判据是标在 `X-OAMS-Conflict` 上的那个码，不是 detail 的英文措辞。

    `detail` 随时可以改一遍措辞，改完调用方就不该跟着改；这层契约要是塌了，
    `/setup` 只能靠认字符串，第二个把人往同一岗位放的人又会看到假话。
    """
    import pytest as _pytest

    from oams import api as oams_api
    from oams.schemas import AssignmentCreate

    position = _new_position(pilot_room, "PILOTMARK")
    _create_pilot(pilot_room, position, employee_code="PILOT601")
    member_id = pilot_room["conn"].execute(
        "SELECT member_id FROM oams.workforce_members WHERE employee_code = %s",
        ("PILOT601",)).fetchone()["member_id"]

    with _pytest.raises(oams_api.HTTPException) as caught:
        oams_api.insert_assignment(pilot_room["conn"], AssignmentCreate(
            member_id=member_id, position_id=position, assignment_type="primary",
            starts_at=datetime.datetime.now(datetime.timezone.utc), reason="撞同一个岗位"),
            pilot_room["people"][PLATFORM])

    assert caught.value.status_code == 409
    assert caught.value.headers[oams_api.CONFLICT_HEADER] == oams_api.CONFLICT_POSITION_OCCUPIED
