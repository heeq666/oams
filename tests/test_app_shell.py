"""Every page uses the same shell: departments on the left, globals on top."""
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path


import pytest  # noqa: E402  (test-module wrapper)


pytestmark = pytest.mark.db  # needs a live PostgreSQL and a served app


def _script() -> None:
    """Body of the original standalone e2e script, unchanged."""

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
    import psycopg
    from psycopg.rows import dict_row
    from oams.security import issue_session

    base = os.environ.get("OAMS_BASE", "http://127.0.0.1:8000")
    failures = []

    def check(name, ok, detail=""):
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else "  " + str(detail)))
        if not ok:
            failures.append(name)

    with psycopg.connect(os.environ["OAMS_DATABASE_URL"], row_factory=dict_row) as conn:
        admin = conn.execute("SELECT member_id FROM oams.workforce_members WHERE employee_code='ADMIN001'").fetchone()["member_id"]
        lead = conn.execute("SELECT member_id FROM oams.workforce_members WHERE employee_code='DESIGN001'").fetchone()["member_id"]
        org = conn.execute("SELECT org_unit_id FROM oams.org_units WHERE org_code='DESIGN'").fetchone()["org_unit_id"]

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    def session(member_id):
        token, _ = issue_session(str(member_id))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect,
                                             urllib.request.HTTPCookieProcessor(CookieJar()))

        def call(path, method="GET", form=None):
            payload = urllib.parse.urlencode(form).encode() if form is not None else None
            req = urllib.request.Request(base + path, data=payload, method=method)
            if payload is not None:
                req.add_header("Content-Type", "application/x-www-form-urlencoded")
            try:
                with opener.open(req, timeout=15) as resp:
                    return resp.status, resp.read().decode(), dict(resp.headers)
            except urllib.error.HTTPError as err:
                return err.code, err.read().decode(), dict(err.headers)

        call("/login", "POST", {"token": token})
        return call

    def shell(html):
        """Return (has_rail, rail_names, top_links)."""
        has_rail = 'class="app-rail"' in html
        rail = re.search(r'<aside class="app-rail".*?</aside>', html, re.S)
        names = re.findall(r'class="rail-item[^"]*"[^>]*>(?:\s*<span[^>]*></span>)?\s*([^<]+?)\s*</a>',
                           rail.group(0)) if rail else []
        top = re.search(r"<nav[^>]*>(.*?)</nav>", html, re.S)
        links = re.findall(r">([^<]+)<", top.group(1)) if top else []
        return has_rail, [n.strip() for n in names], [l.strip() for l in links if l.strip()]

    admin_call = session(admin)
    lead_call = session(lead)

    def follow(call, path, limit=5):
        """Departments redirect twice (to the department, then to its first page)."""
        for _ in range(limit):
            status, html, headers = call(path)
            if status != 302:
                return status, html
            target = headers.get("location", "")
            if target.startswith("http"):
                target = target.split("8000", 1)[-1]
            if not target:
                return status, html
            path = target
        return status, html

    for label, call, path in (
        ("departments", lead_call, "/departments"),
        ("settings", admin_call, "/settings"),
        ("review queue", admin_call, "/reviews"),
        ("progress", admin_call, "/progress"),
    ):
        status, html = follow(call, path)
        has_rail, names, links = shell(html)
        check(label + " page uses the shared shell", status == 200 and has_rail, status)

    status, html, _ = admin_call("/settings")
    has_rail, names, links = shell(html)
    check("the settings column lists its own views",
          has_rail and names == ["部门与岗位", "岗位目录", "工作项管理",
                                 "审计记录", "试用与开关"], names)
    check("admin top bar keeps globals", links == ["进度中心", "汇报中心", "页面管理", "设置"], links)

    status, html, _ = admin_call("/progress")
    has_rail, names, links = shell(html)
    check("the progress column leads with the company overview, then departments",
          has_rail and names[:1] == ["公司总览"] and "设计部" in names[1:], names)

    # 左侧栏是**二级菜单**：菜单项必须是真页面，不能是同一页里的锚点。以前三条都指
    # /reviews 加 #decided / #catalog，而「待审核」那条正好就是那一页本身，于是另外
    # 两条读起来像被塞进了它里面（用户原话：「为什么最近决定和各部门页面会在待审核里」）。
    def rail_items(html):
        aside = re.search(r'<aside class="app-rail".*?</aside>', html, re.S)
        block = aside.group(0) if aside else ""
        return re.findall(r'<a href="([^"]+)" class="rail-item([^"]*)"', block)

    views = (("/reviews", "/reviews"), ("/reviews/settings", "/reviews/settings"),
             ("/reviews/decided", "/reviews/decided"))
    for path, own in views:
        status, html, _ = admin_call(path)
        items = rail_items(html)
        hrefs = [href for href, _ in items]
        on = [href for href, cls in items if "on" in cls]
        _, names, _ = shell(html)
        check(path + " is its own page, not an anchor on the queue",
              status == 200
              and hrefs == ["/reviews", "/reviews/settings", "/reviews/decided"]
              and not any("#" in h for h in hrefs), hrefs)
        # 名字是老大定的：这一条叫「页面设置」（以前叫「各部门页面」）。
        check(path + " names its views the way the owner asked",
              names == ["待审核", "页面设置", "最近决定"], names)
        check(path + " highlights exactly its own item", on == [own], on)
    status, queue, _ = admin_call("/reviews")
    status2, catalog, _ = admin_call("/reviews/settings")
    status3, decided, _ = admin_call("/reviews/decided")
    # 用标题判，不用「这几个字有没有出现」：待审卡里也有一句「会记进最近决定」。
    settings_head = '<h2 style="margin:0">页面设置</h2>'
    decided_head = '<h2 style="margin:0">最近决定</h2>'
    check("page settings lives on its own page only",
          status2 == 200 and settings_head in catalog and "谁能看" in catalog
          and settings_head not in queue,
          (status2, settings_head in queue))
    check("the decision list lives on its own page only",
          status3 == 200 and decided_head in decided and decided_head not in queue,
          (status3, decided_head in queue))

    status, html = follow(lead_call, "/departments")
    has_rail, names, links = shell(html)
    # A reviewer who manages no department is staff: the column shows the company
    # overview and the one department they belong to -- nothing else.
    check("staff column shows the company overview and their own department",
          has_rail and names == ["公司总览", "设计部"], names)
    check("staff top bar keeps the pages they may read",
          links == ["进度中心", "汇报中心", "页面管理"], links)
    check("department tabs are inside the content area, not the sidebar",
          'class="page-tabs"' in html and names.count("设计部") == 1, names)

    if failures:
        raise AssertionError("failed checks: " + ", ".join(failures))
    print("")
    print("all shell checks passed")


def test_app_shell(site) -> None:
    """Run the original standalone script as a pytest test."""

    _script()
