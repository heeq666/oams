"""Shared pytest fixtures for the Workhub test suite.

Design rules (deliberate):

* **Nothing here touches the database at import time.**  ``psycopg`` is only
  imported inside fixture bodies, so collecting the pure-logic suites
  (descriptor / render / modules / metrics / funnel / mcp) never needs a
  live PostgreSQL.
* Tests that *do* need a database are marked ``@pytest.mark.db``.  Their
  fixtures connect lazily and ``pytest.skip`` (not error) when no database
  is reachable, so ``pytest -q`` stays usable on a laptop without Docker.
* The e2e scripts expect an HTTP server to talk to.  ``workhub_server``
  boots the real ASGI app in a background thread on a free port and points
  every ``WORKHUB_*_BASE`` environment variable at it, so the legacy
  scripts keep working unchanged.

Environment:

    WORKHUB_DATABASE_URL   service connection string (defaults to the local
                           docker ``workhub-pg`` instance on port 55433)
    WORKHUB_TEST_PORT      preferred port for the in-process server
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = REPO_ROOT / "app"

DEFAULT_DATABASE_URL = "postgresql://workhub:change-me@127.0.0.1:55433/workhub"

BASE_ENV_VARS = (
    "WORKHUB_BASE",
    "WORKHUB_IFACE_BASE",
    "WORKHUB_PILOT_BASE",
    "WORKHUB_DEMO_BASE",
)


def database_url() -> str:
    """Service connection string used by the DB-backed tests."""

    return os.environ.get("WORKHUB_DATABASE_URL") or DEFAULT_DATABASE_URL


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def http_ok(url: str, timeout: float = 1.0) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:
            return 200 <= response.status < 500
    except (urllib.error.URLError, OSError, ValueError):
        return False


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "db: needs a live PostgreSQL (set WORKHUB_DATABASE_URL); skipped when unreachable",
    )
    # ``workhub`` lives in ``app/``; make plain ``import workhub`` work no matter
    # how pytest was invoked.
    app = str(APP_DIR)
    if app not in os.sys.path:
        os.sys.path.insert(0, app)


class Database:
    """Tiny helper handed to DB-backed tests."""

    def __init__(self, url: str) -> None:
        self.url = url

    def connect(self, **kwargs):
        import psycopg
        from psycopg.rows import dict_row

        kwargs.setdefault("row_factory", dict_row)
        return psycopg.connect(self.url, **kwargs)

    def one(self, sql: str, params: tuple = ()):
        with self.connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return row

    def scalar(self, sql: str, params: tuple = ()):
        row = self.one(sql, params)
        return None if row is None else next(iter(row.values()))

    def member_id(self, employee_code: str):
        return self.scalar(
            "SELECT member_id FROM workhub.workforce_members WHERE employee_code = %s",
            (employee_code,),
        )

    def org_unit_id(self, org_code: str):
        return self.scalar(
            "SELECT org_unit_id FROM workhub.org_units WHERE org_code = %s",
            (org_code,),
        )

    def all(self, sql: str, params: tuple = ()):
        with self.connect() as conn:
            return conn.execute(sql, params).fetchall()


def require(database, members=(), orgs=()) -> dict:
    """Skip (never error) when the seeded rows a script depends on are missing.

    The legacy e2e scripts were written against a seed set that is not present in
    every database (DESIGN001, MVP1001, ...).  Looking the rows up and skipping
    keeps pytest -q green on a database seeded differently.
    """

    found: dict = {}
    missing: list[str] = []
    for code in members:
        member_id = database.member_id(code)
        if member_id is None:
            missing.append("member " + code)
        else:
            found[code] = member_id
    for code in orgs:
        org_unit_id = database.org_unit_id(code)
        if org_unit_id is None:
            missing.append("org unit " + code)
        else:
            found[code] = org_unit_id
    if missing:
        pytest.skip("database is missing seed rows: " + ", ".join(missing))
    return found


@pytest.fixture(scope="session")
def db_ready() -> Database:
    """Session-scoped database handle; skips the test when unreachable."""

    url = database_url()
    try:
        import psycopg  # noqa: F401  (deliberately imported lazily)
    except ImportError:  # pragma: no cover - psycopg is a runtime dependency
        pytest.skip("psycopg is not installed; skipping database tests")

    database = Database(url)
    try:
        database.scalar("SELECT 1")
        database.scalar("SELECT to_regclass('workhub.schema_migrations')")
    except Exception as exc:  # pragma: no cover - depends on the machine
        pytest.skip(f"no usable PostgreSQL at {url}: {exc.__class__.__name__}: {exc}")

    if database.scalar("SELECT to_regclass('workhub.schema_migrations')") is None:
        pytest.skip("workhub schema is not migrated in this database")

    return database


@pytest.fixture(scope="session")
def admin_member_id(db_ready: Database):
    """Member id of the bootstrapped administrator (ADMIN001 by default)."""

    member_id = db_ready.member_id("ADMIN001")
    if member_id is None:
        pytest.skip("no ADMIN001 member in the database; run bootstrap-admin first")
    return member_id


@pytest.fixture(scope="session")
def workhub_server(db_ready: Database):
    """Boot the real ASGI app in a background thread and return its base URL."""

    import uvicorn

    os.environ.setdefault("WORKHUB_DATABASE_URL", db_ready.url)
    port = int(os.environ.get("WORKHUB_TEST_PORT") or 0) or free_port()
    base = f"http://127.0.0.1:{port}"

    config = uvicorn.Config(
        "workhub.api:app",
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="workhub-test-server", daemon=True)
    thread.start()

    deadline = time.time() + 45
    while time.time() < deadline:
        if http_ok(base + "/login"):
            break
        time.sleep(0.15)
    else:  # pragma: no cover - environment problem
        server.should_exit = True
        thread.join(timeout=10)
        pytest.skip(f"the ASGI app did not become ready on {base}")

    for name in BASE_ENV_VARS:
        os.environ[name] = base

    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture()
def isolated_database(tmp_path: Path, db_ready: Database):
    """Create a throw-away database with all migrations applied.

    Used by the scripts that bootstrap their own administrator (they refuse to
    run against a database that already has one).
    """

    import psycopg

    name = f"workhub_pytest_{os.getpid()}_{int(time.time() * 1000) % 100000}"
    admin_url = _replace_dbname(db_ready.url, "postgres")
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        conn.execute(f'CREATE DATABASE "{name}"')

    url = _replace_dbname(db_ready.url, name)
    try:
        yield url
    finally:
        try:
            with psycopg.connect(admin_url, autocommit=True) as conn:
                conn.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (name,),
                )
                conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        except Exception:  # pragma: no cover - best effort cleanup
            pass


def _replace_dbname(url: str, dbname: str) -> str:
    head, _, _tail = url.rpartition("/")
    return f"{head}/{dbname}"


# ---------------------------------------------------------------------------
# Seeded, throw-away database used by the end-to-end scripts
# ---------------------------------------------------------------------------
#
# The legacy e2e scripts were written against a hand-built database (a company
# administrator, a design department with one lead, and two fictional MVP
# departments).  Rather than depend on whatever happens to be in the developer's
# database, the fixtures below build that world from scratch in a database of
# their own.


def _admin_connection(db_ready):
    """Autocommit connection to the maintenance database (for CREATE/DROP)."""

    import psycopg

    return psycopg.connect(_replace_dbname(db_ready.url, "postgres"), autocommit=True)


def _create_database(db_ready) -> tuple[str, str]:
    name = f"workhub_pytest_{os.getpid()}_{int(time.time() * 1000) % 1000000}"
    with _admin_connection(db_ready) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        conn.execute(f'CREATE DATABASE "{name}"')
    return name, _replace_dbname(db_ready.url, name)


def _drop_database(db_ready, name: str) -> None:
    try:
        with _admin_connection(db_ready) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
    except Exception:  # pragma: no cover - best effort cleanup
        pass


def _cli(args: list[str], url: str) -> str:
    """Run one workhub.cli subcommand against the given database URL."""

    import subprocess

    env = dict(os.environ)
    env["WORKHUB_DATABASE_URL"] = url
    env["PYTHONPATH"] = str(APP_DIR) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    completed = subprocess.run(
        [os.sys.executable, "-m", "workhub.cli", *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if completed.returncode != 0:  # pragma: no cover - environment problem
        raise RuntimeError(
            "workhub.cli " + " ".join(args) + " failed: " + completed.stderr[-800:]
        )
    return completed.stdout


SEED_SQL = """
-- bootstrap-admin already created the company unit; only add it when it did not.
INSERT INTO workhub.org_units (org_code, name, unit_type)
SELECT 'COMPANY', '公司', 'company'
 WHERE NOT EXISTS (SELECT 1 FROM workhub.org_units WHERE org_code = 'COMPANY');

