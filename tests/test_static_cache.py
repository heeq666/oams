"""静态资源的版本串与缓存头。

改完样式的人刷新一次还是旧样子，是最常见也最没道理的浪费时间：浏览器按自己的
心情缓存 css，而「上线了吗」不该靠 Ctrl+Shift+R 才能回答。这里的四条规则：

1. 模板引用的静态地址带版本串，版本串是**文件内容的哈希**，不是手写常量；
2. 内容一变，地址就变——变的是地址，所以浏览器没得可选，只能回源；
3. 地址上的版本串跟当前文件对得上，就可以长缓存（内容永远不变，immutable）；
4. 对不上、或者根本没带版本串（旧缓存里的残页、有人手打的地址），就 `no-cache`，
   让浏览器每次先问一句，而不是自己猜。

"""

from __future__ import annotations

import re

import pytest

import oams.api  # noqa: F401  —— 先装配好路由，TestClient 才拿得到 app
from starlette.testclient import TestClient

from oams import web

CSS = "oams-modules.css"


@pytest.fixture
def client() -> TestClient:
    return TestClient(oams.api.app)


@pytest.fixture
def static_dir(tmp_path, monkeypatch):
    """一份临时静态目录，用来验证「改一个字节换一个地址」。"""
    monkeypatch.setattr(web, "STATIC_DIR", tmp_path)
    return tmp_path


def test_a_static_url_carries_the_hash_of_the_file_contents():
    version = web.static_version(CSS)
    assert re.fullmatch(r"[0-9a-f]{8}", version), version
    assert web.static_url(CSS) == f"/static/{CSS}?v={version}"
    # 两个不同文件不该算出同一个版本串，否则一个改了两处都换地址（或者都不换）。
    assert web.static_url("oams-collab.css") != web.static_url(CSS)


def test_one_edited_byte_changes_the_url(static_dir):
    target = static_dir / CSS
    target.write_text("a { color: red }", encoding="utf-8")
    before = web.static_url(CSS)

    target.write_text("a { color: blue }", encoding="utf-8")
    after = web.static_url(CSS)

    assert before.startswith(f"/static/{CSS}?v=")
    assert after.startswith(f"/static/{CSS}?v=")
    assert before != after, "内容改了地址没变，浏览器就会继续拿旧的"


def test_a_missing_file_gets_no_invented_version(static_dir):
    assert web.static_version("nope.css") == ""
    # 说不出版本就不带参数，让下面那条「没带就 no-cache」的规则接手；
    # 编一个假版本串会让它被长缓存，那才是真的回不来了。
    assert web.static_url("nope.css") == "/static/nope.css"


def test_a_versioned_url_may_be_cached_forever(client):
    response = client.get(web.static_url(CSS))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_a_bare_url_must_be_rechecked_every_time(client):
    response = client.get(f"/static/{CSS}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"


def test_a_stale_version_string_falls_back_to_rechecking(client):
    """旧缓存里的残页带着老版本串：内容已经变了，不能让它长缓存。"""
    response = client.get(f"/static/{CSS}?v=00000000")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"


def test_the_page_links_what_the_helper_returns(client):
    """页面上的地址必须就是助手函数给的那个，否则版本串白加。

    模板那七处引用归界面，她把它们换成 `{{ static_url(...) }}` 之后这条才有意义；
    哪天有人回退成写死的 `/static/...`，这条会红——那正是要提醒的事。
    """
    response = client.get("/login")
    assert response.status_code == 200
    html = response.text
    for relative in (CSS, "oams-collab.css", "vendor/htmx.min.js", "brand/logo-login.png"):
        assert web.static_url(relative) in html, relative
