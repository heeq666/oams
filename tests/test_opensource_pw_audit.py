"""`scripts/opensource-rewrite.sh` 的口令判据：真值点名，示例/占位放行。

为什么单独测这一条：重写脚本核对段原来把整行 `POSTGRES_PASSWORD=change-me 拿去
`grep -v PASSWORD`，而这一栏自己永远含 `PASSWORD`——计数恒为 0，「0 处残留」是瞎的。
现在判据按**值**判，并分两张白名单（示例值 / 占位与变量）。这个文件钉住它真的会响：

* 造一行真口令（`POSTGRES_PASSWORD=change-me 必须点名报出来；
* 造占位与示例（`'<你的口令>'`、`"$VAR"`、`oams_test_pw`、`pw`、`change-me`）→ 必须放行。

判据函数**从脚本本身抽出来跑**（不是在这里另抄一份实现）：脚本改了判据，这里跟着改，
不会出现「测试还绿、脚本已经瞎了」。抽不到就直接失败，不静默跳过。
"""
from __future__ import annotations

import pathlib
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "opensource-rewrite.sh"

SAMPLES = """\
POSTGRES_PASSWORD=change-me
POSTGRES_PASSWORD='<你的口令>'
POSTGRES_PASSWORD="$OAMS_DB_PASSWORD"
POSTGRES_PASSWORD=oams_test_pw
POSTGRES_PASSWORD=pw
POSTGRES_PASSWORD=change-me
postgresql://postgres:change-me@127.0.0.1:55460/x
postgresql://postgres:change-me@127.0.0.1:55460/x
postgresql://oams:change-me@127.0.0.1:55433/oams
postgresql://postgres:change-me@127.0.0.1:55460/x
"""


def _lines() -> list[str]:
    return SCRIPT.read_text(encoding="utf-8").splitlines()


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
    assert "samples.md:1  值=hunter2" in leaked

    leaked = _audit("leak_pw_lines", "leak", tmp_path)
    assert "samples.md:7  值=hunter2" in leaked  # 连接串里的真口令
    assert "samples.md:9  值=devpass" in leaked


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
    assert "hunter2" in leaked
    assert "oams_test_pw" not in leaked
    # 与真口令同形状、只有值不同：一个报一个不报，说明判的是值不是整行。
    assert leaked.count("samples.md:") == 1
