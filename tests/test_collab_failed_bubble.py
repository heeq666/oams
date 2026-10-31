"""发送失败那一下，这一句不能被吞掉。

htmx 默认把失败吞掉：断网（`htmx:sendError`）或 5xx（`htmx:responseError`）时
既不换目标也不提示，输入框被下一条话顶掉后，那句就没了——人以为发出去了。

这一份锁的是**服务端交给浏览器的契约**：气泡长什么样、红色感叹号给不给读屏器
交代、重发按钮是不是一个「不提交表单」的按钮、幂等键藏在哪、脚本在哪个钩子上
把它塞进请求、两种失败都接住了没有、重发是不是拿着同一个 id。这些都是从渲染
结果里读得出来的事实，不需要浏览器。

**不在这里的**：真的断网时脚本克隆气泡、把正文搬过去、点重发再把这一条发成功
——那要浏览器执行脚本。真正的前端路径验收在 `scripts/ego-collab-failure-check.mjs`
（ego 浏览器驱动，跑法：`ego-browser nodejs < scripts/ego-collab-failure-check.mjs`），
这里只保证它拿到的模板与钩子是对的。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest

import oams.api  # noqa: F401  —— 先装配好路由
from oams import web

#: 气泡模板直接渲染（不需要数据库，也不进 db 标记）。
MEMBER = {"display_name": "周岚"}
THREAD = {"thread_id": "00000000-0000-0000-0000-000000000001"}


def render(name: str, **context: object) -> str:
    return web.TEMPLATES.get_template(name).render(**context)


def bubble() -> str:
    return render("collab/failed.html", member=MEMBER)


def composer(*, draft: str = "", error: str | None = None) -> str:
    return render("collab/composer.html", member=MEMBER, thread=THREAD,
                  csrf="tok", placeholder="说一句", draft=draft, error=error)


class Tags(HTMLParser):
    """只收开标签（含属性）与它们的顺序；判断结构够用，不必建树。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, {k: (v or "") for k, v in attrs}))

    handle_startendtag = handle_starttag

    def find(self, tag: str, **attrs) -> list[dict[str, str]]:
        out = []
        for name, got in self.tags:
            if name != tag:
                continue
            if all(got.get(k) == v for k, v in attrs.items()):
                out.append(got)
        return out


def tags(html: str) -> Tags:
    parsed = Tags()
    parsed.feed(html)
    return parsed


# --------------------------------------------------------------------------- #
# 气泡本身
# --------------------------------------------------------------------------- #


def test_the_bubble_is_marked_as_a_failed_message():
    """一眼看得出「这条没发出去」：类名里带 failed，不是普通气泡换个颜色。"""

    article = tags(bubble()).find("article")
    assert len(article) == 1
    classes = article[0]["class"].split()
    assert "collab-msg" in classes
    assert "collab-msg-failed" in classes


def test_the_bubble_puts_an_exclamation_and_a_reason_on_one_line():
    """红色感叹号是装饰，原因才是内容；两者同一行，读屏器只念原因。"""

    parsed = tags(bubble())
    mark = parsed.find("span", **{"class": "collab-msg-fail-mark"})
    assert len(mark) == 1, "红色感叹号不见了"
    assert mark[0].get("aria-hidden") == "true", "装饰件要挡掉，否则读屏器念出「感叹号」"
    assert ">!</span>" in bubble(), "那个记号本身应当是 !"

    because = parsed.find("span", **{"data-failed-because": ""})
    assert len(because) == 1, "没有地方写失败原因"


def test_the_bubble_leaves_the_body_for_the_script_to_fill():
    """正文由脚本从输入框搬过来，模板里是空的——所以模板不能被当成「没有正文」。"""

    assert len(tags(bubble()).find("p", **{"data-failed-body": ""})) == 1
    assert re.search(r'data-failed-body[^>]*>\s*</p>', bubble()), "模板里不该预置正文"


def test_the_bubble_says_it_has_not_gone_out_yet():
    """时间那一格不能显示发送时间：这条根本没有发送时间。"""

    html = bubble()
    assert "还没发出去" in html
    assert MEMBER["display_name"] in html, "得说清是谁没发出去"


def test_the_bubble_offers_a_resend_button():
    """重发要是一个 type=button 的按钮：写成 submit 会把正文再发一遍、还不带 id。"""

    buttons = tags(bubble()).find("button", **{"data-resend": ""})
    assert len(buttons) == 1, "没有重发按钮"
    assert buttons[0]["type"] == "button", "重发不能是提交按钮"
    assert "重发" in bubble()


# --------------------------------------------------------------------------- #
# 输入框那一侧：幂等键怎么产生、怎么送出去
# --------------------------------------------------------------------------- #


def test_the_composer_ships_an_empty_idempotency_key():
    """空着表示「还没生成」；由脚本在第一次发送前填上，服务端当没带。"""

    keys = tags(composer()).find("input", name="client_msg_id")
    assert len(keys) == 1, "表单里没有幂等键，重发就会变成发第二条"
    assert keys[0]["type"] == "hidden"
    assert keys[0]["value"] == ""


def test_the_composer_embeds_the_bubble_as_a_template():
    """气泡只当模板用：脚本克隆它、填正文，不能一进来就出现在消息区里。"""

    html = composer()
    assert len(tags(html).find("template", id="collab-failed-tpl")) == 1
    inside = html.split('id="collab-failed-tpl"', 1)[1].split("</template>", 1)[0]
    assert "collab-msg-failed" in inside, "模板里装的应当就是那个气泡"


def test_the_composer_stamps_the_key_onto_the_request():
    """钩子得是 htmx 真正会发的那个：configRequest 时才改得动参数。

    只断言 `client_msg_id` 出现在凭 HTML 里是不够的——藏着不送出去，后端永远
    收不到，重发照样变两条。
    """

    html = composer()
    assert "htmx:configRequest" in html
    assert "parameters.client_msg_id" in html, "参数名写错，后端收不到这个键"


def test_the_composer_reacts_to_both_kinds_of_failure():
    """断网是 sendError，5xx 是 responseError，只接一种就会漏一半。"""

    html = composer()
    assert 'addEventListener("htmx:sendError"' in html
    assert 'addEventListener("htmx:responseError"' in html


def test_resending_reuses_the_id_of_the_failed_bubble():
    """重发是「把这一条发成功」，不是「再发一条」——所以 id 得从气泡上取回来。"""

    html = composer()
    assert '"data-client-msg-id"' in html, "气泡上没挂 id，重发就找不回这一条"
    resend = html.split("function resend(node)", 1)
    assert len(resend) == 2, "找不到重发逻辑"
    body = resend[1].split("function ", 1)[0]
    assert "data-client-msg-id" in body, "重发时没有复用原来的幂等键"


def test_editing_the_text_after_a_failure_is_a_new_message():
    """改过正文就是另一句话，不能把上一句的 id 占过来（否则会被去重掉）。"""

    html = composer()
    listener = html.split('addEventListener("input"', 1)
    assert len(listener) == 2, "没有「改正文换 id」的那段"
    assert "input.value = \"\"" in listener[1].split("})();", 1)[0]


@pytest.mark.parametrize("draft", ["", "一段没发出去的正文"])
def test_the_composer_shows_the_reason_above_the_box_only_when_there_is_one(draft):
    """失败原因贴着输入框出现；正常状态下不要凭空多一句提示。"""

    failed = composer(draft=draft, error="消息太长了，最多 4000 个字。")
    assert 'role="alert"' in failed
    assert "消息太长了" in failed
    quiet = composer(draft=draft)
    assert 'role="alert"' not in quiet
