"""Measure the running site over CDP: is the department rail a persistent column?

Layout is a claim about pixels, so it is checked in a real browser rather than
by reading the served HTML. Requires headless Chrome and node; both are skipped
(not failed) when absent, and the page list is measured while signed in as the
seeded administrator.
"""
import json
import os
import shutil
import socket
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
MEASURE_JS = """(() => {
  const rail = document.querySelector('.app-rail');
  const main = document.querySelector('.app-frame > main');
  if (!rail || !main) return { hasRail: !!rail, hasMain: !!main };
  const r = rail.getBoundingClientRect();
  const m = main.getBoundingClientRect();
  const bar = document.querySelector('header.topbar');
  const b = bar ? bar.getBoundingClientRect() : { top: 0, width: 0, height: 0 };
  return {
    railLeft: Math.round(r.left), railRight: Math.round(r.right),
    railTop: Math.round(r.top), railBottom: Math.round(r.bottom),
    railHeight: Math.round(r.height), viewportHeight: window.innerHeight,
    barTop: Math.round(b.top), barWidth: Math.round(b.width),
    barHeight: Math.round(b.height),
    barScrolled: !!(bar && bar.classList.contains('is-scrolled')),
    // A short page has nothing to scroll, so the capsule legitimately stays at
    // rest: the narrowing is only asserted where the content is taller than the
    // frame.
    scrolls: (m.scrollHeight > m.clientHeight + 1)
      || (document.documentElement.scrollHeight > window.innerHeight + 1),
    mainLeft: Math.round(m.left),
    railIsLeftOfContent: r.right <= m.left + 1,
    // The rail is a column of the frame: the frame owns the whole viewport now,
    // because the top bar floats over it instead of taking a row of it.
    railSpansViewport: Math.abs(r.top) <= 2
      && Math.abs(r.bottom - window.innerHeight) <= 2,
    // The capsule sits below the top edge, is 40px tall, and leaves room on
    // both sides instead of running edge to edge.
    barFloatsOnTop: Math.round(b.top) >= 8 && Math.round(b.top) <= 28
      && Math.round(b.height) === 40,
    // At rest it is the wide capsule; the content column slides underneath it.
    barWideAtRest: Math.round(b.width) >= window.innerWidth - 120,
    position: getComputedStyle(rail).position,
  };
})()"""


def base_url() -> str:
    return os.environ.get("WORKHUB_BASE", "http://127.0.0.1:8000")


def skip_without_browser() -> None:
    if not os.path.exists(CHROME):
        pytest.skip(f"headless Chrome is not installed at {CHROME}")
    if shutil.which("node") is None:
        pytest.skip("node is not on PATH, so the CDP driver cannot run")


def sign_in_path(token: str) -> str:
    """First navigation: exchange a session token for the cookie."""

    return "/login?token=" + token


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
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    profile = tempfile.mkdtemp(prefix="wh-layout-")
    chrome = subprocess.Popen(
        [CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
         "--disable-dev-shm-usage", "--no-first-run",
         "--no-default-browser-check", f"--user-data-dir={profile}",
         "--window-size=1280,900", f"--remote-debugging-port={port}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ws_url = chrome_ws(port)
        out = subprocess.run(
            ["node", str(Path(__file__).resolve().parents[1] / "scripts" / "cdp-measure.mjs"),
             ws_url, MEASURE_JS, json.dumps(paths)],
            capture_output=True, text=True, timeout=180)
        assert out.returncode == 0, out.stderr.strip()[-600:]
        return json.loads(out.stdout)
    finally:
        chrome.kill()
        shutil.rmtree(profile, ignore_errors=True)


def test_layout_persistent_rail(site) -> None:
    skip_without_browser()
    paths = [sign_in_path(admin_token())] + DEFAULT_PATHS
    report = measure(paths)
    failures = []
    for path in DEFAULT_PATHS:
        before, after = report[path]["before"], report[path]["afterScroll"]
        narrowed = (not before.get("scrolls")) or (
            after.get("barScrolled") and after.get("barWidth", 9999) <= 882)
        ok = (before.get("railIsLeftOfContent") and before.get("railSpansViewport")
              and before.get("barFloatsOnTop") and before.get("barWideAtRest")
              and narrowed
              and after.get("railLeft") == 0 and before.get("railLeft") == 0)
        print(("PASS " if ok else "FAIL ") + path +
              f"  rail={before.get('railRight')}x{before.get('railHeight')}"
              f"  bar={before.get('barWidth')}x{before.get('barHeight')}"
              f"  bar_top={before.get('barTop')}"
              f"  floats={before.get('barFloatsOnTop')}"
              f"  wide_at_rest={before.get('barWideAtRest')}"
              f"  scrolls={before.get('scrolls')}"
              f"  scrolled={after.get('barScrolled')}"
              f"  narrowed_to={after.get('barWidth')}"
              f"  spans_viewport={before.get('railSpansViewport')}")
        if not ok:
            failures.append(path)
    assert not failures, f"the rail is not a persistent column on: {failures}"
