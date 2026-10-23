"""The department rail and top bar stay put; only the content pane scrolls.

The first version of this check scrolled the window, so it reported success while
the rail was actually sliding under the top bar. It now scrolls the content pane,
which is the region that scrolls in this shell, and asserts the rail never moves
and never hides behind the bar above it. Requires headless Chrome and node; both
are skipped (not failed) when absent.
"""
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.db  # needs the served app plus headless Chrome

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
DEFAULT_PATHS = ["/board", "/progress", "/reports", "/settings"]


def skip_without_browser() -> None:
    if not os.path.exists(CHROME):
        pytest.skip(f"headless Chrome is not installed at {CHROME}")
    if shutil.which("node") is None:
        pytest.skip("node is not on PATH, so the CDP driver cannot run")


def admin_token() -> str:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
    import psycopg
    from psycopg.rows import dict_row
    from workhub.security import issue_session

    with psycopg.connect(os.environ["WORKHUB_DATABASE_URL"], row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT member_id FROM workhub.workforce_members WHERE employee_code = 'ADMIN001'"
        ).fetchone()
    if row is None:
        pytest.skip("the served database has no ADMIN001 member")
    token, _ = issue_session(row["member_id"])
    return token


def chrome_ws(port):
    for _ in range(80):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1) as resp:
                for tab in json.load(resp):
                    if tab.get("type") == "page":
                        return tab["webSocketDebuggerUrl"]
        except Exception:
            time.sleep(0.25)
    raise AssertionError("chrome did not expose a page target")


def measure(paths):
    port = 9700 + random.randint(0, 200)
    profile = tempfile.mkdtemp(prefix="wh-sticky-")
    chrome = subprocess.Popen(
        [CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
         "--disable-dev-shm-usage", "--no-first-run",
         "--no-default-browser-check", f"--user-data-dir={profile}",
         "--window-size=1280,700", f"--remote-debugging-port={port}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ws_url = chrome_ws(port)
        out = subprocess.run(
            ["node", str(Path(__file__).resolve().parents[1] / "scripts" / "cdp-vertical.mjs"),
             ws_url, json.dumps(paths)],
            capture_output=True, text=True, timeout=180)
        assert out.returncode == 0, out.stderr.strip()[-600:]
        return json.loads(out.stdout)
    finally:
        chrome.kill()
        shutil.rmtree(profile, ignore_errors=True)


def test_layout_sticky_rail(site) -> None:
    skip_without_browser()
    paths = ["/login?token=" + admin_token()] + DEFAULT_PATHS
    report = measure(paths)

    failures = []
    for path in DEFAULT_PATHS:
        data = report.get(path)
        if data is None:
            print("FAIL", path, "was not measured")
            failures.append(path)
            continue
        before, after = data.get("before", {}), data.get("afterScroll", {})
        if "error" in before:
            print("FAIL", path, before)
            failures.append(path)
            continue
        # The top bar is a floating capsule now: it sits above the frame, and the
        # frame (rail + content) owns the whole viewport. What matters is that the
        # rail keeps the full height and never moves while the content scrolls.
        bar_ok = before["barFloats"] and after["barFloats"]
        if after["mainScrollable"] < 100:
            ok = before["railFullyVisible"] and before["railSpansViewport"] and bar_ok
            print(("PASS " if ok else "FAIL ") +
                  f"{path:<12} no scroll needed; rail visible={before['railFullyVisible']} "
                  f"spans={before['railSpansViewport']} bar_floats={before['barFloats']}")
        else:
            moved = abs(after["railTop"] - before["railTop"]) <= 1
            scrolled = after["mainScrollTop"] > 100
            ok = (moved and scrolled and after["railFullyVisible"]
                  and after["railSpansViewport"] and bar_ok)
            print(("PASS " if ok else "FAIL ") +
                  f"{path:<12} content_scroll={before['mainScrollTop']}->{after['mainScrollTop']} "
                  f"rail_top={before['railTop']}->{after['railTop']} "
                  f"visible={after['railFullyVisible']} spans={after['railSpansViewport']} "
                  f"bar_floats={after['barFloats']}")
        if not ok:
            failures.append(path)
    assert not failures, f"the rail does not stay put on: {failures}"
