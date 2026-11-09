"""同一个权限格子，两处说法必须是同一句话。

「只拿到参考权」这件事在界面上出现在两个地方：弹窗「说明」列（`web._access_summary`）
和列表那一格（`web._granted_text`）。两边各自拼句子，曾经拼出「只能作参考，看不到里面的
数据」和「只可作参考，看不到数据」两种写法——同一个意思两个说法，读者还得停下来比较它们
是不是一回事，而 768 下那一格只有 78px（约 6 个汉字），长的那句还会多折一行。

这组用例不钉「必须是哪一句」，只钉**两处一致**：谁要再改其中一处，这里会红，逼他同时看
一眼另一处。句子本身对不对是产品判断，归负责人；一致性是结构问题，归测试。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import oams.api  # noqa: E402,F401  先导 api：它负责把 web 的 router 挂上，反过来会循环
from oams import web  # noqa: E402

REFERENCE_ONLY = {"can_read": False, "can_write": False, "can_base": True}


def _dialog_sentence() -> str:
    """弹窗那一行：`_access_summary` 收的是带三格的 dict（没有 is_owner/closed_reason）。"""
    return web._access_summary(dict(REFERENCE_ONLY))


def _list_sentence() -> str:
    return web._granted_text(dict(REFERENCE_ONLY))


def test_both_places_say_the_same_thing_about_reference_only():
    dialog, listed = _dialog_sentence(), _list_sentence()
    assert dialog == listed, (
        f"「只可作参考」现在有两种说法：弹窗写「{dialog}」，列表写「{listed}」。"
        f"同一个格子两句话，读者要停下来比较；768 下那一格只有 78px，长的那句还会折行。")


def test_the_sentence_says_the_data_is_out_of_reach():
    """这句话存在的唯一理由就是「看不到数据」：删掉它读者会以为点得开。"""
    sentence = _list_sentence()
    assert "参考" in sentence, sentence
    assert "看" in sentence, sentence


def test_readable_rows_do_not_get_the_reference_only_sentence():
    """有查看权的人不该看到「看不到数据」——那是给拿不到数据的人看的。"""
    readable = web._granted_text({"can_read": True, "can_write": False, "can_base": True})
    assert "看不到" not in readable, readable
    assert "可查看" in readable and "可作参考" in readable, readable


def test_nothing_granted_stays_a_dash_in_both_places():
    """一格都没给时两处都是「—」：这里不该出现任何一句话。"""
    assert web._granted_text({"can_read": False, "can_write": False, "can_base": False}) == "—"
    assert web._access_summary({"can_read": False, "can_write": False, "can_base": False}) == "—"
