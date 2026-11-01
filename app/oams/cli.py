import argparse
import json
import sys
from pathlib import Path
from uuid import UUID

import psycopg
import uvicorn
from psycopg.types.json import Jsonb

from oams.config import settings
from oams.db import connection
from oams.security import issue_session

SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"


def db_init():
    """按 schema.sql 建出整套对象；重复执行只输出「已存在」，不报错。"""
    if not SCHEMA_FILE.exists():
        raise SystemExit(f"找不到建库脚本：{SCHEMA_FILE}")
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    try:
        with connection() as conn:
            conn.execute(sql)
    except psycopg.errors.InsufficientPrivilege:
        raise SystemExit(
            "当前角色没有建表权限：建库要用拥有该库的角色（或 DBA）执行，"
            "见 db/deploy/ 的三步；运行时角色故意不给 DDL 权限。"
        )
    print(f"建表完成：{SCHEMA_FILE.name}（幂等，可重复执行）")


def bootstrap_admin(args):
    with connection() as conn:
        row = conn.execute("SELECT count(*) AS n FROM oams.permission_grants WHERE resource = 'workforce.grants' AND action = 'manage' AND scope_type = 'all' AND revoked_at IS NULL").fetchone()
        if row["n"]:
            raise SystemExit("An all-scope administrator grant already exists; refusing to bootstrap another administrator.")
        org = conn.execute(
            "INSERT INTO oams.org_units (org_code, name, unit_type) VALUES (%s, %s, 'company') RETURNING org_unit_id",
            (args.org_code, args.org_name),
        ).fetchone()
        member = conn.execute(
            "INSERT INTO oams.workforce_members (employee_code, display_name, employment_status) VALUES (%s, %s, 'active') RETURNING member_id",
            (args.employee_code, args.name),
        ).fetchone()
        position = conn.execute(
            "INSERT INTO oams.positions (position_code, title, org_unit_id) VALUES (%s, 'System Administrator', %s) RETURNING position_id",
            (args.position_code, org["org_unit_id"]),
        ).fetchone()
        conn.execute(
            """INSERT INTO oams.position_assignments
               (member_id, position_id, assignment_type, status, starts_at, reason)
               VALUES (%s, %s, 'primary', 'active', now(), 'initial system bootstrap')""",
            (member["member_id"], position["position_id"]),
        )
        resources = [
            ("workforce.org_units", "read"), ("workforce.org_units", "manage"),
            ("workforce.members", "read"), ("workforce.members", "manage"),
            ("workforce.positions", "read"), ("workforce.positions", "manage"),
            ("workforce.assignments", "read"), ("workforce.assignments", "manage"),
            ("workforce.grants", "read"), ("workforce.grants", "manage"),
            ("workforce.work_items", "read"), ("workforce.work_items", "manage"),
            ("workforce.reports", "read"), ("workforce.reports", "submit"),
            ("workforce.interfaces", "read"), ("workforce.interfaces", "review"),
            ("workforce.interfaces", "review_company"),
            ("workforce.audit", "read"),
        ]
        for resource, action in resources:
            conn.execute(
                """INSERT INTO oams.permission_grants
                   (subject_type, position_id, resource, action, scope_type, effect, valid_from, reason)
                   VALUES ('position', %s, %s, %s, 'all', 'allow', now(), 'initial system bootstrap')""",
                (position["position_id"], resource, action),
            )
        conn.execute(
            """INSERT INTO oams.workforce_audit_events
               (actor_member_id, event_type, subject_type, subject_id, reason, after_state)
               VALUES (%s, 'system.bootstrapped', 'member', %s, 'initial administrator bootstrap', %s)""",
            (member["member_id"], member["member_id"], Jsonb({"position_id": str(position["position_id"]), "org_unit_id": str(org["org_unit_id"])})),
        )
    print(json.dumps({"member_id": str(member["member_id"]), "position_id": str(position["position_id"]), "org_unit_id": str(org["org_unit_id"])}, indent=2))


