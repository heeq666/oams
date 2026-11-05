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

两条曾经把这条闸门装成「假 0」的漏洞（2026-11-05 补，出处见每个用例的 docstring）：

1. **只扫了顶层模板**。`TEMPLATES.glob("*.html")` 不递归，实际 `rglob` 是 60 个，
   漏掉 `modules/`（21 个）与 `collab/`（14 个）——正好是文案最密的两层。现在用
   `rglob`，并且有用例专门盯着「子目录里的违规必须被扫到」。
2. **`\\b` 对中文失效**。Python 的 `\\w` 包含汉字，所以「加一条emit」「在descriptor里」
   这种中文里最自然的连写，前后都没有词边界，`\\b` 一次也不匹配。现在边界只看 ASCII
   （`(?<![A-Za-z0-9_])` / `(?![A-Za-z0-9_])`）。

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

#: 词边界只看 ASCII。用 `\b` 时汉字算 `\w`，「加一条emit就会出数字」前后都没边界，
#: 于是整类命中被静默漏掉——中文界面里这才是最常见的写法（2026-11-05 实测）。
_AB = r"(?<![A-Za-z0-9_])"
_AA = r"(?![A-Za-z0-9_])"
#: 文件名本体只吃 ASCII：不这么写的话 `[\w-]+` 会把紧贴的汉字一起吞进命中串。
_NAME = r"[A-Za-z0-9_][A-Za-z0-9_.-]*"

#: 内部信息的四类形状。命中的是「可见文字里的一串字符」，不是代码。
_FORBIDDEN = {
    "仓库路径": re.compile(_AB + r"(?:docs|app|scripts|tests|examples|skills|assets)/[\w/-]+" + _AA),
    "文档文件名": re.compile(_AB + _NAME + r"\.md" + _AA),
    "代码文件名": re.compile(_AB + _NAME + r"\.py" + _AA),
    "内部符号": re.compile(
        _AB + r"(?:descriptor|slug|emit|metrics|schema_version|map_to|rollup|jsonb)" + _AA,
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


def collect_templates() -> list[Path]:
    """要扫的模板：**递归**。用 `glob` 只看顶层，会漏掉 modules/ 与 collab/ 两整层。"""
    return sorted(TEMPLATES.rglob("*.html"))


def scan(paths: list[Path]) -> list[str]:
    """扫一批模板，命中写成「相对路径: [类别] 字」——带路径，子目录里才不会分不清是谁。"""
    dirty: list[str] = []
    for path in paths:
        rel = path.relative_to(TEMPLATES).as_posix()
        for label, word in violations(path.read_text(encoding="utf-8")):
            dirty.append(f"{rel}: [{label}] {word!r}")
    return dirty


def test_no_template_prints_internal_paths_or_symbols():
    """全量扫描（含子目录）：可见文字里都不许有内部路径、文件名与代码符号。"""
    dirty = scan(collect_templates())
    assert not dirty, "界面可见文字里有内部信息：\n" + "\n".join(dirty)


def test_subdirectory_templates_are_scanned_not_just_the_top_level():
    """正向对照之「扫得到子目录」：往 modules/ 塞一条违规，全量扫描必须报出来。

    这条防的就是 2026-11-05 之前的形状——`glob("*.html")` 只扫顶层 25 个，子目录 35 个
    根本不进扫描，塞进 `modules/table.html` 的违规照样 3 passed（C经理实测复现）。
    探针文件用完即删，不留脏文件。
    """
    subdirs = [p for p in collect_templates() if p.parent != TEMPLATES]
    assert subdirs, ("模板树里一个子目录模板都没有？那 rglob 与 glob 就没区别了，"
                     "这条对照也就失去意义——先确认 app/oams/templates/ 的结构")

    probe = TEMPLATES / "modules" / "zz-visible-copy-probe.html"
    assert probe.parent.is_dir(), "modules/ 不存在，探针无处可放"
    probe.write_text('<p>在 descriptor 的 metrics 里加一条 emit。</p>', encoding="utf-8")
    try:
        dirty = scan(collect_templates())
    finally:
        probe.unlink()
    assert not probe.exists(), "探针文件没删掉"
    assert any("zz-visible-copy-probe.html" in row for row in dirty), (
        "子目录里的模板没被扫描：往 modules/ 塞了违规却报不出位置。\n"
        f"实际扫到的命中：{dirty}")


def test_cjk_glued_to_ascii_symbols_is_caught():
    """正向对照之「汉字紧贴英文」：中文里最自然的连写必须被抓到。

    `\\b` 在 Python 里把汉字当 `\\w`，所以「加一条emit」「在descriptor里」「把slug印上去」
    前后都没有边界，整类漏光（2026-11-05 实测三个都返回空表）。这是中文界面的常态写法，
    不是边角。
    """
    samples = {
        "汉字+emit": "加一条emit就会出数字",
        "汉字+descriptor": "在descriptor里加",
        "汉字+slug": "把slug印上去",
        "汉字+metrics": "看metrics",
        "汉字+仓库路径": "规则见docs/architecture/x.md",
        "汉字+文件名": "打开规则文档interface-rules.md看",
    }
    missed = [f"{label}（{text}）" for label, text in samples.items()
              if not violations(f"<p>{text}</p>")]
    assert not missed, "扫描器漏了「汉字紧贴英文」这类：\n" + "\n".join(missed)


def test_ascii_boundary_does_not_over_match():
    """反向对照：边界改成只看 ASCII，不能变成「见 prefix 就报」的噪音。

    `emitted` / `descriptors` / `slugger` 这些里面确实含那几个词，但不是内部符号本身；
    把整页判红会让这条闸门被绕过（谁都想加白名单）。
    """
    clean = ('<p>这一栏会自动补全。</p>'
             '<p>页面上不再出现 emitted 这种词，也没有 descriptors 之类的复数。</p>')
    assert violations(clean) == []


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
