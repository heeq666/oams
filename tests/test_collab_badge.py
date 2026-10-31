"""未读徽章上的字数：到 99 写数字，再往上写 99+。

徽章固定 16 高、宽度靠内容 HUG，所以字数是视觉约束，不只是文案问题：一位
16、两位 20、「99+」28。真实条数另有去处（aria-label 与面板头「共 N 条未
读」），徽章只负责一眼看出「有多少没看」。
"""

import oams.api  # noqa: F401  -- web imports api；单独 import web 是半成品模块
from oams.web import _badge_count


def test_zero_and_empty_render_nothing():
    # 空字符串会让 :empty 的 CSS 生效，于是徽章整个消失——这是有意的。
    assert _badge_count(0) == ""
    assert _badge_count(None) == ""
    assert _badge_count("") == ""


def test_counts_below_a_hundred_are_written_out():
    assert _badge_count(1) == "1"
    assert _badge_count(9) == "9"
    assert _badge_count(99) == "99"


def test_a_hundred_and_above_collapse_to_ninety_nine_plus():
    assert _badge_count(100) == "99+"
    assert _badge_count(1234) == "99+"


def test_junk_does_not_leak_onto_the_badge():
    # 未读数来自 SQL count，正常不会脏；脏了也宁可空着，不要把异常文本画到圆点上。
    assert _badge_count("不是数字") == ""
    assert _badge_count([1]) == ""


def test_negative_is_treated_as_nothing():
    assert _badge_count(-3) == ""


def test_string_digits_from_templates_still_work():
    assert _badge_count("12") == "12"