def _member_id_from_code(employee_code):
    """按员工编号取成员 id。

    存在的理由：`/setup` 的成员表只显示姓名与员工编号，全仓没有「列出成员」的命令，
    UUID 在页面和命令行里都查不到——只照说明书走的人会卡在「member id 填什么」。
    员工编号在 schema 里是 UNIQUE（`workforce_members_employee_code_key`），不会一对多。
    """
    with connection() as conn:
        row = conn.execute(
            "SELECT member_id FROM oams.workforce_members WHERE employee_code = %s",
            (employee_code,),
        ).fetchone()
    if row is None:
        raise SystemExit(
            f"找不到员工编号「{employee_code}」对应的成员（编号区分大小写）。"
            "先用 /setup 第 3 步那张成员表核对，或改用 --member-id <UUID>。"
        )
    return str(row["member_id"])


def issue(args):
    """签一张临时会话令牌。

    `--member-id <UUID>` 与 `--employee-code <员工编号>` 二选一，签出来等价。两种写法都给
    就报错而不是挑一个：挑哪个都会让人以为另一个起了作用。
    """
    member_id = getattr(args, "member_id", None)
    employee_code = getattr(args, "employee_code", None)
    if member_id and employee_code:
        raise SystemExit(
            "--member-id 与 --employee-code 只能给一个：两种写法签出来的令牌等价，同时给不知道该按哪个签。"
        )
    if employee_code:
        member_id = _member_id_from_code(employee_code)
    elif not member_id:
        raise SystemExit(
            "缺成员：给 --member-id <UUID> 或 --employee-code <员工编号>（例如 DESIGN001）。"
        )
    try:
        member_uuid = str(UUID(member_id))
    except (TypeError, ValueError):
        raise SystemExit(
            f"--member-id 要的是成员 UUID，「{member_id}」不是。"
            "手上只有员工编号时改用 --employee-code。"
        )
    try:
        token, expires_at = issue_session(member_uuid)
    except ValueError as exc:  # 成员不存在或已离职：给一句人话，不甩 traceback
        raise SystemExit(f"签不出令牌：{exc}（成员可能不存在或已不在职）。")
    print(json.dumps({"access_token": token, "expires_at": expires_at.isoformat()}, indent=2))


def serve():
    uvicorn.run("oams.api:app", host=settings.bind_host, port=settings.bind_port)


def _run_plugin(args):
    """Delegate to oams.plugin_cli so the offline tool has no database import."""
    from oams.plugin_cli import main as plugin_main

    return plugin_main(args.plugin_args)


def _prog_name():
    """帮助里的程序名跟着入口走：`oams` 与兼容别名 `workhub` 各自报各自的名字。

    两个入口都指向这里，写死 "workhub" 会让 `oams --help` 冒充成 workhub；
    其它调用方式（如模块方式、测试里直接调 main()）仍报 workhub。
    """
    name = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else ""
    return name if name in {"oams", "workhub"} else "workhub"


def main():
    parser = argparse.ArgumentParser(prog=_prog_name())
    commands = parser.add_subparsers(dest="command", required=True)
    db = commands.add_parser("db", help="数据库结构与初始化")
    db_commands = db.add_subparsers(dest="db_command", required=True)
    db_commands.add_parser(
        "init", help="建表：按 app/oams/schema.sql 建出全套对象（首次建库与重复执行都安全）"
    ).set_defaults(func=lambda _: db_init())
    bootstrap = commands.add_parser("bootstrap-admin", help="create the first administrator; run once from a trusted operator shell")
    bootstrap.add_argument("--name", required=True)
    bootstrap.add_argument("--employee-code", required=True)
    bootstrap.add_argument("--org-code", required=True)
    bootstrap.add_argument("--org-name", required=True)
    bootstrap.add_argument("--position-code", default="SYSADMIN")
    bootstrap.set_defaults(func=bootstrap_admin)
    token = commands.add_parser("issue-token", help="issue a temporary session to an active member from a trusted operator shell")
    token.add_argument("--member-id", help="成员 UUID（bootstrap-admin 会打印）")
    token.add_argument("--employee-code", help="员工编号（例如 DESIGN001），与 --member-id 二选一")
    token.set_defaults(func=issue)
    commands.add_parser("serve", help="run the API").set_defaults(func=lambda _: serve())
    plugin = commands.add_parser("plugin", help="validate and preview a department plugin offline")
    plugin.add_argument("plugin_args", nargs=argparse.REMAINDER)
    plugin.set_defaults(func=lambda _: _run_plugin(_))
    args = parser.parse_args()
    exit_code = args.func(args)
    if isinstance(exit_code, int) and exit_code != 0:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