INSERT INTO workhub.org_units (org_code, name, unit_type, parent_org_unit_id)
SELECT 'DESIGN', '设计部', 'department', org_unit_id
  FROM workhub.org_units WHERE org_code = 'COMPANY';
INSERT INTO workhub.org_units (org_code, name, unit_type, parent_org_unit_id)
SELECT 'HR', '人事部', 'department', org_unit_id
  FROM workhub.org_units WHERE org_code = 'COMPANY';
INSERT INTO workhub.org_units (org_code, name, unit_type, parent_org_unit_id)
SELECT 'SALES', '业务部', 'department', org_unit_id
  FROM workhub.org_units WHERE org_code = 'COMPANY';

INSERT INTO workhub.positions (position_code, title, org_unit_id)
SELECT 'DESIGN_LEAD', '设计负责人', org_unit_id
  FROM workhub.org_units WHERE org_code = 'DESIGN';
INSERT INTO workhub.positions (position_code, title, org_unit_id)
SELECT 'HR_MANAGER', '人事经理', org_unit_id
  FROM workhub.org_units WHERE org_code = 'HR';
INSERT INTO workhub.positions (position_code, title, org_unit_id)
SELECT 'HR_SPECIALIST', '人事专员', org_unit_id
  FROM workhub.org_units WHERE org_code = 'HR';
