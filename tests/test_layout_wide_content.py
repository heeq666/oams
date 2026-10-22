"""The content column fills the screen next to the rail, at several window widths.

Wide screens should show more of the board rather than more empty margin. Measured
in a real browser because it is a claim about pixels. Requires headless Chrome and
node; both are skipped (not failed) when absent.
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
MEASURE = """(() => {
  const main = document.querySelector('.app-frame > main');
  const frame = document.querySelector('.app-frame');
  if (!main) return { error: 'no main' };
  const m = main.getBoundingClientRect();
  const f = frame.getBoundingClientRect();
  return {
    windowWidth: window.innerWidth,
    mainWidth: Math.round(m.width),
    mainRight: Math.round(m.right),
    frameWidth: Math.round(f.width),
    usedRatio: +(m.width / window.innerWidth).toFixed(3),
  };
})()"""


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


def measure(path, width):
    port = 9500 + random.randint(0, 400)
    profile = tempfile.mkdtemp(prefix="wh-width-")
    chrome = subprocess.Popen(
        [CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
         "--disable-dev-shm-usage", "--no-first-run",
         "--no-default-browser-check", f"--user-data-dir={profile}",
         f"--window-size={width},900", f"--remote-debugging-port={port}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ws_url = chrome_ws(port)
        out = subprocess.run(
            ["node", str(Path(__file__).resolve().parents[1] / "scripts" / "cdp-width.mjs"),
             ws_url, MEASURE, json.dumps([path])],
            capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr.strip()[-500:]
        return list(json.loads(out.stdout).values())[0]
    finally:
        chrome.kill()
        shutil.rmtree(profile, ignore_errors=True)


def test_layout_wide_content(site) -> None:
    skip_without_browser()
    # Sign in first in every profile: a bare /settings would redirect to /login
    # and there would be no .app-frame to measure.
    path = f"/login?token={admin_token()}&next=%2Fsettings"
    failures = []
    for width in (1280, 1680, 2200):
        data = measure(path, width)
        if "error" in data:
            print("FAIL", width, data)
            failures.append(width)
            continue
        # Content must use most of the space left of the rail, not stop at a cap.
        ok = data["usedRatio"] >= 0.80
        print(("PASS " if ok else "FAIL ") +
              f"window={width}  content={data['mainWidth']}px  "
              f"ratio_of_window={data['usedRatio']}")
        if not ok:
            failures.append(width)
    assert not failures, f"the content column does not adapt at widths: {failures}"
