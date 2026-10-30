"""Load, check and preview plugin descriptors from the command line.

The department administrator is a developer colleague, not a platform insider.
These three commands are the whole local loop:

    workhub plugin check examples/production-dept.yaml
    workhub plugin preview examples/production-dept.yaml
    workhub plugin upgrade old-0.1-interface.json --out upgraded.json

All three are offline: no database, no network. A descriptor is data, so checking
one is a pure function over a file -- which is what makes it safe to run in an
editor hook, in CI, or in a pre-commit. `upgrade` moves a schema 0.1 descriptor
onto the 0.2 module vocabulary; it prints the machine-made decisions as hint
lines so a person can confirm them.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from workhub import descriptor as rules


class PluginFileError(Exception):
    """The file could not be read as a descriptor at all (syntax, encoding)."""


def load_descriptor(path: str | Path) -> dict[str, Any]:
    """Read a .yaml/.yml/.json descriptor into a plain dict."""
    file = Path(path)
    if not file.is_file():
        raise PluginFileError(f"{file}: 文件不存在")
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as exc:
        raise PluginFileError(f"{file}: 读不了（{exc}）") from exc

    suffix = file.suffix.lower()
    if suffix == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PluginFileError(f"{file}:{exc.lineno}: JSON 语法错误：{exc.msg}") from exc
    else:
        try:
            import yaml  # imported lazily so a JSON-only install still works
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise PluginFileError("需要 pyyaml 才能读 YAML：pip install pyyaml") from exc
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            where = f"{file}:{mark.line + 1}:{mark.column + 1}" if mark else str(file)
            problem = getattr(exc, "problem", exc)
            raise PluginFileError(f"{where}: YAML 语法错误：{problem}") from exc

    if not isinstance(data, dict):
        raise PluginFileError(f"{file}: 顶层必须是一个对象（map），实际是 {type(data).__name__}")
    return data


def check_file(path: str | Path) -> list[str]:
    """Return every problem with the descriptor at *path*. Empty list means it can go up.

    A plugin file is installed from a shell, with no review page in between, so
    the file has to carry its own sign-off: `require_recorded_approval` is what
    makes "approved" a recorded fact here instead of a sentence the author typed
    about themselves.
    """
    try:
        descriptor = load_descriptor(path)
    except PluginFileError as exc:
        return [str(exc)]
    return rules.validate_descriptor(descriptor, require_recorded_approval=True)


def render_report(path: str | Path, problems: list[str]) -> str:
    """A check report a person can act on."""
    lines = [f"检查 {path}"]
    if not problems:
        lines.append("  OK 通过。字段映射、状态映射、模块版本、容器深度都没问题。")
        return "\n".join(lines)
    lines.append(f"  FAIL 发现 {len(problems)} 条问题：")
    for index, problem in enumerate(problems, start=1):
        lines.append(f"  {index}. {problem}")
    lines.append("")
    lines.append("  改完再跑一次 check；只报错不说怎么改的地方请提给平台维护人。")
    return "\n".join(lines)


def preview_html(descriptor: dict[str, Any]) -> str:
    """Render the descriptor with sample data, offline."""
    from workhub.render import render_preview

    return render_preview(descriptor, sample=True)


def preview_file(path: str | Path) -> str:
    return preview_html(load_descriptor(path))


def upgrade_file(path: str | Path) -> tuple[dict[str, Any], list[str]]:
    """Read a descriptor and move it onto schema 0.2. Returns (upgraded, hint lines)."""
    from workhub.upgrade import upgrade_descriptor

    return upgrade_descriptor(load_descriptor(path))


def render_upgrade_report(path: str | Path, upgraded: dict[str, Any],
                          hints: list[str]) -> str:
    """An upgrade report a person can act on: what the tool decided, and what to confirm."""
    lines = [
        f"升级 {path}",
        f"  schema_version -> {upgraded.get('schema_version')}",
        f"  模块 {len(upgraded.get('modules') or [])} 个，"
        f"能力 {len(upgraded.get('capabilities') or [])} 条，"
        f"指标 {len(upgraded.get('metrics') or [])} 条",
        f"  需要人工确认 {len(hints)} 条：",
    ]
    for index, hint in enumerate(hints, start=1):
        lines.append(f"  {index}. {hint}")
    lines.append("")
    lines.append("  确认完改好的文件，再跑一次 check；渲染不出来先看 check 的第一条。")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Entry point for the plugin subcommand group."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="workhub plugin", description="离线校验与预览部门插件")
    verbs = parser.add_subparsers(dest="verb", required=True)
    for name, help_text in (
        ("check", "静态校验：字段映射、状态映射、模块版本、容器深度"),
        ("preview", "本地渲染预览（假数据，不连数据库）"),
        ("upgrade", "把 schema 0.1 的描述升级到 0.2（含模块词汇表迁移）"),
    ):
        sub = verbs.add_parser(name, help=help_text)
        sub.add_argument("path", help="descriptor 文件路径（.yaml / .yml / .json）")
        sub.add_argument("--json", action="store_true", help="以 JSON 输出，便于接编辑器与 CI")
        sub.add_argument(
            "--out",
            help="preview 时写 HTML、upgrade 时写升级后的 descriptor（不写就打 stdout）",
        )

    args = parser.parse_args(argv)

    if args.verb == "upgrade":
        try:
            upgraded, hints = upgrade_file(args.path)
        except PluginFileError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if args.out:
            Path(args.out).write_text(
                json.dumps(upgraded, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(render_upgrade_report(args.path, upgraded, hints))
            print(f"\n升级后的描述已写入 {args.out}")
        elif args.json:
            payload = {
                "path": str(args.path),
                "ok": not any(h.startswith("转换后仍有校验问题：") for h in hints),
                "descriptor": upgraded,
                "hints": hints,
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(render_upgrade_report(args.path, upgraded, hints))
            print("\n升级后的描述：")
            print(json.dumps(upgraded, ensure_ascii=False, indent=2))
        return 0

    if args.verb == "check":
        problems = check_file(args.path)
        if args.json:
            payload = {"path": str(args.path), "ok": not problems, "problems": problems}
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(render_report(args.path, problems))
        return 0 if not problems else 1

    problems = check_file(args.path)
    if problems:
        # A preview of an invalid descriptor would teach the wrong thing.
        print(render_report(args.path, problems), file=sys.stderr)
        return 1
    try:
        html = preview_file(args.path)
    except PluginFileError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.out:
        Path(args.out).write_text(html, encoding="utf-8")
        print(f"预览已写入 {args.out}")
    elif args.json:
        print(json.dumps({"path": str(args.path), "ok": True, "html": html}, ensure_ascii=False))
    else:
        print(html)
    return 0
