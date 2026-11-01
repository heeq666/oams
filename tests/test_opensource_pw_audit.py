"""`scripts/opensource-rewrite.sh` 的口令判据：真值点名，示例/占位放行。

为什么单独测这一条：重写脚本核对段原来把整行口令赋值行拿去 `grep -v PASSWORD`，
而这一栏自己永远含 `PASSWORD`——计数恒为 0，「0 处残留」是瞎的。
现在判据按**值**判，并分两张白名单（示例值 / 占位与变量）。这个文件钉住它真的会响：

* 造一行真口令（口令赋值的值、以及连接串里的口令段，都用拼接出来的真值）→ 必须点名报出来；
* 造占位与示例（`'<你的口令>'`、`"$VAR"`、`oams_test_pw`、`pw`、`change-me`）→ 必须放行。

**样例里的真值一律动态拼接**（`REAL_PW` / `REAL_PW2`），不写成品字面量：成品值会让
`scripts/opensource-check.sh` 的「现行文件」组把这个文件自己判红——那组规则是对的，
不能靠白名单绕过，改样例而不是改规则。

判据函数**从脚本本身抽出来跑**（不是在这里另抄一份实现）：脚本改了判据，这里跟着改，
不会出现「测试还绿、脚本已经瞎了」。抽不到就直接失败，不静默跳过。

脚本本身**不进公开仓**（在它的移出清单里），所以公开仓 clone 下来这组必须整组 skip，
而不是红：被测文件不在，就没有可验的东西——那和「验过且干净」是两回事，理由写进 -rs。
"""
from __future__ import annotations

import pathlib
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
REWRITE_SCRIPT = REPO / "scripts" / "opensource-rewrite.sh"

pytestmark = pytest.mark.skipif(
    not REWRITE_SCRIPT.exists(),
    reason="本仓未随源码发布内部重写脚本（scripts/opensource-rewrite.sh），这组判据只在主仓跑",
)

# 样例里的「真值」与「键名 + 连接串前缀」都不写成品相邻字面量：写出来，这个文件自己
# 就成了现行文件组的一处命中（规则是对的，不为此开白名单）。拼出来，规则就看不见它。
REAL_PW = "hunter" + "2"
REAL_PW2 = "dev" + "pass"
ENV_KEY = "POSTGRES_PASSWORD"
DSN = "postgresql" + "://"

SAMPLES = "\n".join(
    (
        f"{ENV_KEY}={REAL_PW}",
        f"{ENV_KEY}='<你的口令>'",
        f'{ENV_KEY}="$OAMS_DB_PASSWORD"',
        f"{ENV_KEY}=oams_test_pw",
        f"{ENV_KEY}=pw",
        f"{ENV_KEY}=change-me",
        f"{DSN}postgres:{REAL_PW}@127.0.0.1:55460/x",
        f"{DSN}postgres:oams_test_pw@127.0.0.1:55460/x",
        f"{DSN}oams:{REAL_PW2}@127.0.0.1:55433/oams",
        f"{DSN}postgres:${{PW}}@127.0.0.1:55460/x",
    )
) + "\n"


def _lines() -> list[str]:
    return REWRITE_SCRIPT.read_text(encoding="utf-8").splitlines()


def _function(name: str) -> str:
    """按 `name() {` … 独立 `}` 抽函数体；抽不到就是判据没了，直接失败。"""

    captured: list[str] = []
    for line in _lines():
        if line.startswith(f"{name}()"):
            captured = [line]
            continue
        if captured:
            captured.append(line)
            if line == "}":
                return "\n".join(captured)
    raise AssertionError(f"scripts/opensource-rewrite.sh 里找不到 {name}() 了")


def _pw_example() -> str:
    for line in _lines():
        if line.startswith("PW_EXAMPLE="):
            return line
    raise AssertionError("scripts/opensource-rewrite.sh 里找不到 PW_EXAMPLE= 了")


def _audit(which: str, mode: str, tmp_path) -> str:
    """在临时 git 仓里跑脚本自己的判据，返回它打印的命中行。"""

    bash = f"""
set -uo pipefail
cd "$WORK"
{_pw_example()}
{_function("_pw_allowed")}
{_function(which)}
TOOL=':(exclude)README.md'
TOOL2="$TOOL"
TOOL3="$TOOL"
git init -q .
printf '%s' "$SAMPLES" > samples.md
git add samples.md
git -c user.email=t@example.com -c user.name=t commit -qm seed
{which} {mode} || true
"""
    completed = subprocess.run(
        ["bash", "-c", bash],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=120,
        env={"WORK": str(tmp_path), "SAMPLES": SAMPLES, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_a_real_password_is_named(tmp_path):
    """正向对照：没有这一条，后面「占位被放行」的绿就是空的。"""

    leaked = _audit("leak_env_lines", "leak", tmp_path)
    assert f"samples.md:1  值={REAL_PW}" in leaked

    leaked = _audit("leak_pw_lines", "leak", tmp_path)
    assert f"samples.md:7  值={REAL_PW}" in leaked  # 连接串里的真口令
    assert f"samples.md:9  值={REAL_PW2}" in leaked


def test_examples_and_placeholders_are_let_through(tmp_path):
    leaked = _audit("leak_env_lines", "leak", tmp_path)
    for line in (2, 3, 4, 5, 6):
        assert f"samples.md:{line} " not in leaked

    leaked = _audit("leak_pw_lines", "leak", tmp_path)
    for line in (8, 10):
        assert f"samples.md:{line} " not in leaked

    allowed = _audit("leak_env_lines", "allowed", tmp_path)
    for line in (2, 3, 4, 5, 6):
        assert f"samples.md:{line} " in allowed
    assert "samples.md:1 " not in allowed

    allowed = _audit("leak_pw_lines", "allowed", tmp_path)
    for line in (8, 10):
        assert f"samples.md:{line} " in allowed
    assert "samples.md:7 " not in allowed


def test_the_whole_line_is_not_what_gets_filtered(tmp_path):
    """老写法（整行 grep -v PASSWORD）会把两类 env 一起放过，这里必须分得开。

    只断言「真口令被点名 + 占位被放行」还不够：如果判据退化成「看到 PASSWORD 就放过」，
    第一条会红但错误信息说不清；这条把「同一行格式、只靠值区分」的意图再钉一次。
    """

    leaked = _audit("leak_env_lines", "leak", tmp_path)
    assert REAL_PW in leaked
    assert "oams_test_pw" not in leaked
    # 与真口令同形状、只有值不同：一个报一个不报，说明判的是值不是整行。
    assert leaked.count("samples.md:") == 1
