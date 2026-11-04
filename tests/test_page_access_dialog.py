"""页面设置的权限弹窗：谁能改、勾了什么就落什么、越界整笔不落库、管不到的保留。

这一页的可见范围是**运营动作**，不是签字动作（`_access_editor`）：系统管理员随时能改
任何部门的页，部门管理员只改自己范围里的页。前端把勾不上的部门灰掉只是提示，
真正的判据在保存时按人再核一遍（`security.grantable_org_unit_ids`）——越界就整笔退回
弹窗带中文原因，库里一行都不留。

五条用例对应五件会真出事的错：全公司开不出来、部门管理员把别人的部门开出去、
保存一次把系统管理员的授权悄悄删掉、改了不留审计、没权限的人也能打开弹窗。

数据一律自己造、整笔事务结束时回滚，不依赖种子库以外的东西。
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import urllib.parse

import pytest
from starlette.testclient import TestClient

import oams.api
from oams import web
from oams.config import settings

pytestmark = pytest.mark.db

PLATFORM = "ADMIN001"      # 系统管理员：`workforce.grants/manage`，哪个部门都能改
DESIGN_LEAD = "DESIGN001"  # 部门管理员：`workforce.interfaces/review`，只管 DESIGN
PLAIN = "MVP1001"          # 人事经理：能干活，但没有 `workforce.interfaces/review`

SLUG = "design-sampling"   # 种子里设计部那张已经上线的页
PURPOSE = "oams:page-access-form:v1"
NOT_AN_EDITOR = "改页面权限要系统管理员，或这一页所属部门的部门管理员。"


@contextlib.contextmanager
def _one_transaction(conn):
    yield conn


def _csrf(cookie: str) -> str:
    """表单令牌就是拿会话 cookie 按用途签一次，跟 `web._form_token` 同一把钥。"""
    return hmac.new(cookie.encode(), PURPOSE.encode(), hashlib.sha256).hexdigest()


@pytest.fixture()
def room(seeded_database, monkeypatch):
    """一笔事务里的种子库 + 一个能按身份切人的 TestClient。"""

    import psycopg
    from psycopg.rows import dict_row

    conn = psycopg.connect(seeded_database.url, row_factory=dict_row)
    monkeypatch.setattr(web, "connection", lambda: _one_transaction(conn))
    monkeypatch.setattr(oams.api, "connection", lambda: _one_transaction(conn))

    def unit(code):
        row = conn.execute("SELECT org_unit_id FROM oams.org_units WHERE org_code = %s",
                           (code,)).fetchone()
        assert row is not None, f"{code} 不在种子库里"
        return row["org_unit_id"]

    page = conn.execute(
        """SELECT i.interface_id, i.org_unit_id FROM oams.interfaces i
             JOIN oams.org_units o ON o.org_unit_id = i.org_unit_id
            WHERE o.org_code = 'DESIGN' AND i.slug = %s AND i.status <> 'archived'
            ORDER BY (i.status = 'active') DESC, i.version_no DESC
            LIMIT 1""", (SLUG,)).fetchone()
    assert page is not None, "种子里应该有设计部那张页"

    people = {code: conn.execute(
        "SELECT member_id FROM oams.workforce_members WHERE employee_code = %s",
        (code,)).fetchone()["member_id"]
        for code in (PLATFORM, DESIGN_LEAD, PLAIN)}

    state = {"me": PLATFORM, "cookie": "page-access-dialog-test"}

    def _principal(request):
        return {"member_id": people[state["me"]],
                "employee_code": state["me"],
                "display_name": state["me"],
                "employment_status": "active"}

    monkeypatch.setattr(oams.api, "principal_from_request", _principal)

    def client() -> TestClient:
        made = TestClient(oams.api.app)
        made.cookies.set(settings.web_session_cookie, state["cookie"])
        return made

    def as_(code):
        state["me"] = code

    path = f"/reviews/settings/DESIGN/{SLUG}/access"

    def dialog():
        return client().get(path, follow_redirects=False)

    def save(read=(), write=()):
        form: dict[str, object] = {"csrf": _csrf(state["cookie"])}
        if read:
            form["read"] = [str(unit) for unit in read]
        if write:
            form["write"] = [str(unit) for unit in write]
        return client().post(path, data=form, follow_redirects=False)

    def rows():
        return conn.execute(
            """SELECT org_unit_id, can_read, can_write FROM oams.interface_access
                WHERE owner_org_unit_id = %s AND slug = %s
                ORDER BY org_unit_id""", (page["org_unit_id"], SLUG)).fetchall()

    def clear():
        conn.execute("""DELETE FROM oams.interface_access
                         WHERE owner_org_unit_id = %s AND slug = %s""",
                     (page["org_unit_id"], SLUG))

    def audits():
        return conn.execute(
            """SELECT actor_member_id, reason, before_state, after_state, metadata
                 FROM oams.workforce_audit_events
                WHERE event_type = 'interface.access_changed' AND subject_id = %s
                ORDER BY occurred_at, event_id""", (page["interface_id"],)).fetchall()

    made = {"conn": conn, "client": client, "as_": as_, "dialog": dialog, "save": save,
            "rows": rows, "clear": clear, "audits": audits, "unit": unit,
            "interface_id": page["interface_id"], "owner": page["org_unit_id"],
            "design": unit("DESIGN"), "hr": unit("HR"), "company": unit("COMPANY"),
            "people": people}
    try:
        yield made
    finally:
        conn.rollback()
        conn.close()


def _text(response) -> str:
    return urllib.parse.unquote(response.headers.get("location", "")) + response.text


def test_a_system_admin_opens_the_page_to_the_whole_company(room):
    """勾公司根 = 全公司：落一行 owner 页 / 公司根，其余部门不用再勾。"""
    room["as_"](PLATFORM)
    room["clear"]()
    form = room["dialog"]()
    assert form.status_code == 200, form.text
    assert "「设计部打样进度」的可见范围" in form.text
    # 公司根那一行对系统管理员是可勾的，并且带着「全公司」这个标签
    assert 'name="read" value="' + str(room["company"]) + '"' in form.text
    assert "全公司" in form.text

    saved = room["save"](read=[room["company"]])
    assert saved.status_code == 303, saved.text
    assert saved.headers["location"].startswith("/reviews/settings?saved="), saved.headers["location"]
    assert "权限已保存" in urllib.parse.unquote(saved.headers["location"])

    rows = room["rows"]()
    assert len(rows) == 1, rows
    assert rows[0]["org_unit_id"] == room["company"]
    assert rows[0]["can_read"] is True and rows[0]["can_write"] is False


def test_a_department_admin_cannot_open_a_page_to_another_department(room):
    """部门管理员勾别人的部门：整笔退回弹窗说清是谁，库里一行不落。"""
    room["as_"](DESIGN_LEAD)
    room["clear"]()

    # 弹窗先把勾不上的行说清楚：人事部是「你没有管理范围」，公司根是「只有系统管理员」。
    form = room["dialog"]()
    assert form.status_code == 200, form.text
    assert "你没有「人事部」的管理范围" in form.text
    assert "只有系统管理员能把页面开给全公司" in form.text

    refused = room["save"](read=[room["hr"]])
    assert refused.status_code == 303, refused.text
    location = urllib.parse.unquote(refused.headers["location"])
    assert "/reviews/settings/DESIGN/design-sampling/access?error=" in location, location
    assert "你没有这些部门的管理范围：人事部" in location
    assert room["rows"]() == []


def test_grants_a_manager_cannot_see_survive_a_save(room):
    """管不到的部门若已有授权，原样留着——否则保存一次就把别人的授权删了。"""
    room["as_"](PLATFORM)
    room["clear"]()
    opened = room["save"](read=[room["company"]])
    assert opened.status_code == 303 and "error=" not in opened.headers["location"]
    assert [row["org_unit_id"] for row in room["rows"]()] == [room["company"]]

    # 部门管理员只勾了自己范围里的东西（这里是「什么都不勾」）——公司根那一行必须还在。
    room["as_"](DESIGN_LEAD)
    kept = room["save"]()
    assert kept.status_code == 303, kept.text
    assert "error=" not in kept.headers["location"], kept.headers["location"]
    rows = room["rows"]()
    assert [row["org_unit_id"] for row in rows] == [room["company"]], rows
    assert rows[0]["can_read"] is True


def test_a_saved_change_leaves_one_audit_event_with_before_and_after(room):
    """一次保存一条审计，前后都在，人话里带页名。"""
    room["as_"](PLATFORM)
    room["clear"]()
    assert room["save"](read=[room["company"]]).status_code == 303
    changed = room["save"](read=[room["hr"]], write=[room["hr"]])
    assert changed.status_code == 303, changed.text

    events = room["audits"]()
    assert len(events) == 2, events
    # 同一个事务里两条事件的 occurred_at 一模一样，别按时间排序挑「最新那条」——
    # 按「改成了什么」认人，才不依赖数据库给的顺序。
    def audience_of(event, key):
        return [row["org_code"] for row in event[key]["audience"]]

    opened = next(event for event in events
                  if audience_of(event, "after_state") == ["COMPANY"])
    changed = next(event for event in events if audience_of(event, "after_state") == ["HR"])

    # 第一次保存时前面什么都没有——before 是空清单，不是 null
    assert opened["before_state"]["audience"] == []
    # 第二次的前面正是第一次留下的那一行，不是空的
    assert audience_of(changed, "before_state") == ["COMPANY"]
    assert [(row["org_code"], row["can_read"], row["can_write"])
            for row in changed["after_state"]["audience"]] == [("HR", True, True)]
    assert changed["actor_member_id"] == room["people"][PLATFORM]
    assert "改了「设计部打样进度」的可见范围" in changed["reason"]
    # 按应用/按页筛人：metadata 里得能认出是哪一页
    assert changed["metadata"]["slug"] == SLUG
    assert changed["metadata"]["org_code"] == "DESIGN"
    assert audience_of(changed, "metadata") == ["HR"]


def test_someone_without_review_cannot_open_the_dialog(room):
    """没有 `interfaces/review` 的人：弹窗打不开，保存也退回并说清缺什么。"""
    room["as_"](PLAIN)
    room["clear"]()

    opened = room["dialog"]()
    # GET 不给白墙：把他送回能读的首页，不是 200 里塞一张空弹窗
    assert opened.status_code == 303, opened.status_code
    assert "/access" not in urllib.parse.unquote(opened.headers["location"])

    refused = room["save"](read=[room["company"]])
    assert refused.status_code == 403, refused.status_code
    assert NOT_AN_EDITOR in _text(refused)
    assert room["rows"]() == []
