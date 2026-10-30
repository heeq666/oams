"""Measure layout in the real browser, through ego (see AGENTS.md 浏览器自动化).

The three layout checks used to launch headless Chrome themselves and drive it
over a hand-rolled CDP client (scripts/cdp-*.mjs). They still need a real browser
— "the rail is a column" is a claim about pixels — so they now drive ego instead
and share this helper.

ego swallows argv and the caller's environment, so an input file is the only way
in. Each run therefore gets its own config file under pytest's ``tmp_path``, and
the caller's copy of ``scripts/ego-measure.mjs`` carries that path: nothing is
shared between runs, so serial and parallel runs both behave.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

EGO = "/Users/example-user/.local/bin/ego-browser"
ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "scripts" / "ego-measure.mjs"
PLACEHOLDER = '"__EGO_MEASURE_CONFIG__"'


def admin_token(employee_code="ADMIN001") -> str:
    """Session token for a seeded member, so the page list is measured signed in."""
    sys.path.insert(0, str(ROOT / "app"))
    import psycopg
    from psycopg.rows import dict_row
    from workhub.security import issue_session

    with psycopg.connect(os.environ["WORKHUB_DATABASE_URL"], row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT member_id FROM workhub.workforce_members WHERE employee_code = %s",
            (employee_code,),
        ).fetchone()
    if row is None:
        pytest.skip(f"the served database has no {employee_code} member")
    return issue_session(row["member_id"])[0]


def skip_without_browser() -> None:
    if not os.path.exists(EGO):
        pytest.skip(f"ego-browser is not installed at {EGO}")


def measure(paths, expression, *, viewport, tmp_path, scroll=None, settle=2200, timeout=240):
    """Walk ``paths`` in one fresh ego page; return {path: {before, afterScroll}}."""
    config = tmp_path / "ego-measure.json"
    out = tmp_path / "ego-measure.out.json"
    config.write_text(json.dumps({
        "out": str(out),
        "base": os.environ.get("WORKHUB_BASE", "http://127.0.0.1:8000"),
        "paths": paths,
        "expression": expression,
        "viewport": viewport,
        "scroll": scroll,
        "settle": settle,
    }), encoding="utf-8")
    # Hand ego a private copy of the driver that points at this run's config file.
    script = tmp_path / "ego-measure.mjs"
    source = DRIVER.read_text(encoding="utf-8")
    assert PLACEHOLDER in source, f"{DRIVER} lost its config placeholder"
    script.write_text(source.replace(PLACEHOLDER, json.dumps(str(config))), encoding="utf-8")
    with script.open("rb") as handle:
        run = subprocess.run([EGO, "nodejs"], stdin=handle,
                             capture_output=True, text=True, timeout=timeout)
    assert run.returncode == 0, (run.stdout + run.stderr).strip()[-800:]
    assert out.exists(), f"ego did not write the report: {(run.stdout + run.stderr)[-800:]}"
    return json.loads(out.read_text(encoding="utf-8"))
