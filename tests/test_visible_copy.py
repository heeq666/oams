"""界面可见文字里不许出现内部信息——这条只能靠机器守，写在规范里没人读。

判据（负责人 2026-11-05）：「把这句话删掉，读者是否仍然知道该做什么——仍然知道，
就必须删」。产品是给第三方部门用的，页面不是给作者看的。

所以这里扫的是**用户看得见的那部分文字**：去掉 HTML 标签、HTML 注释、Jinja 注释与
`{{ }}` / `{% %}`（变量值不是模板作者写的，另有用例管），再看剩下有没有：

- 仓库路径（`docs/architecture/…`、`app/oams/…`）；
- 文档/代码文件名（`*.md`、`*.py`）；
- 内部格式名与代码符号（descriptor、slug、emit、metrics、schema_version、map_to…）。

三个真撞过的实例（2026-11-05 之前 `grep` 就有，一直没人拦）：
`department_new_page.html` 印了规则文档路径；`department_workbench.html` 让用户
「在 descriptor 的 metrics 里加一条 emit」；`setup_interfaces.html` 把内部文件名当
链接文字。

允许出现的：JSON。它是用户在输入框里真正要粘的格式，不是我们的内部叫法。
"""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "oams" / "templates"

#: 只去掉「不会显示给用户」与「不是模板作者写的」部分，剩下就是要审的可见文字。
_HIDDEN = [
    (re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.S | re.I), " "),
    (re.compile(r"\{#.*?#\}", re.S), " "),
    (re.compile(r"<!--.*?-->", re.S), " "),
    (re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.S), " "),
    (re.compile(r"<[^>]+>", re.S), "\n"),
]

#: 内部信息的四类形状。命中的是「可见文字里的一串字符」，不是代码。
_FORBIDDEN = {
    "仓库路径": re.compile(r"\b(?:docs|app|scripts|tests|examples|skills|assets)/[\w/-]+"),
    "文档文件名": re.compile(r"[\w-]+\.md\b"),
    "代码文件名": re.compile(r"[\w-]+\.py\b"),
    "内部符号": re.compile(
        r"\b(?:descriptor|slug|emit|metrics|schema_version|map_to|rollup|jsonb)\b",
        re.I),
}

#: 允许的例外。JSON 是用户真要粘的格式；密钥与令牌不算内部信息。
_ALLOWED_WORDS = {"json"}


def visible_text(html: str) -> str:
    """模板原文 -> 只留用户看得见的静态文字，一行一段（行号只是示意，不做断言）。"""
    text = html
    for pattern, replacement in _HIDDEN:
        text = pattern.sub(replacement, text)
    return text


def violations(html: str) -> list[tuple[str, str]]:
    """返回 [(类别, 命中的字)], 空表就是干净。"""
    found: list[tuple[str, str]] = []
    for line in visible_text(html).splitlines():
        for chunk in re.split(r"[\s，。：；（）()、！？：\"'《》]+", line):
            word = chunk.strip()
            if not word:
                continue
            for label, pattern in _FORBIDDEN.items():
                for match in pattern.finditer(word):
                    if match.group(0).lower() in _ALLOWED_WORDS:
                        continue
                    found.append((label, match.group(0)))
    return found


def test_no_template_prints_internal_paths_or_symbols():
    """全量扫描：每个模板的可见文字里都不许有内部路径、文件名与代码符号。"""
    dirty: list[str] = []
    for path in sorted(TEMPLATES.glob("*.html")):
        for label, word in violations(path.read_text(encoding="utf-8")):
            dirty.append(f"{path.name}: [{label}] {word!r}")
    assert not dirty, "界面可见文字里有内部信息：\n" + "\n".join(dirty)


def test_the_scanner_actually_catches_the_three_real_instances():
    """正向对照：把当初真撞过的那三句塞回去，扫描器必须红——不红就是瞎的。

    没有这半条，上面那条「0 处」可能只是因为它从来不匹配任何东西。
    """
    samples = {
        "规则文档路径": '<p class="sub">规则见项目里的 <code>docs/architecture/interface-rules-and-modules.md</code>。</p>',
        "descriptor 与 metrics": '<p class="empty">在 descriptor 的 metrics 里加一条 emit，这里就会出现数字。</p>',
        "文件名当链接": '<p>规则见 <a href="/static">docs/architecture/interface-rules-and-modules.md</a>。</p>',
    }
    for label, html in samples.items():
        assert violations(html), f"扫描器漏了这类：{label}"


def test_jinja_output_and_comments_are_not_counted():
    """反向对照的另一半：变量值与注释不算「模板写死的文案」，不该被这条拦。

    `{{ }}` 里的东西是库里的数据（页面名、部门名）；`{# #}` 是写给自己看的。
    把它们算进来，这条就会变成「谁也不敢动模板」的噪音。
    """
    clean = ('{# 规则见 docs/architecture/x.md #}\n'
             '<p>{{ page.descriptor_name }} · {{ row.slug }}</p>\n'
             '<!-- 内部：app/oams/web.py:583 -->\n'
             '<p>页面描述（JSON）</p>')
    assert violations(clean) == []
