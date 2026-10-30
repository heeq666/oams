"""The content column fills the screen next to the rail, at several window widths.

Wide screens should show more of the board rather than more empty margin. Measured
in a real browser, because it is a claim about pixels; the browser is ego (see
AGENTS.md「浏览器自动化」), skipped (not failed) when it is not installed.
"""
import pytest

from layout_browser import admin_token, measure, skip_without_browser

pytestmark = pytest.mark.db  # needs the served app plus a real browser


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


def test_layout_wide_content(site, tmp_path) -> None:
    skip_without_browser()
    # Sign in first: a bare /settings would redirect to /login and there would be
    # no .app-frame to measure. The driver confirms the landing page before it
    # starts measuring, so a stale cookie cannot turn this into a login-screen run.
    login = f"/login?token={admin_token()}&next=%2Fsettings"
    failures = []
    for width in (1280, 1680, 2200):
        data = measure(["/settings"], MEASURE, viewport={"width": width, "height": 900},
                       tmp_path=tmp_path, login=login)["/settings"]["before"]
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