INSERT INTO workhub.positions (position_code, title, org_unit_id)
SELECT 'SALES_LEAD', '业务负责人', org_unit_id
  FROM workhub.org_units WHERE org_code = 'SALES';

INSERT INTO workhub.workforce_members (employee_code, display_name, employment_status, joined_at)
VALUES ('DESIGN001', '设计负责人', 'active', current_date - 30)
     , ('MVP1001', '周岚', 'active', current_date - 30)
     , ('MVP1002', '陈默', 'active', current_date - 30)
     , ('MVP2001', '林溪', 'active', current_date - 30);

INSERT INTO workhub.position_assignments
       (member_id, position_id, assignment_type, status, starts_at, reason)
SELECT m.member_id, p.position_id, 'primary', 'active', now() - interval '30 days', 'seed'
  FROM workhub.workforce_members m
  JOIN workhub.positions p
    ON (m.employee_code, p.position_code) IN (
         ('DESIGN001', 'DESIGN_LEAD'),
         ('MVP1001', 'HR_MANAGER'),
         ('MVP1002', 'HR_SPECIALIST'),
         ('MVP2001', 'SALES_LEAD'));

INSERT INTO workhub.permission_grants
       (subject_type, position_id, resource, action, scope_type, valid_from, reason)
SELECT 'position', p.position_id, g.resource, g.action, 'all', now(), 'seed'
  FROM workhub.positions p
  JOIN (VALUES
         ('DESIGN_LEAD', 'workforce.org_units', 'read'),
         ('DESIGN_LEAD', 'workforce.positions', 'read'),
         ('DESIGN_LEAD', 'workforce.work_items', 'read'),
         ('DESIGN_LEAD', 'workforce.reports', 'read'),
         ('DESIGN_LEAD', 'workforce.reports', 'submit'),
         ('DESIGN_LEAD', 'workforce.interfaces', 'review'),
         ('HR_MANAGER', 'workforce.org_units', 'read'),
         ('HR_MANAGER', 'workforce.positions', 'read'),
         ('HR_MANAGER', 'workforce.work_items', 'read'),
         ('HR_MANAGER', 'workforce.work_items', 'manage'),
         ('HR_MANAGER', 'workforce.reports', 'read'),
         ('HR_MANAGER', 'workforce.reports', 'submit'),
         ('HR_SPECIALIST', 'workforce.org_units', 'read'),
         ('HR_SPECIALIST', 'workforce.positions', 'read'),
         ('HR_SPECIALIST', 'workforce.work_items', 'read'),
         ('HR_SPECIALIST', 'workforce.reports', 'read'),
         ('HR_SPECIALIST', 'workforce.reports', 'submit'),
         ('SALES_LEAD', 'workforce.work_items', 'read'),
         ('SALES_LEAD', 'workforce.reports', 'read'),
         ('SALES_LEAD', 'workforce.reports', 'submit')
       ) AS g(position_code, resource, action)
    ON g.position_code = p.position_code;

