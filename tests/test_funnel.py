"""Tests for workhub.funnel and workhub.datacenter (contract §7).

Pure functions only: no database, no network. The numbers asserted here are the
26D冬1-2波 measurements frozen in the P0 contract (§2) and the concept doc
(契约三), reproduced by tests/fixtures/datacenter_26d.json.

Run: .venv/bin/python -m pytest tests/test_funnel.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from workhub.datacenter import (  # noqa: E402
    DataCenter,
    DataCenterError,
    DataCenterHTTP,
    DataCenterNotConfigured,
    FixtureDataCenter,
    PRODUCT_FIELDS,
    TOOL_DEVELOPMENT_RECORDS,
    TOOL_SEARCH_PRODUCTS,
    build_datacenter,
    normalize_development,
    normalize_marketing,
    normalize_products,
    unwrap_tool_result,
)
from workhub.funnel import (  # noqa: E402
    FUNNEL_KEYS,
    SAMPLE_DONE_WORDS,
    SAMPLE_WIP_WORDS,
    compute_funnel,
    derive_style_sample_state,
    format_metric,
)

WAVE = "26D冬1-2波"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "datacenter_26d.json"


@pytest.fixture()
def dc():
    return FixtureDataCenter(FIXTURE)


class DictDataCenter:
    """A DataCenter built from literal dicts, for hand-made edge cases."""

    def __init__(self, products=None, development=None, marketing=None, waves=None):
        self._products = products or []
        self._development = development or {"styles": {}}
        self._marketing = marketing or {"records": [], "totals": {}}
        self._waves = waves or []

    def list_waves(self):
        return list(self._waves)

    def list_products(self, *, wave=None):
        return [dict(p) for p in self._products]

    def list_development(self, *, wave=None):
        return {"styles": {k: dict(v) for k, v in self._development["styles"].items()}}

    def list_marketing(self, *, wave=None):
        return dict(self._marketing)


def style(code, *, ordered=False, qty=0, amount=0, designed=True):
    return {
        "style_code": code,
        "wave": WAVE,
        "category": "毛衣",
        "has_design_image": designed,
        "is_ordered": ordered,
        "quantity": qty,
        "paid_amount": amount,
    }


# ---------------------------------------------------------------------------
# the frozen vocabulary
# ---------------------------------------------------------------------------

def test_done_words_contain_the_only_sampled_word():
    assert "已出样" in SAMPLE_DONE_WORDS


def test_wip_words_cover_the_four_named_by_the_concept_doc():
    # 契约三 line 137: 无色出样，且有颜色在 配料中/制版中/工艺中/制衣中
    for word in ("配料中", "制版中", "工艺中", "制衣中"):
        assert word in SAMPLE_WIP_WORDS


def test_done_and_wip_vocabularies_do_not_overlap():
    assert set(SAMPLE_DONE_WORDS).isdisjoint(SAMPLE_WIP_WORDS)


# ---------------------------------------------------------------------------
# derive_style_sample_state
# ---------------------------------------------------------------------------

def test_derive_done_when_any_colour_is_sampled():
    assert derive_style_sample_state(["已出样"]) == "done"


def test_derive_done_beats_everything_else():
    # The real 68-style data has 11 styles carrying several colour states at
    # once; one sampled colour is enough for the style level.
    assert derive_style_sample_state(["未开始", "已出样"]) == "done"
    assert derive_style_sample_state(["取消", "已出样", "配料中"]) == "done"


def test_derive_wip_when_no_colour_sampled_but_one_is_in_production():
    assert derive_style_sample_state(["制版中"]) == "wip"
    assert derive_style_sample_state(["未开始", "制衣中"]) == "wip"


def test_derive_pending_for_not_started_unfilled_or_empty():
    assert derive_style_sample_state(["未开始"]) == "pending"
    assert derive_style_sample_state(["未填写"]) == "pending"
    assert derive_style_sample_state([]) == "pending"


def test_derive_pending_for_negative_and_metadata_words():
    # 取消/失败 are terminal, 已安排 is scheduling-only: none of them means the
    # colour is being worked on right now.
    assert derive_style_sample_state(["取消", "失败", "已安排"]) == "pending"


def test_derive_is_order_independent_and_ignores_duplicates():
    states = ["配料中", "已出样", "未开始", "已出样"]
    assert derive_style_sample_state(states) == "done"
    assert derive_style_sample_state(list(reversed(states))) == "done"


def test_derive_ignores_unknown_words():
    assert derive_style_sample_state(["某个新状态"]) == "pending"


# ---------------------------------------------------------------------------
# the 26D冬1-2波 measured values (contract §2)
# ---------------------------------------------------------------------------

def test_funnel_reproduces_every_contract_number(dc):
    metrics = compute_funnel(dc, wave=WAVE)
    assert metrics == {
        "funnel.designed": 59,
        "funnel.sample_done": 59,
        "funnel.sample_wip": 1,
        "funnel.sample_pending": 8,
        "funnel.sample_open": 9,
        "funnel.priced": 13,
        "funnel.ordered": 29,
        "funnel.not_ordered": 39,
        "funnel.qty": 63289,
        "funnel.gmv": 17458700,
        "funnel.roi": pytest.approx(5.1751, abs=1e-4),
    }


def test_funnel_returns_exactly_the_eleven_contract_keys(dc):
    metrics = compute_funnel(dc, wave=WAVE)
    assert tuple(sorted(metrics)) == tuple(sorted(FUNNEL_KEYS))
    assert len(metrics) == 11


def test_funnel_matches_the_home_page_six_slots(dc):
    metrics = compute_funnel(dc, wave=WAVE)
    assert format_metric("funnel.sample_done", metrics["funnel.sample_done"]) == "59 款"
    assert format_metric("funnel.sample_open", metrics["funnel.sample_open"]) == "9 款"
    assert format_metric("funnel.ordered", metrics["funnel.ordered"]) == "29 款"
    assert format_metric("funnel.not_ordered", metrics["funnel.not_ordered"]) == "39 款"
    assert format_metric("funnel.gmv", metrics["funnel.gmv"]) == "¥1,745.87 万"
    assert format_metric("funnel.roi", metrics["funnel.roi"]) == "5.17"


def test_sample_buckets_partition_the_style_count(dc):
    metrics = compute_funnel(dc, wave=WAVE)
    styles = dc.list_products(wave=WAVE)
    assert (
        metrics["funnel.sample_done"]
        + metrics["funnel.sample_wip"]
        + metrics["funnel.sample_pending"]
        == len(styles)
        == 68
    )


def test_sample_open_is_wip_plus_pending(dc):
    metrics = compute_funnel(dc, wave=WAVE)
    assert metrics["funnel.sample_open"] == (
        metrics["funnel.sample_wip"] + metrics["funnel.sample_pending"]
    )


def test_ordered_and_not_ordered_partition_the_style_count(dc):
    metrics = compute_funnel(dc, wave=WAVE)
    assert metrics["funnel.ordered"] + metrics["funnel.not_ordered"] == 68


def test_multi_colour_style_nbck26151_derives_done(dc):
    # Real record: 灰=未开始, 棕杏燕麦=已出样. A naive "last state wins" or
    # "all colours must agree" rule gets this style wrong.
    development = dc.list_development(wave=WAVE)["styles"]
    assert development["NBCK26151"]["sample_states"] == ["未开始", "已出样"]
    assert derive_style_sample_state(development["NBCK26151"]["sample_states"]) == "done"


def test_two_styles_have_no_development_record_and_count_as_pending(dc):
    codes = {p["style_code"] for p in dc.list_products(wave=WAVE)}
    recorded = set(dc.list_development(wave=WAVE)["styles"])
    assert len(codes) == 68
    assert len(recorded) == 66
    assert codes - recorded == {"NBCK26066", "NBCK26067"}


def test_pricing_counts_the_latest_version_only(dc):
    development = dc.list_development(wave=WAVE)["styles"]
    statuses = [e.get("pricing_status") for e in development.values()]
    assert statuses.count("核价完成") == 13
    assert statuses.count("核价中") == 29
    assert statuses.count("取消") == 3
    assert statuses.count("未填写") == 1
    assert 46 == len([s for s in statuses if s])  # 46 styles have a pricing record


def test_designed_counts_has_design_image_not_style_count(dc):
    products = dc.list_products(wave=WAVE)
    assert sum(1 for p in products if p["has_design_image"]) == 59


# ---------------------------------------------------------------------------
# rules that are easy to break
# ---------------------------------------------------------------------------

def test_funnel_ignores_order_status_completely():
    # The fixture deliberately stores a contradictory order_status on every row:
    # the API ignores that parameter, so any aggregation using it would be wrong.
    products = [
        dict(style("A", ordered=True, qty=10, amount=100), order_status="未下单"),
        dict(style("B", ordered=False), order_status="已下单"),
    ]
    metrics = compute_funnel(DictDataCenter(products=products), wave=WAVE)
    assert metrics["funnel.ordered"] == 1
    assert metrics["funnel.not_ordered"] == 1
    assert metrics["funnel.qty"] == 10
    assert metrics["funnel.gmv"] == 100


def test_ordered_uses_is_ordered_true_not_truthy_strings():
    products = [
        style("A", ordered="false"),
        style("B", ordered="true"),
        style("C", ordered=1),
    ]
    metrics = compute_funnel(DictDataCenter(products=products), wave=WAVE)
    assert metrics["funnel.ordered"] == 2
    assert metrics["funnel.not_ordered"] == 1


def test_roi_is_none_when_no_spend_was_recorded():
    marketing = {"records": [], "totals": {"spend": 0, "marketing_sales": 6889366}}
    metrics = compute_funnel(
        DictDataCenter(products=[style("A")], marketing=marketing), wave=WAVE
    )
    assert metrics["funnel.roi"] is None
    assert format_metric("funnel.roi", None) == "—"


def test_roi_divides_sales_by_spend():
    marketing = {"records": [], "totals": {"spend": 1000, "marketing_sales": 2500}}
    metrics = compute_funnel(
        DictDataCenter(products=[style("A")], marketing=marketing), wave=WAVE
    )
    assert metrics["funnel.roi"] == pytest.approx(2.5)


def test_empty_datacenter_yields_zeroes_and_no_roi():
    metrics = compute_funnel(DictDataCenter(), wave=None)
    assert metrics["funnel.qty"] == 0
    assert metrics["funnel.gmv"] == 0
    assert metrics["funnel.ordered"] == 0
    assert metrics["funnel.sample_pending"] == 0
    assert metrics["funnel.roi"] is None


def test_style_with_colour_in_production_and_one_cancelled_is_wip():
    development = {"styles": {"A": {"sample_states": ["取消", "制衣中"]}}}
    metrics = compute_funnel(
        DictDataCenter(products=[style("A")], development=development), wave=WAVE
    )
    assert metrics["funnel.sample_wip"] == 1
    assert metrics["funnel.sample_pending"] == 0


# ---------------------------------------------------------------------------
# wave scoping
# ---------------------------------------------------------------------------

def test_wave_scoping_changes_the_numbers(dc):
    main = compute_funnel(dc, wave=WAVE)
    every = compute_funnel(dc, wave=None)
    assert every["funnel.qty"] > main["funnel.qty"]
    assert every["funnel.ordered"] > main["funnel.ordered"]


def test_unknown_wave_raises_a_helpful_error(dc):
    with pytest.raises(DataCenterError) as excinfo:
        dc.list_products(wave="不存在的波次")
    assert "不存在的波次" in str(excinfo.value)


def test_list_waves_comes_from_the_fixture(dc):
    assert dc.list_waves() == [WAVE, "26D冬3-4波"]


# ---------------------------------------------------------------------------
# format_metric
# ---------------------------------------------------------------------------

def test_format_metric_uses_the_contract_shapes():
    assert format_metric("funnel.qty", 63289) == "63,289 件"
    assert format_metric("funnel.gmv", 17458700) == "¥1,745.87 万"
    assert format_metric("funnel.roi", 5.1751) == "5.17"
    assert format_metric("funnel.sample_done", 59) == "59 款"


def test_format_metric_renders_none_as_dash():
    assert format_metric("funnel.roi", None) == "—"
    assert format_metric("funnel.gmv", None) == "—"


def test_format_metric_roi_truncates_rather_than_rounds():
    # 6889366 / 1331248 = 5.1751 -> 5.17, not 5.18.
    assert format_metric("funnel.roi", 6889366 / 1331248) == "5.17"


def test_format_metric_groups_thousands():
    assert format_metric("funnel.qty", 3176) == "3,176 件"
    assert format_metric("funnel.gmv", 12345678) == "¥1,234.57 万"


# ---------------------------------------------------------------------------
# normalisation of raw data-centre payloads
# ---------------------------------------------------------------------------

def test_normalize_products_maps_envelopes_and_types():
    payload = {
        "data": {
            "items": [
                {
                    "style": "NBCK26151",
                    "wave": WAVE,
                    "category": "毛衣",
                    "has_design_image": 1,
                    "is_ordered": "是",
                    "quantity": "1,200",
                    "paid_amount": "¥3,500.50",
                }
            ]
        }
    }
    rows = normalize_products(payload, wave=WAVE)
    assert len(rows) == 1
    row = rows[0]
    assert set(PRODUCT_FIELDS) <= set(row)
    assert row["style_code"] == "NBCK26151"
    assert row["has_design_image"] is True
    assert row["is_ordered"] is True
    assert row["quantity"] == 1200.0
    assert row["paid_amount"] == 3500.50


def test_normalize_products_skips_rows_without_a_style_code():
    rows = normalize_products({"items": [{"category": "毛衣"}, {"style_code": "A"}]})
    assert [r["style_code"] for r in rows] == ["A"]


def test_normalize_development_keeps_every_colour_word():
    payload = {
        "records": [
            {"style_code": "A", "sample_status": "未开始"},
            {"style_code": "A", "sample_status": "已出样"},
            {"style_code": "B", "sample_status": "制版中"},
        ]
    }
    styles = normalize_development(payload)["styles"]
    assert styles["A"]["sample_states"] == ["未开始", "已出样"]
    assert styles["B"]["sample_states"] == ["制版中"]


def test_normalize_development_takes_the_latest_pricing_version():
    payload = {
        "items": [
            {"style_code": "A", "pricing_status": "核价完成", "pricing_version": "v1"},
            {"style_code": "A", "pricing_status": "核价中", "pricing_version": "v2"},
            {"style_code": "B", "pricing_status": "核价中", "pricing_version": "v1"},
            {"style_code": "B", "pricing_status": "核价完成", "pricing_version": "v3"},
        ]
    }
    styles = normalize_development(payload)["styles"]
    assert styles["A"]["pricing_status"] == "核价中"
    assert styles["B"]["pricing_status"] == "核价完成"


def test_normalize_development_tolerates_rows_without_pricing():
    payload = {"items": [{"style_code": "A", "sample_status": "已出样"}]}
    styles = normalize_development(payload)["styles"]
    assert styles["A"].get("pricing_status") is None


def test_normalize_marketing_sums_rows_when_no_totals_block():
    payload = {"items": [{"spend": 100, "marketing_sales": 500},
                         {"spend": 50, "marketing_sales": 250}]}
    normalised = normalize_marketing(payload)
    assert normalised["totals"]["spend"] == 150
    assert normalised["totals"]["marketing_sales"] == 750


def test_normalize_marketing_prefers_the_totals_block():
    payload = {"items": [{"spend": 1, "marketing_sales": 2}],
               "totals": {"spend": 1331248, "marketing_sales": 6889366}}
    normalised = normalize_marketing(payload)
    assert normalised["totals"]["spend"] == 1331248
    assert normalised["totals"]["marketing_sales"] == 6889366


def test_unwrap_tool_result_handles_structured_content():
    assert unwrap_tool_result({"structuredContent": {"items": [1]}}) == {"items": [1]}
    assert unwrap_tool_result({"structuredContent": {"result": [1, 2]}}) == [1, 2]


def test_unwrap_tool_result_parses_text_blocks():
    result = {"content": [{"type": "text", "text": '{"items": [{"style_code": "A"}]}'}]}
    assert unwrap_tool_result(result) == {"items": [{"style_code": "A"}]}


# ---------------------------------------------------------------------------
# the HTTP client, exercised through an injected transport
# ---------------------------------------------------------------------------

def test_http_client_without_a_url_and_transport_is_not_configured(monkeypatch):
    monkeypatch.delenv("WORKHUB_DATACENTER_URL", raising=False)
    with pytest.raises(DataCenterNotConfigured) as excinfo:
        DataCenterHTTP()
    assert "WORKHUB_DATACENTER_URL" in str(excinfo.value)


def test_http_client_paginates_until_a_short_page():
    pages = {
        1: {"items": [{"style_code": "A"}, {"style_code": "B"}]},
        2: {"items": [{"style_code": "C"}]},
    }
    calls = []

    def transport(tool, arguments):
        calls.append((tool, dict(arguments)))
        return pages[arguments["page"]]

    client = DataCenterHTTP("http://example.invalid/mcp", transport=transport, page_size=2)
    rows = client.list_products(wave=WAVE)
    assert [r["style_code"] for r in rows] == ["A", "B", "C"]
    assert [c[1]["page"] for c in calls] == [1, 2]
    assert all(c[0] == TOOL_SEARCH_PRODUCTS for c in calls)
    assert all(c[1]["wave"] == WAVE for c in calls)


def test_http_client_deduplicates_overlapping_pages():
    def transport(tool, arguments):
        if arguments["page"] == 1:
            return {"items": [{"style_code": "A"}, {"style_code": "B"}]}
        return {"items": [{"style_code": "B"}]}

    client = DataCenterHTTP("http://example.invalid/mcp", transport=transport, page_size=2)
    assert [r["style_code"] for r in client.list_products(wave=WAVE)] == ["A", "B"]


def test_http_client_passes_the_wave_to_development_records():
    seen = {}

    def transport(tool, arguments):
        seen[tool] = dict(arguments)
        return {"items": []}

    client = DataCenterHTTP("http://example.invalid/mcp", transport=transport)
    client.list_development(wave=WAVE)
    assert seen[TOOL_DEVELOPMENT_RECORDS]["wave"] == WAVE


def test_http_client_supplies_a_data_center_protocol():
    client = DataCenterHTTP("http://example.invalid/mcp", transport=lambda t, a: {})
    assert isinstance(client, DataCenter)
    assert isinstance(FixtureDataCenter(FIXTURE), DataCenter)


def test_build_datacenter_honours_the_fixture_variable(monkeypatch):
    monkeypatch.setenv("WORKHUB_DATACENTER_FIXTURE", str(FIXTURE))
    built = build_datacenter()
    assert isinstance(built, FixtureDataCenter)
    assert compute_funnel(built, wave=WAVE)["funnel.ordered"] == 29


def test_build_datacenter_returns_an_explicit_instance_unchanged(dc):
    assert build_datacenter(dc) is dc


# ---------------------------------------------------------------------------
# fixture integrity
# ---------------------------------------------------------------------------

def test_fixture_declares_its_provenance():
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    meta = raw["meta"]
    assert meta["main_wave"] == WAVE
    assert "契约" in meta["source"]
    assert meta["expected_main_wave"]["funnel.ordered"] == 29


def test_fixture_expected_values_match_what_the_code_computes(dc):
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    expected = raw["meta"]["expected_main_wave"]
    actual = compute_funnel(dc, wave=WAVE)
    for key, value in expected.items():
        if key == "funnel.roi":
            assert actual[key] == pytest.approx(value, abs=1e-2)
        else:
            assert actual[key] == value, key
