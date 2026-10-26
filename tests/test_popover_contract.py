"""浮层只有一个载体：触发器写 data-wh-popover，面板写 class="wh-popover"。

这条契约把三件事从各页面里收走了：面板挂哪儿（body）、摆在哪个坐标（fixed +
脚本量）、怎么关（脚本兜 mousedown / Esc / focusout，因为规范里没有遮罩）。

用例都是静态断言，不需要浏览器：谁把面板搬回 <details>、谁在面板类里重新写一遍
背景和投影、谁又手写一段定位脚本，这里立刻红。
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app" / "workhub" / "templates"
STATIC = ROOT / "app" / "workhub" / "static"
BASE = TEMPLATES / "base.html"


class _Tags(HTMLParser):
    """收集每个开始标签的属性，以及它当时被哪些标签包着。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.open: list[str] = []
        self.tags: list[tuple[str, dict[str, str], tuple[str, ...]]] = []
        self.ids: dict[str, dict[str, str]] = {}

    def handle_starttag(self, tag, attrs):
        attributes = {name.lower(): (value or "") for name, value in attrs}
        self.tags.append((tag.lower(), attributes, tuple(self.open)))
        if attributes.get("id"):
            self.ids[attributes["id"]] = attributes
        if tag.lower() not in {"area", "base", "br", "col", "embed", "hr", "img",
                               "input", "link", "meta", "source", "track", "wbr"}:
            self.open.append(tag.lower())

    def handle_endtag(self, tag):
        wanted = tag.lower()
        for index in range(len(self.open) - 1, -1, -1):
            if self.open[index] == wanted:
                del self.open[index:]
                return


def _markup(path: Path) -> str:
    """去掉 Jinja 注释与语句：注释里的 <details> 只是说明文字，不是节点。"""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"\{#.*?#\}", "", text, flags=re.S)
    return re.sub(r"\{%.*?%\}", "", text, flags=re.S)


@pytest.fixture(scope="module")
def base_tags() -> _Tags:
    parser = _Tags()
    parser.feed(_markup(BASE))
    return parser


def _has_class(attributes: dict[str, str], name: str) -> bool:
    return name in attributes.get("class", "").split()


def _popover_panels(parser: _Tags):
    for tag, attributes, ancestors in parser.tags:
        if _has_class(attributes, "wh-popover"):
            yield tag, attributes, ancestors


def test_every_trigger_points_at_a_popover_panel(base_tags):
    triggers = [(tag, attributes) for tag, attributes, _ in base_tags.tags
                if "data-wh-popover" in attributes]
    assert triggers, "base.html 里应该至少有一个 data-wh-popover 触发器"

    for tag, attributes in triggers:
        assert tag == "button", f"触发器要是 button，拿到了 <{tag}>：焦点与键盘语义靠它"
        target = attributes.get("aria-controls", "")
        assert target, f"触发器缺 aria-controls：{attributes}"
        panel = base_tags.ids.get(target)
        assert panel is not None, f"aria-controls={target} 指向的节点不存在"
        assert _has_class(panel, "wh-popover"), f"{target} 没有 wh-popover 类，样式会缺一整层"
        assert "hidden" in panel, f"{target} 初始应当 hidden，由载体打开"
        assert "fixed" in _css_block(".wh-popover")


def test_panels_hang_off_body_never_inside_details(base_tags):
    """<details> 是 backdrop root，玻璃在里面只会糊到空气——这是踩过的坑。"""

    panels = list(_popover_panels(base_tags))
    assert panels, "应当有 .wh-popover 面板"

    for tag, attributes, ancestors in panels:
        assert tag == "div", f"面板应当是 div，拿到了 <{tag}>"
        assert "details" not in ancestors, (
            f"面板 {attributes.get('id')} 长在 <details> 里，backdrop-filter 会失效"
        )
        assert ancestors == ("html", "body"), (
            f"面板 {attributes.get('id')} 被 {ancestors} 包着，只允许直接挂 body"
        )
        assert "hidden" in attributes


def test_no_page_writes_its_own_popover_plumbing():
    for path in sorted(TEMPLATES.glob("*.html")):
        assert "wireMenu" not in _markup(path), f"{path.name} 还留着旧的手写菜单逻辑"
    for path in sorted(STATIC.glob("*.js")):
        text = path.read_text(encoding="utf-8")
        assert "wireMenu" not in text, f"{path.name} 还留着旧的手写菜单逻辑"


def _css_block(selector: str, text: str | None = None) -> str:
    if text is None:
        text = BASE.read_text(encoding="utf-8")
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", text)
    assert match, f"base.html 里找不到 {selector} 的规则"
    return match.group(1)


def test_the_look_lives_in_one_rule_only():
    skin = _css_block(".wh-popover")
    assert "backdrop-filter" in skin
    assert "box-shadow" in skin
    assert "border-radius: var(--r-card)" in skin
    assert "z-index: var(--z-popover)" in skin
    assert "--z-popover:" in BASE.read_text(encoding="utf-8")

    for selector in (".account-menu", ".dev-menu", ".wh-list"):
        block = _css_block(selector)
        for property_name in ("backdrop-filter", "box-shadow", "background"):
            assert property_name not in block, (
                f"{selector} 又自己写了 {property_name}；皮肤只属于 .wh-popover"
            )


def test_the_carrier_loads_before_its_clients():
    head = BASE.read_text(encoding="utf-8")
    carrier = head.index("/static/wh-popover.js")
    assert carrier < head.index("/static/wh-select.js"), (
        "wh-popover.js 必须先加载：wh-select.js 展开时要在 window.whPopover 上挂面板"
    )


def test_the_select_delegates_to_the_carrier():
    text = (STATIC / "wh-select.js").read_text(encoding="utf-8")
    assert "window.whPopover.attach" in text, "下拉框应当把摆放与关闭交给载体"
    assert "backdrop-filter" not in text and "boxShadow" not in text, (
        "下拉框不该再自己上妆，长相归 .wh-popover"
    )
    assert "list.className = 'wh-list'" in text, "展开层自己的类只剩行密度"
    carrier = (STATIC / "wh-popover.js").read_text(encoding="utf-8")
    assert "classList.add('wh-popover')" in carrier, (
        "attach 时给面板补 wh-popover 类，展开层才与菜单同一档玻璃"
    )


def test_the_carrier_closes_without_a_scrim():
    """规范不落遮罩，所以关闭必须有脚本兜，四条路一条都不能少。"""

    text = (STATIC / "wh-popover.js").read_text(encoding="utf-8")
    assert "mousedown" in text
    assert "Escape" in text
    assert "focusout" in text
    assert "scroll" in text