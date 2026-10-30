"""Measure the running site in a real browser: is the rail a persistent column?

Layout is a claim about pixels, so it is checked in a real browser rather than by
reading the served HTML. The browser is ego (see AGENTS.md「浏览器自动化」),
skipped (not failed) when it is not installed, and the page list is measured while
signed in as the seeded administrator.
"""
import pytest

from layout_browser import admin_token, measure, skip_without_browser

pytestmark = pytest.mark.db  # needs the served app plus a real browser

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
    // The capsule sits below the top edge and is 40px tall.
    barFloatsOnTop: Math.round(b.top) >= 8 && Math.round(b.top) <= 28
      && Math.round(b.height) === 40,
    // At rest the bar runs edge to edge (40px gutter), so the logo and the
    // account sit at the screen edges; the content column slides underneath.
    barWideAtRest: Math.round(b.width) >= window.innerWidth - 2,
    position: getComputedStyle(rail).position,
  };
})()"""


VIEWPORT = {"width": 1280, "height": 900}
# The shell scrolls the content column, not the window, so scroll whichever
# element actually owns the scrollbar before taking the second reading.
SCROLL = """document.querySelectorAll('.app-frame > main').forEach(function (m) {
  if (m.scrollHeight > m.clientHeight) m.scrollTop = 500;
});
window.scrollTo(0, 500);"""


def sign_in_path(token: str) -> str:
    """First navigation: exchange a session token for the cookie."""

    return "/login?token=" + token


def test_layout_persistent_rail(site, tmp_path) -> None:
    skip_without_browser()
    paths = [sign_in_path(admin_token())] + DEFAULT_PATHS
    report = measure(paths, MEASURE_JS, viewport=VIEWPORT,
                     scroll=SCROLL, tmp_path=tmp_path)
    failures = []
    for path in DEFAULT_PATHS:
        before, after = report[path]["before"], report[path]["afterScroll"]
        narrowed = (not before.get("scrolls")) or (
            after.get("barScrolled") and after.get("barWidth", 9999) <= 722)
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
