"""照着别人的页面做一版：复制定义、不复制数据，三样私有信息必须换成本部门的。

来由（2026-11-09）：`interfaces.html` 上「照着做一版 →」原来只把来源记在案，页面
描述得自己从头重写一遍——对外说的「照搬」实际搬不了一行。这次把它做实。

三条硬要求，用例按它们写：
1. 三样部门私有信息必须换掉（`org_unit_code` / `position_codes` / `interface_id`）：
   留在里头就是一页挂在别人部门身份上的残页，而 `validate_descriptor` 只看编码存
   不存在、看不出这是谁家的编码；
2. 条目数据一条都不跟过来——「可作参考」说的是页面定义（字段、模块、版式），不
   是条目里的内容；
3. 权限每次重新判：表单里带回来的来源 id 不是依据。
"""
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path
from uuid import uuid4

import pytest

import conftest as suite  # noqa: E402  pytest 会把 tests/ 放进 sys.path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

pytestmark = pytest.mark.db

# 先导 api：`app/oams/api.py:1873` 反向 import web，先导 web 会撞循环导入。
import oams.api  # noqa: E402,F401

TARGET_ORG = "HR"      # 只是给断言消息留的可读名，真实值一律从 _world() 拿
SOURCE_ORG = "DESIGN"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """复制成功靠 303 说明去向，跟随重定向会把断言要看的地址吃掉。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _call(opener, base: str, path: str, *, method: str = "GET", form: dict | None = None):
    payload = urllib.parse.urlencode(form).encode() if form is not None else None
    request = urllib.request.Request(base + path, data=payload, method=method)
    if payload is not None:
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with opener.open(request, timeout=30) as response:
            return response.status, response.read().decode(), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(), dict(error.headers)


def _logged_in(base: str, employee_code: str):
    """用真接口登录一个人：签发会话、把它贴到 /login 的表单上。

    成员按**应用自己连的那个库**来查（`oams.db.connection`）。全量跑起来时别的用例
    会把连接指向临时库，这里若拿 `db_ready.url` 的 member_id 去签会话，服务端在自己
    库上找不到那个人，报「member must exist and be active」。
    """

    from oams.db import connection
    from oams.security import issue_session

    with connection() as conn:
        row = conn.execute("SELECT member_id FROM oams.workforce_members "
                           "WHERE employee_code = %s", (employee_code,)).fetchone()
    assert row is not None, f"应用连的库里没有 {employee_code}"
    token, _ = issue_session(row["member_id"])
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirect,
        urllib.request.HTTPCookieProcessor(CookieJar()))
    status, _, _ = _call(opener, base, "/login", method="POST", form={"token": token})
    assert status == 302, f"{employee_code} 登录失败：{status}"
    return opener


def _csrf(html: str) -> str:
    found = re.search(r'name="csrf" value="([0-9a-f]+)"', html)
    assert found, "复制页上没有 csrf 字段，提交会被挡掉"
    return found.group(1)


def _world() -> dict:
    """种子库里的世界：DESIGN 那一页当来源，HR 的岗位当目标。

    用套件自己的种子库（`site` fixture）：它由部署时同一套 bootstrap 与种子脚本
    建成，跑全量时不会被别的用例改指向，也不会像开发库那样时有时无——早先这组
    用例在开发库上写，全量跑起来连接被指到临时库，三条就静默 skip 了。
    """

    from oams.db import connection

    with connection() as conn:
        source = conn.execute(
            """SELECT i.interface_id, i.slug, i.name, i.descriptor, o.org_code AS source_code
                 FROM oams.interfaces i JOIN oams.org_units o USING (org_unit_id)
                WHERE i.status = 'active'
                ORDER BY i.slug LIMIT 1""").fetchone()
        target = conn.execute(
            """SELECT o.org_unit_id, o.org_code, p.position_id, p.position_code
                 FROM oams.positions p JOIN oams.org_units o USING (org_unit_id)
                WHERE p.status = 'active' AND o.org_code NOT IN ('COMPANY', %s)
                ORDER BY o.org_code, p.position_code LIMIT 1""",
            (source["source_code"] if source else "",)).fetchone()
        foreign = conn.execute(
            """SELECT p.position_id FROM oams.positions p
                WHERE p.status = 'active' AND p.org_unit_id <> %s LIMIT 1""",
            (target["org_unit_id"] if target else None,)).fetchone()
    # 种子里缺一样就直接红，不 skip：跳过也是绿，可这组用例什么都没验
    assert source is not None, "种子库里没有一张 active 页，复制功能无从验起"
    assert target is not None, "种子库里没有第二个带岗位的部门，复制功能无从验起"
    assert foreign is not None, "种子库里没有别的部门的岗位，越权那条无从验起"
    return {"target": target, "source": source, "foreign": foreign,
            "position": {"position_id": target["position_id"],
                         "position_code": target["position_code"]}}


def _row_by_slug(slug: str):
    from oams.db import connection

    with connection() as conn:
        return conn.execute(
            """SELECT interface_id, org_unit_id, slug, name, status, version_no, based_on,
                      descriptor, position_id
                 FROM oams.interfaces WHERE slug = %s
                ORDER BY version_no DESC LIMIT 1""", (slug,)).fetchone()


def _discard(slug: str) -> None:
    """只删这次造的草稿；审计 append-only，一行都不动。"""

    from oams.db import connection

    with connection() as conn:
        row = conn.execute("SELECT interface_id FROM oams.interfaces WHERE slug = %s",
                           (slug,)).fetchone()
        if row is None:
            return
        interface_id = row["interface_id"]
        conn.execute("DELETE FROM oams.interface_entries WHERE interface_id = %s",
                     (interface_id,))
        conn.execute("DELETE FROM oams.interfaces WHERE interface_id = %s", (interface_id,))


# ---------------------------------------------------------------------------
# 1. 纯函数：三样私有信息换掉，其余一字不动
# ---------------------------------------------------------------------------


def test_copied_descriptor_rewrites_only_the_private_keys():
    from oams.web import copied_descriptor

    source = {"interface_id": "old-id", "name": "财务报销台账", "org_unit_code": "FIN",
              "position_codes": ["FIN_LEAD"], "fields": [{"key": "amount"}],
              "capabilities": ["entries:write"],
              "metrics": [{"metric_key": "dept.FIN.expense_total", "agg": "sum",
                           "from_field": "amount"},
                          {"metric_key": "entries.count", "agg": "count"}],
              "modules": [{"kind": "table"},
                          {"kind": "headline",
                           "params": {"metric_key": "dept.FIN.expense_total"}}]}
    snapshot = json.loads(json.dumps(source))

    copied = copied_descriptor(source, interface_id="new-id", name="行政报销台账",
                               org_unit_code="GA", position_code="GA_LEAD")

    assert copied["interface_id"] == "new-id"
    assert copied["name"] == "行政报销台账"
    assert copied["org_unit_code"] == "GA"
    assert copied["position_codes"] == ["GA_LEAD"]
    # 指标名里钉着部门：搬页必须连同命名空间一起搬，否则校验当场拒（它拒得对）
    assert [item["metric_key"] for item in copied["metrics"]] == \
        ["dept.GA.expense_total", "entries.count"]
    assert copied["metrics"][0]["from_field"] == "amount", "只换前缀，别把指标算法一起改了"
    assert copied["modules"][1]["params"]["metric_key"] == "dept.GA.expense_total"
    assert copied["modules"][0] == source["modules"][0]
    assert copied["fields"] == source["fields"]
    assert copied["capabilities"] == source["capabilities"]
    # 深拷贝：来源可能是刚从库里读出来的那一份，改副本不能把它改脏
    copied["modules"].append({"kind": "x"})
    copied["metrics"][0]["agg"] = "avg"
    assert source == snapshot


# ---------------------------------------------------------------------------
# 2. 走真路由：复制出一份草稿，私有信息全是本部门的
# ---------------------------------------------------------------------------


def test_copy_creates_a_department_draft_with_private_keys_rewritten(site):
    world = _world()
    slug = f"copy-e2e-{uuid4().hex[:10]}"
    target = world["target"]["org_unit_id"]
    src_code = world["source"]["source_code"]
    dst_code = world["target"]["org_code"]
    opener = _logged_in(site, "ADMIN001")
    _discard(slug)
    try:
        status, html, _ = _call(opener, site,
                                f"/departments/{target}/copy"
                                f"?based_on={world['source']['interface_id']}")
        assert status == 200, status
        assert world["source"]["name"] in html, "复制页没显示来源页名字，人没法确认照的是哪一页"

        status, body, headers = _call(
            opener, site, f"/departments/{target}/copy", method="POST",
            form={"csrf": _csrf(html), "source_id": str(world["source"]["interface_id"]),
                  "slug": slug, "name": "照过来的一版",
                  "position_id": str(world["position"]["position_id"])})
        assert status == 303, (status, body[:300], headers.get("location"))
        assert headers.get("location", "").endswith("?saved=1"), \
            f"没落在新页上，重定向去了 {headers.get('location')}"

        row = _row_by_slug(slug)
        assert row is not None, "复制之后库里没有这一页"
        assert row["status"] == "draft", "复制出来的页面不能一上来就是已发布"
        assert str(row["based_on"]) == str(world["source"]["interface_id"]), "来源没记下来"
        assert str(row["org_unit_id"]) == str(target)

        descriptor = row["descriptor"]
        assert descriptor["org_unit_code"] == dst_code, descriptor["org_unit_code"]
        assert descriptor["position_codes"] == [world["position"]["position_code"]], \
            descriptor["position_codes"]
        assert descriptor["interface_id"] == str(row["interface_id"]), \
            "描述里还指着来源页的编号，按描述找页面的代码会认错人"
        assert descriptor["name"] == "照过来的一版"
        assert descriptor["fields"] == world["source"]["descriptor"]["fields"]
        source_metrics = world["source"]["descriptor"].get("metrics") or []
        expected_keys = [item["metric_key"].replace(f"dept.{src_code}.",
                                                    f"dept.{dst_code}.")
                         for item in source_metrics]
        assert [item["metric_key"] for item in (descriptor.get("metrics") or [])] == expected_keys
        assert not any(str(item["metric_key"]).startswith(f"dept.{src_code}.")
                       for item in (descriptor.get("metrics") or [])), \
            "指标还挂在别的部门的命名空间里"
        # 除了命名空间，模块定义应当一字不差：把它换回来源的写法应当等于来源原文
        moved_back = json.loads(json.dumps(descriptor["modules"]).replace(
            f"dept.{dst_code}.", f"dept.{src_code}."))
        assert moved_back == world["source"]["descriptor"]["modules"]

        from oams.db import connection
        with connection() as conn:
            entries = conn.execute(
                "SELECT count(*) AS n FROM oams.interface_entries WHERE interface_id = %s",
                (row["interface_id"],)).fetchone()["n"]
            audit = conn.execute(
                """SELECT count(*) AS n FROM oams.workforce_audit_events
                    WHERE event_type = 'interface.copied'
                      AND subject_id = %s""", (row["interface_id"],)).fetchone()["n"]
        assert entries == 0, "条目跟过来了——复制的是定义，不是内容"
        assert audit == 1, f"复制应当恰好留一条审计，实际 {audit}"
    finally:
        _discard(slug)


# ---------------------------------------------------------------------------
# 2b. 带指标的页：命名空间必须跟着搬，否则校验当场拒（这条走真路由）
# ---------------------------------------------------------------------------


def test_copying_a_page_with_metrics_moves_them_into_the_departments_namespace(site):
    """种子里的 DESIGN 页没有指标，抄它验不出命名空间这件事——这里补一张带指标的。"""

    world = _world()
    src_code = world["source"]["source_code"]
    dst_code = world["target"]["org_code"]
    target = world["target"]["org_unit_id"]
    slug = f"copy-metrics-{uuid4().hex[:10]}"
    opener = _logged_in(site, "ADMIN001")
    source_id = _make_source_with_metrics(world)
    _discard(slug)
    try:
        status, html, _ = _call(opener, site,
                                f"/departments/{target}/copy?based_on={source_id}")
        assert status == 200, status
        status, body, headers = _call(
            opener, site, f"/departments/{target}/copy", method="POST",
            form={"csrf": _csrf(html), "source_id": source_id, "slug": slug,
                  "name": "带指标的一版",
                  "position_id": str(world["position"]["position_id"])})
        assert status == 303, (status, body[:300], headers.get("location"))
        assert headers.get("location", "").endswith("?saved=1"), \
            f"校验没放过这份描述，重定向去了 {headers.get('location')}"

        row = _row_by_slug(slug)
        assert row is not None, "带指标的页没复制出来"
        keys = [item["metric_key"] for item in (row["descriptor"].get("metrics") or [])]
        assert keys == [f"dept.{dst_code}.expense_total"], keys
        assert not any(key.startswith(f"dept.{src_code}.") for key in keys)
    finally:
        _discard(slug)
        _drop_page(source_id)


def _make_source_with_metrics(world: dict) -> str:
    """拿财务台账那份真实描述，改成本部门的一张 active 页，指标名也改成本部门的。"""

    from psycopg.types.json import Jsonb

    from oams.db import connection

    src_code = world["source"]["source_code"]
    path = suite.REPO_ROOT / "data" / "neutral-demo-fin-expense-ledger.json"
    descriptor = json.loads(path.read_text(encoding="utf-8"))
    new_id = str(uuid4())
    descriptor["interface_id"] = new_id
    descriptor["name"] = "带指标来源"
    descriptor["org_unit_code"] = src_code
    descriptor["position_codes"] = list(world["source"]["descriptor"].get("position_codes") or [])
    descriptor = json.loads(json.dumps(descriptor).replace("dept.FIN.", f"dept.{src_code}."))
    with connection() as conn:
        conn.execute(
            """INSERT INTO oams.interfaces
                   (interface_id, org_unit_id, slug, name, status, visibility, schema_version,
                    descriptor, position_id, version_no)
               VALUES (%s, (SELECT org_unit_id FROM oams.org_units WHERE org_code = %s),
                       %s, '带指标来源', 'active', 'department', %s, %s, NULL, 1)""",
            (new_id, src_code, f"metric-src-{uuid4().hex[:8]}",
             descriptor.get("schema_version", "0.2"), Jsonb(descriptor)))
    return new_id


# ---------------------------------------------------------------------------
# 3. 岗位必须是本部门的；来源必须真的开放给这个人
# ---------------------------------------------------------------------------


def test_copy_refuses_a_position_from_another_department(site):
    world = _world()
    slug = f"copy-badpos-{uuid4().hex[:10]}"
    target = world["target"]["org_unit_id"]
    opener = _logged_in(site, "ADMIN001")
    _discard(slug)
    status, html, _ = _call(opener, site,
                            f"/departments/{target}/copy"
                            f"?based_on={world['source']['interface_id']}")
    assert status == 200, status
    status, _, headers = _call(
        opener, site, f"/departments/{target}/copy", method="POST",
        form={"csrf": _csrf(html), "source_id": str(world["source"]["interface_id"]),
              "slug": slug, "name": "不该建出来的一版",
              "position_id": str(world["foreign"]["position_id"])})
    assert status == 303, status
    assert "error=" in headers.get("location", ""), headers.get("location")
    assert _row_by_slug(slug) is None, "岗位不是本部门的，页面却建出来了"


def test_copy_refuses_a_source_that_is_not_offered_to_this_person(site):
    """表单里换一个来源编号不能变成「能复制任意一页」。

    权限在提交时重新判：来源必须出现在这个人自己的参考清单里（清单只列已启用的
    页）。这里拿一张**草稿页**和一个**不存在的编号**去试——两个编号都真实可用，
    但都不是「开放给你的来源」。
    """

    world = _world()
    target = world["target"]["org_unit_id"]
    slug = f"copy-denied-{uuid4().hex[:10]}"
    opener = _logged_in(site, "ADMIN001")
    draft_id = _make_draft_source(world)
    try:
        for based_on in (draft_id, str(uuid4())):
            status, body, headers = _call(
                opener, site, f"/departments/{target}/copy?based_on={based_on}")
            assert status == 303, (based_on, status, body[:200])
            assert "error=" in headers.get("location", ""), (based_on, headers.get("location"))
            assert 'name="source_id"' not in body, "不该开放的来源照样渲染出了复制表单"

        status, body, headers = _call(
            opener, site, f"/departments/{target}/copy", method="POST",
            form={"csrf": "0" * 64, "source_id": draft_id, "slug": slug,
                  "name": "不该建出来的一版",
                  "position_id": str(world["position"]["position_id"])})
        assert _row_by_slug(slug) is None, "越权的来源居然真的复制出来了"
    finally:
        _discard(slug)
        _drop_page(draft_id)


def _make_draft_source(world: dict) -> str:
    """在来源部门造一张草稿页：编号真实存在，但不在任何人的参考清单里。"""

    from psycopg.types.json import Jsonb

    from oams.db import connection

    descriptor = json.loads(json.dumps(world["source"]["descriptor"]))
    new_id = str(uuid4())
    descriptor["interface_id"] = new_id
    descriptor["name"] = "草稿来源"
    with connection() as conn:
        conn.execute(
            """INSERT INTO oams.interfaces
                   (interface_id, org_unit_id, slug, name, status, visibility, schema_version,
                    descriptor, position_id, version_no)
               VALUES (%s, (SELECT org_unit_id FROM oams.org_units WHERE org_code = %s),
                       %s, '草稿来源', 'draft', 'department', %s, %s, NULL, 1)""",
            (new_id, world["source"]["source_code"], f"draft-src-{uuid4().hex[:8]}",
             descriptor.get("schema_version", "0.1"), Jsonb(descriptor)))
    return new_id


def _drop_page(interface_id: str) -> None:
    from oams.db import connection

    with connection() as conn:
        conn.execute("DELETE FROM oams.interface_entries WHERE interface_id = %s", (interface_id,))
        conn.execute("DELETE FROM oams.interfaces WHERE interface_id = %s", (interface_id,))
