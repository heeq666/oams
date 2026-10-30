"""The demo pages in `data/` must be openable: they validate and they render.

Two of them ("设计部款式开发打样进度", "设计部周测试页") shipped as schema 0.1 and sat in
the demo database as pages nobody could open: the renderer refused `item_card`,
`list_block`, `progress_bar` and `evidence_input`, and the refusal surfaced as
"you have no permission" because it went through `_denied`. That cost a day of
looking in the wrong place, so the descriptors that seed the trial environment are
checked here instead of only by hand in a browser.

Everything is pure: no database, no HTTP, no clock.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from workhub.descriptor import validate_descriptor  # noqa: E402
from workhub.render import RenderError, render_interface, render_read_only  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_PAGES = [
    REPO_ROOT / "data" / "live-design-interface.json",
    REPO_ROOT / "data" / "design-style-development.json",
    REPO_ROOT / "data" / "design-weekly-test.json",
]


def _descriptor(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", DEMO_PAGES, ids=[p.name for p in DEMO_PAGES])
def test_a_demo_page_passes_validation(path: Path) -> None:
    descriptor = _descriptor(path)
    problems = validate_descriptor(
        descriptor, known_org_unit_codes={"DESIGN"}, known_position_codes={"DESIGN_LEAD"})
    assert problems == []


@pytest.mark.parametrize("path", DEMO_PAGES, ids=[p.name for p in DEMO_PAGES])
def test_a_demo_page_renders_in_both_viewpoints(path: Path) -> None:
    descriptor = _descriptor(path)
    edit = render_interface(descriptor, values={})
    assert "wh-m" in edit
    read = render_read_only(descriptor, values={})
    assert "wh-m" in read


SLUGS = {
    "live-design-interface.json": "design-sampling",
    "design-style-development.json": "design-style-development",
    "design-weekly-test.json": "design-weekly-test",
}


@pytest.mark.parametrize("path", DEMO_PAGES, ids=[p.name for p in DEMO_PAGES])
def test_a_demo_page_keeps_its_identity(path: Path) -> None:
    """The slug is what the seeder keys on; renaming a file must not rename a page."""
    descriptor = _descriptor(path)
    assert descriptor["interface_id"] == SLUGS[path.name]
    assert descriptor["schema_version"] == "0.2"
    assert descriptor["org_unit_code"] == "DESIGN"
