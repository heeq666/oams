"""Renderer checks: descriptors become markup, and markup stays inert.

Keeps the 16 checks the v0.1 renderer shipped with, re-expressed for the v0.2
tree (containers plus the frozen module registry) as real pytest assertions
instead of the old print("PASS") script. Each test keeps the meaning of the
check it replaces.

Semantic descriptor validation lives in workhub.descriptor and is covered by
unit A own suite. The renderer only gates on what it needs in order to paint,
so "the sample descriptor is valid" is asserted here as "the renderer accepts
it" -- duplicating A rulebook from B suite would couple the two units release
timing for no benefit.
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from workhub.render import (  # noqa: E402
    MAX_RENDER_DEPTH,
    RenderError,
    known_module_keys,
    registry_source,
    render_interface,
    render_preview,
    render_read_only,
    resolve_spec,
)


def base(**overrides):
    """A small but realistic department page: containers plus leaves."""
    descriptor = {
        "schema_version": "0.2",
        "interface_id": "8f2c1a52-0000-4000-8000-000000000001",
        "name": "设计部打样进度",
        "org_unit_code": "DESIGN",
        "position_codes": ["DESIGN_LEAD"],
        "capabilities": ["entries:write"],
        "metrics": [],
        "modules": [
            {"type": "columns", "key": "root", "order": 1, "params": {"columns": 2},
             "children": [
                 {"type": "card", "key": "card-sample", "params": {"title": "本周打样"},
                  "children": [
                      {"type": "progress_row", "key": "row", "order": 1,
                       "source_fields": ["title", "status", "progress_value", "note"]},
                  ]},
                 {"type": "rows", "key": "right", "children": [
                     {"type": "metric_input", "key": "num", "order": 2,
                      "params": {"unit": "件"}, "source_fields": ["title", "metric_value"]},
                     {"type": "evidence_gallery", "key": "ev", "order": 3,
                      "source_fields": ["title", "evidence_url", "note"]},
                 ]},
             ]},
        ],
        "fields": [
            {"key": "title", "label": "款号", "type": "text", "required": True,
             "map_to": "title", "aggregatable": True, "example": "AW24-001"},
            {"key": "progress_value", "label": "完成打样数", "type": "percent",
             "required": True, "map_to": "progress_value", "aggregatable": True,
             "example": "12"},
            {"key": "evidence_url", "label": "打样单号", "type": "link", "required": False,
             "map_to": "evidence_url", "aggregatable": False,
             "example": "https://example.com/sy-2410.pdf"},
            {"key": "metric_value", "label": "打样件数", "type": "number", "required": True,
             "map_to": "metric_value", "aggregatable": True, "example": "12"},
            {"key": "status", "label": "打样状态", "type": "enum", "required": True,
             "map_to": "status", "aggregatable": True, "example": "打样中",
             "options": ["待打样", "打样中", "等面料", "已完成"]},
            {"key": "note", "label": "面料备注", "type": "text", "required": False,
             "map_to": "note", "aggregatable": True, "example": "待补货"},
        ],
        "status_map": {"待打样": "planned", "打样中": "in_progress",
                       "等面料": "blocked", "已完成": "done"},
        "rules": {"evidence_required": True, "progress_range": [0, 100]},
        "meta": {"author_position_code": "DESIGN_LEAD", "version": 1,
                 "visibility": "department", "review": {"status": "draft"}},
    }
    descriptor.update(overrides)
    return descriptor

# BODY_PLACEHOLDER
# --- the original 16 checks -------------------------------------------------

def test_sample_descriptor_renders_without_structural_errors():
    # was: "sample descriptor is valid" -- the renderer structural gate accepts it
    html = render_interface(base())
    assert html.startswith('<div class="wh-interface')
    assert 'data-mode="edit"' in html


def test_renders_interface_name():
    assert "设计部打样进度" in render_interface(base())


def test_renders_every_bound_field_label():
    html = render_interface(base())
    for label in ("款号", "完成打样数", "打样状态", "面料备注", "打样件数"):
        assert label in html, label


def test_renders_inputs_for_fields():
    html = render_interface(base())
    assert html.count("<input") >= 2
    assert "<select" in html


def test_marks_non_aggregatable_module():
    # evidence_gallery is aggregatable=False: the department is told so explicitly
    assert "不参与公司汇总" in render_interface(base())


def test_percent_maps_to_number_input():
    assert 'type="number" min="0" max="100"' in render_interface(base())


def test_renders_existing_values():
    html = render_interface(base(), values={"title": "AW24-001", "metric_value": "12"})
    assert 'value="AW24-001"' in html
    assert 'value="12"' in html


def test_rejects_invalid_descriptor():
    invalid = base()
    del invalid["name"]
    with pytest.raises(RenderError) as exc:
        render_interface(invalid)
    assert "name" in str(exc.value)


def test_escapes_descriptor_text():
    hostile = base()
    hostile["fields"][0]["label"] = "<script>alert(1)</script>"
    html = render_interface(hostile)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_escapes_submitted_values():
    html = render_interface(base(), values={"title": "<img onerror=alert(1)>"})
    assert "<img onerror" not in html


def test_escaping_is_visible_as_entities():
    html = render_interface(base(), values={"note": "<b>粗体</b>"})
    assert "&lt;b&gt;粗体&lt;/b&gt;" in html


def test_readonly_view_shows_data():
    assert "AW24-001" in render_read_only(base(), values={"title": "AW24-001"})


def test_readonly_hides_department_private_module():
    # evidence_gallery never leaves the department, in either direction
    html = render_read_only(base(), values={"evidence_url": "https://example.com/secret.pdf"})
    assert "secret.pdf" not in html


def test_readonly_has_no_inputs():
    html = render_read_only(base())
    assert "<input" not in html
    assert "<select" not in html
    assert "<textarea" not in html


def test_composed_container_renders_children():
    # was: "custom module renders". v0.2 retires the custom module: a bespoke
    # block is now a composition of registered modules, and the semantic that
    # a composed block still draws its content is what stays under test.
    html = render_interface(base())
    assert "本周打样" in html            # the card title, i.e. a container own text
    assert "款号" in html                # a leaf rendered two levels down
    assert html.count('data-type="card"') == 1


def test_composed_container_has_no_script():
    assert "<script" not in render_interface(base())


# --- checks added with the v0.2 renderer (not part of the original 16) ------

def test_rejects_unknown_module_type():
    bad = base()
    bad["modules"] = [{"type": "hero_banner", "key": "h"}]
    with pytest.raises(RenderError) as exc:
        render_interface(bad)
    assert "hero_banner" in str(exc.value)


def test_rejects_children_on_a_leaf_module():
    bad = base()
    bad["modules"] = [{"type": "note_block", "params": {"text": "x"},
                       "children": [{"type": "note_block", "params": {"text": "y"}}]}]
    with pytest.raises(RenderError) as exc:
        render_interface(bad)
    assert "不能有子模块" in str(exc.value)


def test_rejects_nesting_deeper_than_the_render_limit():
    node = {"type": "note_block", "params": {"text": "太深了"}}
    for _ in range(MAX_RENDER_DEPTH):
        node = {"type": "rows", "children": [node]}
    with pytest.raises(RenderError) as exc:
        render_interface(base(modules=[node]))
    assert "LAYOUT_TOO_DEEP" in str(exc.value)


def test_display_module_reads_aggregates_not_the_descriptor():
    descriptor = base(modules=[{"type": "metric_card",
                                "params": {"metric_key": "funnel.sample_done"}}])
    html = render_interface(descriptor, aggregates={"funnel.sample_done": 59})
    assert "已出样" in html
    assert "59" in html
    # without aggregates the card says so instead of inventing a number
    assert "\u2014" in render_interface(descriptor)


def test_preview_marks_its_viewpoint():
    # 角标（进汇总 / 不进汇总）已随 preview_tag 下线；preview 仍要说明自己是哪一视角
    html = render_preview(base())
    assert 'data-mode="preview"' in html


def test_preview_renders_disabled_widgets():
    # preview 是这页的图纸：控件要在场（看清长什么样），但一格都提交不了
    html = render_preview(base())
    found = re.findall(r'<(?:input|select|textarea)\b[^>]*class="wh-input"[^>]*>', html)
    assert found, "审核视角要看得见控件"
    for tag in found:
        assert "disabled" in tag, tag
        assert 'tabindex="-1"' in tag, tag
        assert "required" not in tag, tag


def test_every_registered_module_has_a_template():
    import workhub.render as render

    missing = [key for key in known_module_keys() if not render.has_template(key)]
    assert missing == []


def test_registry_comes_from_the_shared_module_table():
    # The renderer must not own a second copy of the module contract: once unit A
    # lands workhub.modules it is the only source of truth.
    keys = set(known_module_keys())
    assert len(keys) == 20
    assert registry_source() in ("workhub.modules", "contract-mirror")
    assert resolve_spec("metric_card") is not None
    assert resolve_spec("does_not_exist") is None


def test_containers_say_nothing_themselves():
    # Rule from concept 3.1: a container carries no data binding of its own, so a
    # bare container renders no field labels and no widgets.
    descriptor = base(modules=[{"type": "grid", "children": [
        {"type": "note_block", "params": {"text": "说明"}}]}])
    html = render_interface(descriptor)
    assert "说明" in html
    assert "<input" not in html
    assert 'class="wh-field-label"' not in html
