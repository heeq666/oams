"""表格单元格里「文字在前、胶囊/徽章在后」的写法守卫。

为什么要有这条：`base.html` 为了让胶囊里的**字**跟表头对齐，写了

    td > .pill:first-child, td > .badge:first-child { margin-left: -8px; }

左侧那 -8px 等于把胶囊左移它自己的内距。可 CSS 的 `:first-child` **不把文字节点
算作子元素**——所以「裸写名字 + 胶囊」的单元格（`<td>{{ c.name }} <span class="pill">本部门</span></td>`）
里，胶囊仍然被判成第一个子元素，被左移 8px，正好压住名字的第一个字。

2026-11-05 用户截图指出「红框那个胶囊挡住了左边的文字」，就是权限弹窗的部门列。
修法有两种，这里只守第二种：

1. 改 CSS —— 做不到。纯 CSS 无法表达「左边没有文字节点」。
2. **文字包一层 `<span>`** —— 胶囊就不再是第一个子元素，-8px 不生效，胶囊与名字
   保持正常的行内 8px。

所以这条用例扫描所有模板：单元格去掉前导空白与 Jinja 控制标签之后，如果第一个字符
是文字（不是 `<`），而这一格里出现了 pill / badge，就算违规。

判据（避免误报）：
- `{% if %}` / `{% endif %}` / `{# … #}` 这类标签不算文字；
- 单元格里先出现 `<div>`、`<a>`、`<strong>` 等其它元素不算（那些不是裸文字）；
- 只看 `td`，`th` 不放胶囊。
"""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "oams" / "templates"
TAG = re.compile(r"\{%-?.*?-?%\}|\{#.*?#\}", re.S)
CELL_WITH_PILL = re.compile(r"<span class=\"(?:pill|badge)")


def _bare_text_before_first_pill(cell: str) -> bool:
    """胶囊之前是不是只有裸文字（没有任何已闭合的元素）。

    只看第一个 pill/badge 之前的片段：去掉 Jinja 标签与空白后，
    - 空 → 胶囊本来就是第一个内容，合法（-8px 正是给它用的）；
    - 含 `>` → 前面已经有别的元素（如 `<div class="meta">`），胶囊不是 `:first-child`，不受影响；
    - 非空且不含 `>` → 前面是裸文字，胶囊会拿到 -8px、压住这段文字，判违规。
    """
    m = CELL_WITH_PILL.search(cell)
    if not m:
        return False
    prefix = TAG.sub("", cell[: m.start()])
    stripped = prefix.strip()
    if not stripped:
        return False
    return ">" not in prefix


def violations(html: str) -> list[str]:
    """返回这一份模板里所有「裸文字 + 胶囊/徽章」的 td 片段。"""
    found = []
    for m in re.finditer(r"<td>(.*?)</td>", html, re.S):
        cell = m.group(1)
        if _bare_text_before_first_pill(cell):
            found.append(re.sub(r"\s+", " ", cell).strip()[:80])
    return found


def test_no_bare_text_before_pill_in_any_template():
    offenders = {}
    for path in sorted(TEMPLATES.glob("*.html")):
        hits = violations(path.read_text(encoding="utf-8"))
        if hits:
            offenders[path.name] = hits
    assert not offenders, (
        "这些单元格是「裸文字 + 胶囊/徽章」，胶囊会被 td > .pill:first-child 的 -8px 压住文字；"
        f"把前面那段文字包进 <span> 再提交：{offenders}"
    )


def test_the_detector_actually_catches_the_bug():
    """正向对照：拿真的坏写法喂进去必须红，而且好写法必须不红。

    没有这条，上面那条「0 处」就是瞎的——扫描器写错一个字符，永远是 0 处。
    """
    bad = '<td>{{ c.name }} <span class="pill success">本部门</span></td>'
    assert violations(bad), "扫描器没认出坏写法，等于没有守卫"

    # 只在文字前面夹一个 Jinja 标签也算坏（标签不是子元素，更不是文字）
    bad2 = '<td>{% if c.is_owner %}{{ c.name }} <span class="pill success">本部门</span>{% endif %}</td>'
    assert violations(bad2)

    good = '<td><span>{{ c.name }}</span> <span class="pill success">本部门</span></td>'
    assert not violations(good)

    # 胶囊本身在单元格最前面（没有前置文字）是合法写法，-8px 正是给它用的
    good2 = '<td>{% if c.is_owner %}<span class="pill success">本部门</span>{% endif %}</td>'
    assert not violations(good2)

    # 前置是别的元素、不是裸文字，不受影响
    good3 = '<td><strong>{{ c.name }}</strong> <span class="pill">v3</span></td>'
    assert not violations(good3)
