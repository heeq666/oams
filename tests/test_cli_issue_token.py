"""`oams issue-token`：两种指人的写法等价，错法都给一行人话。

存在理由：说明书让运维跑 `issue-token --member-id <UUID>`，但页面上只有员工编号，
UUID 在哪都查不到——`--employee-code` 就是为这条死路加的。这里钉三件事：

* 两条路签出来的令牌，`GET /login?token=` 之后都能进 `/board`（不是只断言「退出码 0」）；
* 给错东西时**一行中文提示 + 非零退出**，不甩 traceback（照文档走的人只会看到这一行）；
* 两个参数同时给要报错，不替人挑一个——挑哪个都会让人以为另一个也生效了。

全部走真库（员工编号在 schema 里 UNIQUE）与真签到，不 mock `issue_session`。
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

import pytest

import conftest as suite

pytestmark = pytest.mark.db


def _run(*args: str, url: str) -> subprocess.CompletedProcess:
    """跑真 CLI（子进程），与 `oams issue-token` 在运维机器上看到的一样。"""

    env = dict(os.environ)
    env["OAMS_DATABASE_URL"] = url
    env["PYTHONPATH"] = str(suite.APP_DIR) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    return subprocess.run(
        [sys.executable, "-m", "oams.cli", "issue-token", *args],
        cwd=str(suite.REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _token(*args: str, url: str) -> str:
    completed = _run(*args, url=url)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)["access_token"]


def _open(url: str):
    """带 cookie 跟完重定向：`/login?token=` → `/board`。"""

    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        urllib.request.ProxyHandler({}),
    )
    try:
        with opener.open(url, timeout=15) as response:
            return response.status, response.read().decode("utf-8", "replace"), response.url
    except urllib.error.HTTPError as exc:  # 4xx 的正文要留给断言
        return exc.code, exc.read().decode("utf-8", "replace"), exc.url


def _no_traceback(completed: subprocess.CompletedProcess) -> None:
    assert "Traceback" not in completed.stderr
    assert "Traceback" not in completed.stdout


def test_both_ways_of_naming_the_member_sign_the_same_person(seeded_oams, site):
    """同一成员：编号签的、UUID 签的，都能从登录链接进到自己的看板。"""

    url = seeded_oams["url"]
    member = seeded_oams["database"].member_id("DESIGN001")

    by_id = _token("--member-id", str(member), url=url)
    by_code = _token("--employee-code", "DESIGN001", url=url)

    # 每次签发都是新会话，不是同一串；等价说的是「签到的是同一个人」。
    assert by_id != by_code

    for token in (by_id, by_code):
        status, body, final = _open(site + "/login?token=" + urllib.parse.quote(token))
        assert status == 200, (status, final)
        assert final.endswith("/board")
        assert "设计负责人" in body


def test_an_unknown_employee_code_is_one_line_not_a_traceback(seeded_oams):
    completed = _run("--employee-code", "NO-SUCH-4X", url=seeded_oams["url"])

    assert completed.returncode != 0
    assert "找不到员工编号" in completed.stderr
    assert "NO-SUCH-4X" in completed.stderr
    assert completed.stdout == ""  # 没签出任何东西
    _no_traceback(completed)


def test_giving_both_flags_is_refused_rather_than_guessed(seeded_oams):
    member = seeded_oams["database"].member_id("DESIGN001")
    completed = _run(
        "--member-id", str(member),
        "--employee-code", "DESIGN001",
        url=seeded_oams["url"],
    )

    assert completed.returncode != 0
    assert "只能给一个" in completed.stderr
    assert completed.stdout == ""
    _no_traceback(completed)


def test_a_member_id_that_is_not_a_uuid_gets_one_line(seeded_oams):
    completed = _run("--member-id", "DESIGN001", url=seeded_oams["url"])

    assert completed.returncode != 0
    assert "不是" in completed.stderr
    assert "--employee-code" in completed.stderr  # 指回能用的那条路
    assert completed.stdout == ""
    _no_traceback(completed)


def test_neither_flag_gives_a_hint(seeded_oams):
    completed = _run(url=seeded_oams["url"])

    assert completed.returncode != 0
    assert "缺成员" in completed.stderr
    assert "--employee-code" in completed.stderr
    _no_traceback(completed)