-- Migration 0008 derives the interface-review grant from workforce.grants/manage,
-- but it runs before bootstrap-admin ever creates that grant, so a freshly
-- bootstrapped administrator would not be able to review pages.  Grant it here.
INSERT INTO workhub.permission_grants
       (subject_type, position_id, resource, action, scope_type, valid_from, reason)
SELECT 'position', p.position_id, 'workforce.interfaces', 'review', 'all', now(), 'seed'
  FROM workhub.positions p WHERE p.position_code = 'SYSADMIN';


-- A department that has drawn a screen, so the workbench and the review queue
-- have something real to show.  The descriptor is the current schema; the
-- database stores it as data and never as code.
INSERT INTO workhub.interfaces
       (org_unit_id, slug, name, status, visibility, schema_version, descriptor,
        position_id, version_no, created_by, updated_by, reviewed_by, reviewed_at)
SELECT o.org_unit_id, 'design-sampling', '设计部打样进度', 'active', 'department', '0.2',
       '{"schema_version":"0.2","interface_id":"design-sampling","name":"设计部打样进度","org_unit_code":"DESIGN","position_codes":["DESIGN_LEAD"],"capabilities":["entries:write","entries:read:dept","aggregate:read:dept"],"modules":[{"key":"layout","type":"rows","params":{"gap":"md"},"children":[{"key":"progress","type":"progress_row","module_version":"^1.0","source_fields":["title","status","progress_value","note","evidence"]}]}],"fields":[{"key":"title","label":"款号","type":"text","required":true,"map_to":"title","aggregatable":true,"example":"AW24-001"},{"key":"status","label":"打样状态","type":"enum","required":true,"map_to":"status","aggregatable":true,"example":"打样中","options":["待打样","打样中","等面料","已完成"]},{"key":"progress_value","label":"完成打样数","type":"number","required":true,"map_to":"progress_value","aggregatable":true,"example":"40"},{"key":"note","label":"备注","type":"text","required":false,"map_to":"note","aggregatable":false,"example":"已完成初版打样"},{"key":"evidence","label":"打样单号","type":"text","required":true,"map_to":"evidence","aggregatable":true,"example":"SY-2410"}],"status_map":{"待打样":"planned","打样中":"in_progress","等面料":"blocked","已完成":"done"},"rules":{"evidence_required":true,"progress_range":[0,100]},"meta":{"author_position_code":"DESIGN_LEAD","version":1,"visibility":"department","review":{"status":"active"}}}'::jsonb,
       p.position_id, 1, m.member_id, m.member_id, a.member_id, now()
  FROM workhub.org_units o
  JOIN workhub.positions p ON p.position_code = 'DESIGN_LEAD'
  JOIN workhub.workforce_members m ON m.employee_code = 'DESIGN001'
  JOIN workhub.workforce_members a ON a.employee_code = 'ADMIN001'
 WHERE o.org_code = 'DESIGN';

INSERT INTO workhub.interface_entries
       (interface_id, title, status, blocked_reason, needs_decision, progress_value,
        note, evidence, extra, reported_by, submitted_via, entry_mode)
SELECT i.interface_id, 'AW24-001', 'blocked', '等面料到仓，供应商承诺 10-20', true, 40,
       '已完成初版打样', '样衣确认单 SY-2410', '{"fabric_note": "待补货"}'::jsonb,
       m.member_id, 'human', 'live'
  FROM workhub.interfaces i
  JOIN workhub.workforce_members m ON m.employee_code = 'DESIGN001'
 WHERE i.slug = 'design-sampling' AND i.status = 'active';

INSERT INTO workhub.work_items (org_unit_id, position_id, title, kind, status, blocked_reason, needs_decision)
SELECT p.org_unit_id, p.position_id, 'MVP演示：招聘进度汇报', 'project', 'in_progress', NULL, false
  FROM workhub.positions p WHERE p.position_code = 'HR_MANAGER';
INSERT INTO workhub.work_items (org_unit_id, position_id, title, kind, status, blocked_reason, needs_decision)
SELECT p.org_unit_id, p.position_id, 'MVP演示：合同续签确认', 'routine', 'blocked', '等待法务确认模板', true
  FROM workhub.positions p WHERE p.position_code = 'HR_MANAGER';
INSERT INTO workhub.work_items (org_unit_id, position_id, title, kind, status, blocked_reason, needs_decision)
SELECT p.org_unit_id, p.position_id, 'MVP演示：客户需求确认', 'project', 'blocked', '等待客户反馈需求清单', true
  FROM workhub.positions p WHERE p.position_code = 'SALES_LEAD';

