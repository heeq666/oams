"""模块层 CSS 令牌锁：把照 Figma 核过的间距/尺寸值钉成断言。

上一轮照着 `docs/design/design-system-v1.md` 与 Figma 模块层（主库 `140:3438`）
逐个核了 13 处值，但核完只写进了 CSS 注释里：谁手滑把 `.wh-tab` 的 height 从 30
改回 32，全量 pytest 依然是绿的，只有人再看一遍才能发现。这个文件把那些值变成红绿。

做法是**真的把 CSS 解析成 (选择器 → 声明) 再取值**，不是"文件里出现过这串字符"：

- 把属性挪进别的规则、改选择器名、把声明删掉换默认值 → 都会红；
- `.wh-status-row` 的 gap 与 margin-bottom 分开断言，改一个不会连带掩盖另一个；
- `.wh-evidence` / `.wh-meta` 要求确实合写在同一条规则里（它们是同一档次要文本，
  拆开就会漂成两个值），`.wh-note` 单独一条规则，也一并锁 top。

纯静态：不起浏览器、不连数据库、不进 `db` 标记，随时可跑（毫秒级）。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

import pytest

CSS_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "workhub"
    / "static"
    / "workhub-modules.css"
)

_COMMENT = re.compile(r"/\*.*?\*/", re.S)


class Rule(NamedTuple):
    """一条 CSS 规则：所在的 at-rule 上下文（顶层为 None）+ 选择器 + 声明。"""

    at: str | None
    selectors: tuple[str, ...]
    decls: dict[str, str]

    @property
    def where(self) -> str:
        scope = f"{self.at} 里" if self.at else "顶层"
        return f"{scope}的 {', '.join(self.selectors)}"


def _blank_comments(css: str) -> str:
    """把注释换成等长空白（保留换行，行号仍然对得上）。"""
    return _COMMENT.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), css)


def _declarations(body: str) -> dict[str, str]:
    decls: dict[str, str] = {}
    for chunk in body.split(";"):
        prop, sep, value = chunk.partition(":")
        if not sep:
            continue
        name = prop.strip().lower()
        if not name:
            continue
        decls[name] = re.sub(r"\s+", " ", value).strip()
    return decls


def _parse(text: str, at: str | None, out: list[Rule]) -> None:
    """把一段 CSS 拆成规则列表；@media 之类递归进去并记下上下文。"""
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
                _parse(body, prelude, out)
            elif prelude:
                selectors = tuple(s.strip() for s in prelude.split(",") if s.strip())
                out.append(Rule(at, selectors, _declarations(body)))
        elif char == "}":
            index += 1
        else:
            buf.append(char)
            index += 1


def _load_rules() -> list[Rule]:
    rules: list[Rule] = []
    _parse(_blank_comments(CSS_PATH.read_text(encoding="utf-8")), None, rules)
    return rules


RULES: list[Rule] = _load_rules()
TOP_LEVEL: list[Rule] = [r for r in RULES if r.at is None]


# --- 取值助手：读的是解析出来的声明，不是文件里的子串 -------------------------


def top_rules(selector: str) -> list[Rule]:
    """顶层所有把 `selector` 原样列进选择器表的规则。"""
    return [r for r in TOP_LEVEL if selector in r.selectors]


def only_rule(selector: str) -> Rule:
    found = top_rules(selector)
    assert len(found) == 1, (
        f"{selector} 在顶层应恰好有一条规则，实际 {len(found)} 条"
        f"（{', '.join(r.where for r in found) or '无'}）"
    )
    return found[0]


def declaration(rule: Rule, prop: str) -> str:
    assert prop in rule.decls, (
        f"{rule.where} 没有声明 {prop}；现有声明：{', '.join(sorted(rule.decls))}"
    )
    return rule.decls[prop]


def px(rule: Rule, prop: str) -> float:
    """取一个长度值。写成别的单位（或算不出的表达式）都算红。"""
    return length(rule, prop, declaration(rule, prop))


def length(rule: Rule, prop: str, raw: str) -> float:
    """CSS 里 `0` 可以不带单位，其余长度必须是 px。"""
    if raw == "0":
        return 0.0
    match = re.fullmatch(r"(-?\d+(?:\.\d+)?)px", raw)
    assert match, f"{rule.where} 的 {prop} 应为 px 长度，实际是 {raw!r}"
    return float(match.group(1))


def edges(rule: Rule, prop: str) -> dict[str, str]:
    """把 margin/padding 简写拆成 top/right/bottom/left 四个原始值。"""
    parts = declaration(rule, prop).split()
    assert 1 <= len(parts) <= 4, f"{rule.where} 的 {prop} 不是合法简写：{parts}"
    if len(parts) == 1:
        top = right = bottom = left = parts[0]
    elif len(parts) == 2:
        top = bottom = parts[0]
        right = left = parts[1]
    elif len(parts) == 3:
        top, right, bottom = parts
        left = right
    else:
        top, right, bottom, left = parts
    return {"top": top, "right": right, "bottom": bottom, "left": left}


def side_px(rule: Rule, prop: str, side: str) -> float:
    """取 margin / padding 的某一边：有简写就拆简写，没有就读单边长写。

    模块层两种写法都有（`.wh-note` 用 `margin: 8px 0 0`，`.wh-link` 用
    `margin-top: 8px`），断言不该因为改写法而误判，但值必须一致。
    """
    if prop in rule.decls:
        raw = edges(rule, prop)[side]
        return length(rule, f"{prop} 的 {side}", raw)
    longhand = f"{prop}-{side}"
    assert longhand in rule.decls, (
        f"{rule.where} 既没有 {prop} 简写也没有 {longhand}；"
        f"现有声明：{', '.join(sorted(rule.decls))}"
    )
    return length(rule, longhand, rule.decls[longhand])


# --- 解析器自检：证明下面那些断言真的读得懂 CSS，不是空过 --------------------


def test_the_css_file_exists_and_is_the_one_we_parse():
    assert CSS_PATH.is_file(), f"找不到模块层样式表：{CSS_PATH}"
    assert CSS_PATH.stat().st_size > 10_000, "样式表小得不像话，先确认没被截断"


def test_parser_reads_rules_and_nested_at_rules():
    """解析结果要覆盖顶层与 @media 内部两层，否则下面的选择器断言会凭空变绿。"""
    assert len(TOP_LEVEL) >= 120, f"只解析出 {len(TOP_LEVEL)} 条顶层规则，解析器可能漏块"
    nested = [r for r in RULES if r.at]
    assert nested, "一条 @media 内的规则都没解析到：递归解析坏了"
    assert any(r.at and r.at.startswith("@media") for r in nested)
    # 具体抽查：@media print 里那条 .wh-container 必须落在嵌套层，不能混进顶层，
    # 否则 .wh-container 会看起来有两条不同的内距。
    print_scope = [
        r for r in nested if r.at and "print" in r.at and ".wh-container" in r.selectors
    ]
    assert print_scope, "@media print 里的 .wh-container 没被识别"
    assert declaration(print_scope[0], "background") == "none"


def test_parser_distinguishes_comments_from_declarations():
    """把值写进注释不算数：解析器只认花括号里的声明。"""
    sample = """
    /* 卡片内距 16（这条注释里的 16 不该被当成声明） */
    .demo {
      /* padding: 99px; 注释里的声明也不算 */
      padding: 16px;
    }
    @media (max-width: 900px) { .demo { padding: 24px; } }
    """
    rules: list[Rule] = []
    _parse(_blank_comments(sample), None, rules)
    top = [r for r in rules if r.at is None]
    assert len(top) == 1
    assert declaration(top[0], "padding") == "16px", "注释里的 padding 被当成了真声明"
    nested = [r for r in rules if r.at]
    assert declaration(nested[0], "padding") == "24px"


def test_every_locked_selector_resolves_to_a_real_rule():
    """13 处令牌涉及的选择器都要能在样式表里找到，改类名不能悄悄绕过断言。"""
    selectors = {
        ".wh-card-title",
        ".wh-tabbar",
        ".wh-tab",
        ".wh-divider-label",
        ".wh-note",
        ".wh-evidence",
        ".wh-meta",
        ".wh-status-row",
        ".wh-rank-rows",
        ".wh-rank-rows > li",
        ".wh-link",
        ".wh-note-text",
        ".wh-container",
    }
    missing = sorted(s for s in selectors if not top_rules(s))
    assert not missing, f"这些选择器在顶层样式表里找不到：{missing}"


# --- 被锁的 13 处令牌 -------------------------------------------------------


def test_card_title_bottom_margin_is_12():
    """模块卡片标题与内容之间 12（Figma 模块 / card 148:5953 的纵向 itemSpacing）。"""
    rule = only_rule(".wh-card-title")
    assert side_px(rule, "margin", "bottom") == 12
    assert side_px(rule, "margin", "top") == 0


def test_tab_chip_is_30_high_with_12px_side_padding():
    """Tab 芯片对齐控件基准线 30，内距 0·12，行高 18。"""
    rule = only_rule(".wh-tab")
    assert px(rule, "height") == 30
    assert side_px(rule, "padding", "left") == 12
    assert side_px(rule, "padding", "right") == 12
    assert side_px(rule, "padding", "top") == 0
    assert side_px(rule, "padding", "bottom") == 0
    assert px(rule, "line-height") == 18


def test_tabbar_gap_is_8():
    rule = only_rule(".wh-tabbar")
    assert px(rule, "gap") == 8


def test_divider_label_bottom_margin_is_8():
    rule = only_rule(".wh-divider-label")
    assert side_px(rule, "margin", "bottom") == 8


def test_note_evidence_meta_share_top_margin_8():
    """三条次要文本统一「与上一段 8」：note 自成一条，evidence 与 meta 合写。"""
    note = only_rule(".wh-note")
    assert side_px(note, "margin", "top") == 8
    assert side_px(note, "margin", "right") == 0
    assert side_px(note, "margin", "bottom") == 0

    shared = [r for r in TOP_LEVEL if {".wh-evidence", ".wh-meta"} <= set(r.selectors)]
    assert len(shared) == 1, (
        ".wh-evidence 与 .wh-meta 应合写在同一条规则里（同一档次要文本），"
        f"实际匹配 {len(shared)} 条"
    )
    rule = shared[0]
    assert side_px(rule, "margin", "top") == 8
    assert side_px(rule, "margin", "right") == 0
    assert side_px(rule, "margin", "bottom") == 0


def test_status_row_gap_and_bottom_margin_are_8():
    """状态行两个属性分别锁：行内间距 8，行与行 8，末行不留白。"""
    rule = only_rule(".wh-status-row")
    assert px(rule, "gap") == 8
    assert side_px(rule, "margin", "bottom") == 8

    last = only_rule(".wh-status-row:last-child")
    assert side_px(last, "margin", "bottom") == 0


def test_rank_rows_gap_is_8_both_levels():
    """排行表：行与行 8，行内序号与名称 8。"""
    rows = only_rule(".wh-rank-rows")
    assert px(rows, "gap") == 8
    item = only_rule(".wh-rank-rows > li")
    assert px(item, "gap") == 8


def test_link_top_margin_is_8():
    rule = only_rule(".wh-link")
    assert side_px(rule, "margin", "top") == 8


def test_note_text_line_height_is_20():
    """正文 13/20（Figma note_block 148:6322）。"""
    rule = only_rule(".wh-note-text")
    assert px(rule, "line-height") == 20


def test_container_padding_is_16():
    """模块容器内距 16：合并规则与单独规则都必须是 16，不能有一条漏改。"""
    found = top_rules(".wh-container")
    assert len(found) >= 2, f".wh-container 应有合并与单独两条规则，实际 {len(found)}"
    for rule in found:
        assert side_px(rule, "padding", "top") == 16, f"{rule.where} 内距不对"
        assert side_px(rule, "padding", "right") == 16, f"{rule.where} 内距不对"
        assert side_px(rule, "padding", "bottom") == 16, f"{rule.where} 内距不对"
        assert side_px(rule, "padding", "left") == 16, f"{rule.where} 内距不对"


@pytest.mark.parametrize(
    "selector",
    [
        ".wh-card-title",
        ".wh-tabbar",
        ".wh-tab",
        ".wh-divider-label",
        ".wh-note",
        ".wh-status-row",
        ".wh-rank-rows",
        ".wh-rank-rows > li",
        ".wh-link",
        ".wh-note-text",
        ".wh-container",
    ],
)
def test_locked_rules_use_tokens_not_literal_colour_or_radius(selector):
    """顺带守住硬规则：这些模块规则里不许写死颜色，圆角只能走变量。"""
    rules = top_rules(selector)
    assert rules, f"{selector} 没有顶层规则"
    for rule in rules:
        for prop, value in rule.decls.items():
            if prop in {"color", "background", "border", "border-color", "outline"}:
                assert "#" not in value, f"{rule.where} 的 {prop} 写死了颜色：{value}"
                assert "rgb(" not in value, f"{rule.where} 的 {prop} 写死了颜色：{value}"
            if prop.startswith("border-radius"):
                assert value.startswith("var(--wh-radius"), (
                    f"{rule.where} 的 {prop} 没走圆角令牌：{value}"
                )
