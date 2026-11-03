"""审计理由必须是中文。

英文句子在「审计记录」页里就是一行看不懂的日志，而 `api.audit` 的 reason 是
唯一入口——所以这条用例直接扫源码，而不是等人一条条看：凡 `audit(...)` 的
reason 参数里的字符串字面量、以及 `issue_session(reason=...)` 的字面量，都必须
含中文字符（`[\\u4e00-\\u9fff]`）。调用方传进来的动态理由（`payload.reason`、
`note.strip()`）扫不到字面量，跳过——那不是我们能替它写的句子。

同一条规矩也守住「有人新写一句英文」这种回归：旧树上这批字面量全是英文，
所以它在旧树上必红。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

CJK = re.compile(r"[\u4e00-\u9fff]")
ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = (ROOT / "app", ROOT / "scripts")


def _reason_literal(expr: ast.expr) -> str | None:
    """把 reason 表达式里的字符串字面量拼起来（f-string 只取固定片段）。

    纯变量（`payload.reason`、`row["change_reason"]`）返回 None：那条理由由
    调用方给，不归源码管。所以不往 `Subscript` 里走——`row["change_reason"]`
    里的 `"change_reason"` 是字典键，不是给人看的句子。
    """
    pieces: list[str] = []

    def walk(node: ast.expr) -> None:
        if isinstance(node, ast.Subscript):
            walk(node.value)      # 只要被取的东西，不要键
            return
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            pieces.append(node.value)
            return
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.expr):
                walk(child)

    walk(expr)
    if not pieces:
        return None
    return "".join(pieces)


def _is_audit_call(node: ast.Call) -> bool:
    func = node.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
    return name == "audit"


def _is_issue_session(node: ast.Call) -> bool:
    func = node.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
    return name == "issue_session"


def find_violations(source: str, filename: str) -> list[str]:
    """返回「有字面量、但没有一个中文字符」的理由，形如 `文件:行 内容`。"""
    problems: list[str] = []
    tree = ast.parse(source)

    def check(expr: ast.expr, where: str) -> None:
        literal = _reason_literal(expr)
        if literal is None or CJK.search(literal):
            return
        problems.append(f"{filename}:{where} 审计理由没有中文：{literal.strip()!r}")

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _is_audit_call(node):
            # audit(conn, actor, event_type, subject_type, subject_id, reason, ...)
            if len(node.args) >= 6:
                check(node.args[5], str(getattr(node.args[5], "lineno", node.lineno)))
            for keyword in node.keywords:
                if keyword.arg == "reason":
                    check(keyword.value, str(getattr(keyword.value, "lineno", node.lineno)))
        elif _is_issue_session(node):
            for keyword in node.keywords:
                if keyword.arg == "reason":
                    check(keyword.value, str(getattr(keyword.value, "lineno", node.lineno)))

    # issue_session 的默认理由写在 def 上，不是调用点，单独看一遍：只看名为
    # reason 的那个参数，别把 provider="administrator-issued" 当成理由。
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "issue_session":
            continue
        args = node.args
        pairs = list(zip(args.args[len(args.args) - len(args.defaults):], args.defaults))
        pairs += list(zip(args.kwonlyargs, args.kw_defaults))
        for name, default in pairs:
            if name.arg != "reason" or default is None:
                continue
            if isinstance(default, ast.Constant) and isinstance(default.value, str):
                check(default, str(default.lineno))
    return problems


def _source_files() -> list[Path]:
    files: list[Path] = []
    for root in SOURCE_ROOTS:
        files.extend(sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts))
    return files


def test_every_audit_reason_is_chinese() -> None:
    problems: list[str] = []
    for path in _source_files():
        problems.extend(find_violations(path.read_text(encoding="utf-8"),
                                        str(path.relative_to(ROOT))))
    assert problems == [], "这些审计理由还是英文（审计页上没人看得懂）：\n" + "\n".join(problems)


def test_the_checker_catches_an_english_reason() -> None:
    """正向对照：塞一句英文进去必须报红，否则这条用例是瞎的。"""
    source = (
        "audit(conn, actor, 'interface.created', 'interface', row['interface_id'],\n"
        "      'created inside department workbench', row)\n"
    )
    problems = find_violations(source, "planted.py")
    assert len(problems) == 1
    assert "created inside department workbench" in problems[0]


def test_the_checker_accepts_chinese_and_dynamic_reasons() -> None:
    source = (
        "audit(conn, actor, 'interface.created', 'interface', row['interface_id'],\n"
        "      f'通过 {app_name} 代表 {who} 新建了页面草稿', row)\n"
        "audit(conn, actor, 'grant.created', subject_type, subject_id, payload.reason, row)\n"
        "issue_session(member_id, reason='由运维命令行签发了登录会话')\n"
    )
    assert find_violations(source, "ok.py") == []


def test_issue_session_default_reason_must_be_chinese() -> None:
    source = (
        "def issue_session(member_id, reason: str = 'session issued through trusted operator CLI'):\n"
        "    return member_id, reason\n"
    )
    problems = find_violations(source, "security.py")
    assert len(problems) == 1
    assert "session issued through trusted operator CLI" in problems[0]
