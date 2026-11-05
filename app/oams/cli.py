import argparse
import json
import os
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
            "当前角色没有建表权限：换个拥有该库的角色（或请 DBA）重跑 `oams db init`；"
            "建库、建 schema、建表都走这一条命令，不再有分角色的建库脚本。"
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
               VALUES (%s, 'system.bootstrapped', 'member', %s, '初始化了第一位管理员', %s)""",
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

    顺手给出一条能直接贴进浏览器的登录地址：签出来的令牌本身是给终端用的，拿到它的人
    下一步永远是「拼成 /login?token=… 打开」，拼错前缀（端口、主机名）是最常见的下一步。
    站点前缀依次取 `--base-url`、环境变量 `OAMS_BASE_URL`、本机默认 8000。
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
    login_url = _login_url(token, getattr(args, "base_url", None))
    print(json.dumps({"access_token": token, "expires_at": expires_at.isoformat(),
                      "login_url": login_url}, indent=2))
    print("把这一行贴进浏览器就能登录：" + login_url, file=sys.stderr)


#: 没有给站点前缀时的默认值：本机试用环境的地址（`serve` 默认绑 127.0.0.1:8000）。
DEFAULT_BASE_URL = "http://127.0.0.1:8000"


def _login_url(token, base_url=None):
    """拼出这次令牌的登录地址：`<站点前缀>/login?token=<令牌>`。

    三处来源按优先级取：命令行 `--base-url` → 环境变量 `OAMS_BASE_URL` → 本机默认值。
    末尾斜杠统一去掉，`http://host/` 与 `http://host` 拼出来是同一个地址；整串只有空白时
    当作没给（环境变量里留个空行不该把默认值顶掉）。
    """
    base = (base_url or os.environ.get("OAMS_BASE_URL") or "").strip() or DEFAULT_BASE_URL
    return base.rstrip("/") + "/login?token=" + token


def serve():
    # 免登录没有登录这一步：它只许绑在本机。绑到别的机器也连得到的地址就拒绝启动，
    # 而不是起完打一行警告——警告没人读，而这时候谁打开都是系统管理员。
    from oams.security import assert_dev_autologin_is_local

    assert_dev_autologin_is_local()
    uvicorn.run("oams.api:app", host=settings.bind_host, port=settings.bind_port)


def _run_plugin(args):
    """Delegate to oams.plugin_cli so the offline tool has no database import."""
    from oams.plugin_cli import main as plugin_main

    return plugin_main(args.plugin_args)


def main():
    parser = argparse.ArgumentParser(prog="oams")
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
    token.add_argument("--base-url", help="登录地址的站点前缀，例如 http://127.0.0.1:8000（默认读环境变量 OAMS_BASE_URL，再退 http://127.0.0.1:8000）")
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