INSERT INTO workhub.work_item_reports
       (work_item_id, reported_by_member_id, submitted_via, progress_value, note, evidence)
SELECT i.work_item_id, m.member_id, 'agent', 60, '已完成候选人初筛，等待面试安排', '虚构演示：候选人清单 v1'
  FROM workhub.work_items i, workhub.workforce_members m
 WHERE i.title = 'MVP演示：招聘进度汇报' AND m.employee_code = 'MVP1001';
INSERT INTO workhub.work_item_reports
       (work_item_id, reported_by_member_id, submitted_via, progress_value, note, evidence)
SELECT i.work_item_id, m.member_id, 'agent', 30, '已发出需求确认清单', '虚构演示：需求邮件摘要'
  FROM workhub.work_items i, workhub.workforce_members m
 WHERE i.title = 'MVP演示：客户需求确认' AND m.employee_code = 'MVP2001';
"""


@pytest.fixture(scope="session")
def seeded_workhub(db_ready) -> dict:
    """A migrated, bootstrapped and seeded database of its own."""

    name, url = _create_database(db_ready)
    try:
        _cli(["migrate"], url)
        _cli(
            [
                "bootstrap-admin",
                "--name", "管理员",
                "--employee-code", "ADMIN001",
                "--org-code", "COMPANY",
                "--org-name", "公司",
            ],
            url,
        )
        import psycopg

        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(SEED_SQL)
        database = Database(url)
        yield {
            "name": name,
            "url": url,
            "database": database,
            "admin_member_id": database.member_id("ADMIN001"),
        }
    finally:
        _drop_database(db_ready, name)


@pytest.fixture(scope="session")
def seeded_database(seeded_workhub) -> Database:
    """The seeded database handle, for scripts that only need rows."""

    return seeded_workhub["database"]

@pytest.fixture(scope="session")
def bootstrapped_database(db_ready) -> Database:
    """A migrated, bootstrapped database with no demonstration rows at all.

    Suites that assert on *global* scopes -- "every active department rolls up
    into the company board" -- cannot run against a developer database, because
    whatever pages already live there get counted too.  This fixture creates one
    database per session (not per test: 37 CREATE DATABASE calls would dominate
    the run), applies the migrations and bootstraps ADMIN001, and leaves the rest
    empty so the only rows a test sees are the ones it inserted itself.  Each
    test still does its own SAVEPOINT / ROLLBACK, so the rows are real ones the
    database wrote -- constraints, defaults and triggers included.
    """

    name, url = _create_database(db_ready)
    try:
        _cli(["migrate"], url)
        _cli(
            [
                "bootstrap-admin",
                "--name", "管理员",
                "--employee-code", "ADMIN001",
                "--org-code", "COMPANY",
                "--org-name", "公司",
            ],
            url,
        )
        yield Database(url)
    finally:
        _drop_database(db_ready, name)


# ---------------------------------------------------------------------------
# Serving the application against a chosen database
# ---------------------------------------------------------------------------
#
# workhub.config.settings is one mutable object shared by workhub.db, so a test
# can point the running process at a different database simply by swapping
# settings.database_url before the server handles a request.  That lets every
# e2e script run against its own pristine database without restarting Python.


SERVED: dict[str, str] = {}


def point_settings_at(url: str) -> str:
    """Repoint the process-wide settings at *url*; returns the previous URL."""

    import workhub.config as config_module
    import workhub.db as db_module

    previous = config_module.settings.database_url
    config_module.settings.database_url = url
    if hasattr(db_module, "settings"):  # workhub.db imports the same object
        db_module.settings.database_url = url
    os.environ["WORKHUB_DATABASE_URL"] = url
    return previous


@contextlib.contextmanager
def serve(url: str):
    """Serve the real ASGI app against *url* and yield its base URL."""

    import uvicorn

    previous = point_settings_at(url)
    # Several served instances coexist in one session (a session-scoped site,
    # plus a per-test migrated_site).  Each one rebinds the WORKHUB_*_BASE
    # variables, so it has to put them back on the way out -- otherwise the
    # scripts that read them straight from the environment would keep aiming at
    # a server that has already shut down.
    previous_env = {name: os.environ.get(name) for name in BASE_ENV_VARS}
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    config = uvicorn.Config(
        "workhub.api:app",
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="workhub-test-server", daemon=True)
    thread.start()
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            if http_ok(base + "/login"):
                break
            time.sleep(0.15)
        else:  # pragma: no cover - environment problem
            pytest.skip(f"the ASGI app did not become ready on {base}")
        SERVED[base] = url
        for name in BASE_ENV_VARS:
            os.environ[name] = base
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        for name, value in previous_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        point_settings_at(previous)


def load_seed(spec, url: str, admin_member_id: str) -> str:
    """Run scripts/load_department_data.py against *url*; returns its stdout."""

    import subprocess

    env = dict(os.environ)
    env["PYTHONPATH"] = str(APP_DIR) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    env.update(
        {
            "WORKHUB_DATABASE_URL": url,
            "WORKHUB_MIGRATOR_URL": url,
            "WORKHUB_ADMIN_MEMBER_ID": str(admin_member_id),
            "WORKHUB_LOADER_PORT": str(free_port()),
        }
    )
    completed = subprocess.run(
        [
            os.sys.executable,
            str(REPO_ROOT / "scripts" / "load_department_data.py"),
            str(spec),
            "--apply",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if completed.returncode != 0:  # pragma: no cover - environment problem
        raise RuntimeError(
            "load_department_data failed: " + (completed.stderr or completed.stdout)[-800:]
        )
    return completed.stdout


@pytest.fixture(scope="session")
def site(seeded_workhub):
    """The app served against the seeded database (ADMIN001, DESIGN001, MVP...)."""

    with serve(seeded_workhub["url"]) as base:
        yield base


@pytest.fixture()
def migrated_site(db_ready):
    """A migrated database with no administrator, served by its own app instance.

    The scripts that exercise bootstrap-admin / migrate need a database nobody
    has touched yet, so this one is created per test and dropped afterwards.
    """

    name, url = _create_database(db_ready)
    try:
        _cli(["migrate"], url)
        with serve(url) as base:
            yield {"name": name, "url": url, "base": base, "database": Database(url)}
    finally:
        _drop_database(db_ready, name)


@pytest.fixture(scope="session")
def hr_site(db_ready):
    """The seed-hr department (example-co + HR + 10 work items) served by the app.

    Built with scripts/load_department_data.py -- the same loader a real
    deployment uses -- so the loader itself is covered by the suite too.
    """

    name, url = _create_database(db_ready)
    try:
        _cli(["migrate"], url)
        bootstrap = _cli(
            [
                "bootstrap-admin",
                "--name", "example-user",
                "--employee-code", "9999",
                "--org-code", "example-co",
                "--org-name", "example-co",
            ],
            url,
        )
        load_seed(REPO_ROOT / "data" / "seed-hr-department.json", url,
                  json.loads(bootstrap)["member_id"])
        with serve(url) as base:
            yield {"name": name, "url": url, "base": base, "database": Database(url)}
    finally:
        _drop_database(db_ready, name)


@pytest.fixture(autouse=True)
def no_dev_autologin():
    """The design-stage shortcut is always off unless a test turns it on.

    WORKHUB_DEV_AUTOLOGIN signs in session-less requests.  A suite that asserts
    "the board redirects an unauthenticated caller" would pass for the wrong
    reason with it on -- and worse, a logout that clears nothing would look fine.
    The conversation between the two is exactly the regression nobody notices, so
    the switch is forced off here; tests/test_dev_autologin.py sets it back itself.
    """

    from workhub.config import settings

    previous = getattr(settings, "dev_autologin", None)
    settings.dev_autologin = None
    yield
    settings.dev_autologin = previous


@pytest.fixture(autouse=True)
def settings_follow_the_test(request):
    """Point the process-wide settings at the database of the server in use.

    Two served instances coexist inside one pytest session, and
    workhub.db.connection() reads settings.database_url on every request.  So
    before a test body runs, settings has to name the database behind the server
    that test is about to talk to -- otherwise one file's requests would land in
    another file's database.
    """

    bases = []
    for name in ("site", "hr_site", "migrated_site"):
        if name not in request.fixturenames:
            continue
        value = request.getfixturevalue(name)
        bases.append(value if isinstance(value, str) else value["base"])
    if bases and bases[-1] in SERVED:
        # This test talks to bases[-1]; aim the process (and the environment the
        # legacy scripts read) at the database and the port behind it.
        previous_env = {name: os.environ.get(name) for name in BASE_ENV_VARS}
        for name in BASE_ENV_VARS:
            os.environ[name] = bases[-1]
        point_settings_at(SERVED[bases[-1]])
        yield
        for name, value in previous_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        return
    yield
