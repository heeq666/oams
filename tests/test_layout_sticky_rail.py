"""The department rail and top bar stay put; only the content pane scrolls.

The first version of this check scrolled the window, so it reported success while
the rail was actually sliding under the top bar. It now scrolls the content pane,
which is the region that scrolls in this shell, and asserts the rail never moves
and never hides behind the bar above it. The browser is ego (see AGENTS.md
「浏览器自动化」), skipped (not failed) when it is not installed.
"""
import pytest

from layout_browser import admin_token, measure, skip_without_browser

pytestmark = pytest.mark.db  # needs the served app plus a real browser

DEFAULT_PATHS = ["/board", "/progress", "/reports", "/settings"]
VIEWPORT = {"width": 1280, "height": 700}  # short enough that the content scrolls
SCROLL = "document.querySelector('.app-frame > main').scrollTop = 600"
PROBE = """(() => {
  const rail = document.querySelector('.app-rail');
  const main = document.querySelector('.app-frame > main');
  const bar = document.querySelector('header.topbar');
  if (!rail || !main) return { error: 'missing shell elements' };
  const r = rail.getBoundingClientRect();
  const b = bar ? bar.getBoundingClientRect() : null;
  return {
    railTop: Math.round(r.top), railBottom: Math.round(r.bottom), railHeight: Math.round(r.height),
    barBottom: b ? Math.round(b.bottom) : null,
    viewport: window.innerHeight,
    mainScrollTop: Math.round(main.scrollTop),
    mainScrollable: main.scrollHeight - main.clientHeight,
    railFullyVisible: r.top >= -1 && r.bottom <= window.innerHeight + 1,
    // The top bar floats above the frame (a capsule with gutters), so the rail
    // starts at the viewport top instead of being pushed below the bar.
    railSpansViewport: Math.abs(r.top) <= 2 && Math.abs(r.bottom - window.innerHeight) <= 2,
    barFloats: b ? (Math.abs(b.top - 20) <= 2 && Math.abs(b.height - 40) <= 2) : false,
    barScrolled: bar ? bar.classList.contains('is-scrolled') : false,
    barWidth: b ? Math.round(b.width) : null,
  };
})()"""


def test_layout_sticky_rail(site, tmp_path) -> None:
    skip_without_browser()
    paths = ["/login?token=" + admin_token()] + DEFAULT_PATHS
    report = measure(paths, PROBE, viewport=VIEWPORT, scroll=SCROLL,
                     tmp_path=tmp_path)

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
