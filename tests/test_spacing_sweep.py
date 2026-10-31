"""间距收口之后，两个「只有真数据才出现」的行。

2026-10-31 全站把 21 处档外的 6px 收进 8（`f7ff703`）。复核时发现其中两处
今天渲染不出来，也就是说值被改回 6 也没有一条用例会红：

* `.wh-link`（证据行带链接）——证据区没有 `evidence_url` 时整条链接不渲染；
* 审核卡的改动行 ——首版没有可对比的在线版本，一条改动行也不出，而审核卡真正
  用的类是 `.diff-row`（不是样式表里那条 `.diff-list`）。

这个文件把两件事一起钉住：真数据下这两个类确实渲染出来，且它们之间的间距就是
文档里的 8。值改回 6 精确红一条；模板不再渲染这个类也精确红一条。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

import pytest

from oams.render import render_interface

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULES_CSS = REPO_ROOT / "app" / "oams" / "static" / "oams-modules.css"
BASE_TEMPLATE = REPO_ROOT / "app" / "oams" / "templates" / "base.html"
TEMPLATE_DIR = REPO_ROOT / "app" / "oams" / "templates"

_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _declarations(body: str) -> dict[str, str]:
    decls: dict[str, str] = {}
    for chunk in body.split(";"):
        prop, sep, value = chunk.partition(":")
        if not sep:
            continue
        name = prop.strip().lower()
        if name:
            decls[name] = re.sub(r"\s+", " ", value).strip()
    return decls


def _rules(css: str) -> list[tuple[tuple[str, ...], dict[str, str]]]:
    """把 CSS 拆成 (选择器表, 声明) 两层，@media 里的一并拍平。

    故意写得比 `tests/test_module_css_tokens.py` 那套解析器薄：这里只要按选择器
    取一条声明，不需要区分顶层与嵌套。
    """

    text = _COMMENT.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), css)
    found: list[tuple[tuple[str, ...], dict[str, str]]] = []
    index, length, buf = 0, len(text), []
    while index < length:
        char = text[index]
        if char == "{":
            prelude = "".join(buf).strip()
            buf = []
            depth, cursor = 1, index + 1
            while cursor < length and depth:
                if text[cursor] == "{":
                    depth += 1
                elif text[cursor] == "}":
                    depth -= 1
                cursor += 1
            body = text[index + 1 : cursor - 1]
            index = cursor
            if prelude.startswith("@"):
                found.extend(_rules(body))
            elif prelude:
                selectors = tuple(s.strip() for s in prelude.split(",") if s.strip())
                found.append((selectors, _declarations(body)))
        elif char == "}":
            index += 1
        else:
            buf.append(char)
            index += 1
    return found


def _decls_for(css: str, selector: str) -> dict[str, str]:
    """某个选择器**原样**出现在哪条规则里，就取那条规则的声明。"""

    hits = [decls for selectors, decls in _rules(css) if selector in selectors]
    assert len(hits) == 1, (
        f"{selector} 应恰好出现在一条规则里，实际 {len(hits)} 条"
        f"（若拆成多条，间距就不再是一处可断言的值）"
    )
    return hits[0]


def _style_block(path: Path) -> str:
    """模板里内联 `<style>` 的内容（外壳样式全写在 base.html 里）。"""

    html = path.read_text(encoding="utf-8")
    match = re.search(r"<style[^>]*>(.*?)</style>", html, re.S)
    assert match, f"{path} 里找不到内联样式块"
    return match.group(1)


# --- 证据行：链接与尾随图标之间的 8 -------------------------------------------

DESCRIPTOR = {
    "schema_version": "0.2",
    "interface_id": "b2f3a1c0-0000-4000-8000-0000000000aa",
    "name": "间距收口探针",
    "org_unit_code": "DESIGN",
    "modules": [{"type": "evidence_gallery",
                 "source_fields": ["title", "evidence_url", "note"]}],
    "fields": [
        {"key": "f_title", "label": "款号", "type": "text", "map_to": "title",
         "required": True, "example": "AW26-001"},
        {"key": "f_evidence", "label": "依据", "type": "text", "map_to": "evidence",
         "example": "SY-2610"},
        {"key": "f_url", "label": "证据链接", "type": "link", "map_to": "evidence_url",
         "example": "https://example.com/sheet.pdf"},
        {"key": "f_note", "label": "备注", "type": "text", "map_to": "note",
         "example": "面料已到"},
    ],
    "status_map": {"待打样": "planned", "已完成": "done"},
}

VALUES = {
    "f_title": "AW26-001",
    "f_evidence": "SY-2610",
    "f_url": "https://example.com/sheet.pdf",
    "f_note": "面料已到仓",
}


def test_an_evidence_link_renders_with_its_row_gap() -> None:
    """有链接时证据行真的渲染出 `.wh-link`，且图标与文字之间是 8。"""

    html = render_interface(DESCRIPTOR, VALUES)
    link = re.search(r'<a class="wh-link"[^>]*href="([^"]+)"', html)
    assert link, "证据区没有渲染出 .wh-link，下面那条间距断言就成了空检：" + html[:400]
    assert link.group(1) == "https://example.com/sheet.pdf", link.group(1)
    assert 'class="wh-icon wh-icon-external"' in html, "尾随图标没渲染，图标与文字之间的间距无从谈起"

    decls = _decls_for(MODULES_CSS.read_text(encoding="utf-8"), ".wh-link")
    assert decls.get("gap") == "8px", (
        ".wh-link 的 gap 是链接文字与尾随图标之间的距离，行内档就是 8；"
        f"实际 {decls.get('gap')!r}"
    )
    assert decls.get("margin-top") == "8px", (
        f".wh-link 与上一段之间也是 8；实际 {decls.get('margin-top')!r}"
    )


def test_the_evidence_row_disappears_without_a_link() -> None:
    """没有链接时整条不渲染 —— 这正是这条规则一直没被测到的原因。"""

    html = render_interface(DESCRIPTOR, {**VALUES, "f_url": ""})
    assert "wh-link" not in html, "没有证据链接时不该出现链接行：" + html[:400]


# --- 审核卡的改动行 -----------------------------------------------------------

# 用 tests/fixtures/ 里的快照，别读 `data/live-design-*.json`（被 .gitignore，clone 下来没有）。
BASE_DESCRIPTOR = json.loads((REPO_ROOT / "tests" / "fixtures" / "live-design-interface.json").read_text(encoding="utf-8"))
PROBE_SLUG = "spacing-sweep-probe"


def _no_redirect():
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    return NoRedirect()


def _session(base: str, token: str):
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _no_redirect(),
        urllib.request.HTTPCookieProcessor(CookieJar()))

    def call(path: str, method: str = "GET", form: dict | None = None):
        payload = urllib.parse.urlencode(form).encode() if form is not None else None
        request = urllib.request.Request(base + path, data=payload, method=method)
        if payload is not None:
            request.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with opener.open(request, timeout=20) as response:
                return response.status, response.read().decode(), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode(), dict(error.headers)

    call("/login", "POST", {"token": token})
    return call


def _token_of(html: str, name: str = "csrf") -> str:
    match = re.search(r'name="' + name + r'"\s+value="([^"]+)"', html)
    assert match, "form token " + name + " missing from page"
    return match.group(1)


@pytest.mark.db
def test_a_review_card_with_changes_renders_its_change_rows(site) -> None:
    """两版并存时审核卡真的画出改动行，外壳里那些间距才有对象可施。"""

    import psycopg
    from psycopg.rows import dict_row

    from oams.security import issue_session

    url = os.environ["OAMS_DATABASE_URL"]
    with psycopg.connect(url, row_factory=dict_row) as conn:
        admin = conn.execute(
            "SELECT member_id FROM oams.workforce_members WHERE employee_code = 'ADMIN001'"
        ).fetchone()["member_id"]
        # 审的人必须不是作者：队列不把作者自己的提交给他看。
        reviewer = conn.execute(
            "SELECT member_id FROM oams.workforce_members WHERE employee_code = 'DESIGN001'"
        ).fetchone()["member_id"]
        org = conn.execute(
            "SELECT org_unit_id FROM oams.org_units WHERE org_code = 'DESIGN'"
        ).fetchone()["org_unit_id"]
        conn.execute("DELETE FROM oams.interfaces WHERE slug = %s", (PROBE_SLUG,))
        conn.execute(
            """INSERT INTO oams.interfaces
                   (org_unit_id, slug, name, status, visibility, schema_version, descriptor,
                    version_no, created_by, updated_by)
               VALUES (%s, %s, %s, 'active', 'department', '0.2', %s, 1, %s, %s)""",
            (org, PROBE_SLUG, "间距收口探针", json.dumps(BASE_DESCRIPTOR), admin, admin))
        # 下一版把状态词删掉一个：这属于「会改变公司数字含义」的高危改动，
        # 审核卡上因此既有改动行、也有需注意的提示。
        nxt = json.loads(json.dumps(BASE_DESCRIPTOR))
        nxt["status_map"] = {"待打样": "planned", "打样中": "in_progress", "已完成": "done"}

    try:
        token, _ = issue_session(str(admin))
        call = _session(site, token)
        review_token, _ = issue_session(str(reviewer))
        review = _session(site, review_token)

        status, form, _ = call(f"/departments/{org}/new")
        assert status == 200, status
        status, _, headers = call(f"/departments/{org}/new", "POST", {
            "csrf": _token_of(form),
            "slug": PROBE_SLUG,
            "name": "间距收口探针",
            "position_id": "",
            "descriptor_text": json.dumps(nxt, ensure_ascii=False),
        })
        assert status == 303 and "saved=1" in headers.get("location", ""), headers.get("location", "")

        with psycopg.connect(url, row_factory=dict_row) as conn:
            draft = conn.execute(
                """SELECT interface_id FROM oams.interfaces
                   WHERE slug = %s AND status = 'draft'""", (PROBE_SLUG,)).fetchone()["interface_id"]
        status, page, _ = call(f"/departments/{org}/{draft}")
        assert status == 200, status
        status, _, headers = call(f"/departments/{org}/{draft}/submit-review", "POST",
                                  {"review_csrf": _token_of(page, "review_csrf")})
        assert status == 303 and "saved=review" in headers.get("location", ""), \
            headers.get("location", "")

        status, html, _ = review("/reviews")
        assert status == 200, status
        assert "这一版改了什么" in html, "审核卡没画出改动摘要：" + html[:400]
        assert 'class="diff-row"' in html, (
            "审核卡没有改动行 —— 下面那些间距断言就成了空检；"
            "改动行真用的类是 .diff-row（不是样式表里的 .diff-list）"
        )
        assert 'class="diff-head"' in html, "改动摘要的抬头没渲染"
        assert "等面料" in html, "删掉的那个状态词没被点出来，说明改动行不是真数据渲染的"

        shell = _style_block(BASE_TEMPLATE)
        head = _decls_for(shell, ".diff-head")
        assert head.get("margin") == "0 0 8px", (
            f"改动摘要抬头与下面各行之间是 8；实际 {head.get('margin')!r}"
        )
        row = _decls_for(shell, ".diff-row")
        assert row.get("gap") == "12px", f"改动行里「级别/部门/正文」三列之间是 12；实际 {row.get('gap')!r}"
    finally:
        with psycopg.connect(url, row_factory=dict_row) as conn:
            conn.execute("DELETE FROM oams.interfaces WHERE slug = %s", (PROBE_SLUG,))


def test_no_template_emits_the_old_diff_list_selector() -> None:
    """`.diff-list` 是死选择器：审核卡早改成 `.diff-row` 了。

    这条不是「留着没用也无所谓」——2026-10-31 那次间距收口改的正是 `.diff-list`
    的 gap，改完渲染上零效果。留着这条断言，谁真把它接回模板就会在这里红一次，
    提醒他先确认间距再上线；不想要这条样式就该整条删掉。
    """

    # 只看标记，不看样式块：这条规则自己的定义就写在 base.html 的 <style> 里。
    users = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in TEMPLATE_DIR.rglob("*.html")
        if "diff-list" in re.sub(r"<style[^>]*>.*?</style>", "", path.read_text(encoding="utf-8"), flags=re.S)
    ]
    assert users == [], (
        "有模板开始用 .diff-list 了：" + ", ".join(users) +
        "；先删掉这条断言，再给它补一条间距断言"
    )
