#!/usr/bin/env python3
"""比对「开源重写产物」与「主仓树」，把差异分成三类，只把第三类算失败。

用法：
    python3 scripts/opensource-tree-diff.py <重写仓路径> [主仓路径]

- 主仓默认取本脚本所在仓库；重写仓是 scripts/opensource-rewrite.sh 的产物。
- 移出清单从 scripts/opensource-rewrite.sh 的 PATHS+=( ... ) 里读，不在这里另抄一份，
  免得两处漂移（脚本改了、比对还在按老清单放行）。
- 脱敏规则从 scripts/opensource-redact.txt 与本机私有规则读（与重写脚本同一份来源）。

三类差异：
  1. 移出（预期）：主仓有、重写仓没有，且路径命中移出清单。
  2. 规则改写（预期）：两边都有、内容不同，但把主仓内容按规则替换后与重写仓逐字相同。
  3. 意外：其余一切（不在移出清单却丢了、多出来、内容不同且规则解释不了）。

退出码：0 = 只有预期差异（含零差异）；1 = 有意外差异；2 = 用法或环境错误。
"""
import re
import subprocess
import sys

PATH_BLOCK = re.compile(r"PATHS\+=\s*\((.*?)\)", re.S)
QUOTED = re.compile(r'"([^"]+)"')


def git(repo, *args, binary=False):
    out = subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, check=False
    )
    if out.returncode != 0:
        sys.stderr.write(out.stderr.decode("utf-8", "replace"))
        raise SystemExit(2)
    return out.stdout if binary else out.stdout.decode("utf-8", "replace")


def head_files(repo):
    return set(git(repo, "ls-tree", "-r", "--name-only", "HEAD").splitlines())


def blob(repo, path):
    return git(repo, "show", f"HEAD:{path}", binary=True)


def read_rules(repo):
    """返回 [(pattern, repl)]；literal 规则编译成 re.escape。"""
    sources = [f"{repo}/scripts/opensource-redact.txt"]
    for extra in (
        f"{repo}/tmp/opensource-rules.txt",
        f"{repo}/.artifacts/opensource-rules.txt",
    ):
        try:
            with open(extra, encoding="utf-8") as fh:
                if fh.read().strip():
                    sources.append(extra)
                    break
        except FileNotFoundError:
            continue
    rules = []
    for source in sources:
        try:
            text = open(source, encoding="utf-8").read()
        except FileNotFoundError:
            continue
        for line in text.splitlines():
            if not line.strip() or line.lstrip().startswith("#") or "==>" not in line:
                continue
            pattern, repl = line.split("==>", 1)
            if pattern.startswith("regex:"):
                pat = pattern[len("regex:"):]
            else:
                pat = re.escape(pattern)
            rules.append((pat, repl))
    return rules


def apply_rules(data, rules, per_line):
    """按 filter-repo 的两种可能语义各算一遍：整块替换 / 逐行替换。"""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if b"\x00" in data:
        return None
    if per_line:
        lines = text.splitlines(keepends=True)
        for pat, repl in rules:
            lines = [re.sub(pat, repl, line) for line in lines]
        return "".join(lines).encode("utf-8")
    for pat, repl in rules:
        text = re.sub(pat, repl, text)
    return text.encode("utf-8")


def moved_out(path, prefixes):
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def first_difference(a, b):
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return f"第 {i + 1} 字节起不同"
    return f"长度不同（{len(a)} vs {len(b)} 字节）"


def main():
    if len(sys.argv) < 2:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    rewritten = sys.argv[1].rstrip("/")
    here = __file__.rsplit("/scripts/", 1)[0]
    main_repo = (sys.argv[2] if len(sys.argv) > 2 else here).rstrip("/")

    rewrite_sh = subprocess.run(
        ["git", "-C", main_repo, "show", "HEAD:scripts/opensource-rewrite.sh"],
        capture_output=True,
    )
    if rewrite_sh.returncode != 0:
        print("读不到 scripts/opensource-rewrite.sh（主仓路径不对？）", file=sys.stderr)
        return 2
    prefixes = []
    for block in PATH_BLOCK.findall(rewrite_sh.stdout.decode("utf-8", "replace")):
        prefixes.extend(QUOTED.findall(block))

    rules = read_rules(main_repo)
    main_head = git(main_repo, "rev-parse", "--short", "HEAD").strip()
    new_head = git(rewritten, "rev-parse", "--short", "HEAD").strip()
    main_files, new_files = head_files(main_repo), head_files(rewritten)

    missing = sorted(main_files - new_files)
    extra = sorted(new_files - main_files)
    shared = sorted(main_files & new_files)

    dropped_ok = [p for p in missing if moved_out(p, prefixes)]
    dropped_bad = [p for p in missing if not moved_out(p, prefixes)]
    extra_bad = [p for p in extra if not moved_out(p, prefixes)]

    rewritten_ok, rewritten_bad = [], []
    for path in shared:
        old = blob(main_repo, path)
        new = blob(rewritten, path)
        if old == new:
            continue
        expected = apply_rules(old, rules, per_line=False)
        if expected is None:
            expected = apply_rules(old, rules, per_line=True)
        if expected == new:
            rewritten_ok.append(path)
            continue
        expected = apply_rules(old, rules, per_line=True)
        if expected == new:
            rewritten_ok.append(path)
            continue
        rewritten_bad.append((path, first_difference(expected or b"", new)))

    print(f"主仓   : {main_repo} @ {main_head}")
    print(f"重写仓 : {rewritten} @ {new_head}")
    print(f"移出清单: {len(prefixes)} 条路径前缀（读自 opensource-rewrite.sh）")
    print()

    print(f"[预期] 移出 {len(dropped_ok)} 个文件")
    for path in dropped_ok:
        print(f"    - {path}")
    print(f"[预期] 规则改写 {len(rewritten_ok)} 个文件")
    for path in rewritten_ok:
        print(f"    ~ {path}")
    print()

    failures = 0
    print(f"[意外] 不在移出清单却丢了：{len(dropped_bad)}")
    for path in dropped_bad:
        failures += 1
        print(f"    ! 缺 {path}")
    print(f"[意外] 重写仓多出来的文件：{len(extra_bad)}")
    for path in extra_bad:
        failures += 1
        print(f"    ! 多 {path}")
    print(f"[意外] 内容不同且规则解释不了：{len(rewritten_bad)}")
    for path, why in rewritten_bad:
        failures += 1
        print(f"    ! {path}（{why}）")
    print()

    if failures:
        print(f"结论：{failures} 处意外差异 —— 这份产物不能推。")
        return 1
    print("结论：差异全部可解释（移出清单 + 脱敏规则），可以推。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
