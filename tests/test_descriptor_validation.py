"""Checks for the interface descriptor validator: the gate AI output must pass."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from workhub.descriptor import DescriptorError, assert_descriptor, validate_descriptor

failures = []


def check(name, condition, detail=""):
    if condition:
        print("PASS", name)
    else:
        failures.append(name)
        print("FAIL", name, detail)


def base(**overrides):
    descriptor = {
        "schema_version": "0.1",
        "interface_id": "design-sampling",
        "name": "设计部打样进度",
        "org_unit_code": "DESIGN",
        "position_codes": ["DESIGN_LEAD"],
        "modules": [
            {"key": "item", "type": "item_card", "title": "事项", "order": 1,
             "source_fields": ["title"], "aggregatable": True},
            {"key": "progress", "type": "progress_bar", "title": "打样进度", "order": 2,
             "source_fields": ["sample_done"], "aggregatable": True},
            {"key": "evidence", "type": "evidence_input", "title": "依据", "order": 3,
             "source_fields": ["evidence_ref"], "aggregatable": True},
            {"key": "state", "type": "metric_card", "title": "状态", "order": 4,
             "source_fields": ["state_word"], "aggregatable": True},
        ],
        "fields": [
            {"key": "title", "label": "款号", "type": "text", "required": True,
             "map_to": "title", "aggregatable": True, "example": "AW24-001"},
            {"key": "sample_done", "label": "完成打样数", "type": "number", "required": True,
             "map_to": "progress_value", "aggregatable": True, "example": "12"},
            {"key": "evidence_ref", "label": "打样单号", "type": "text", "required": True,
             "map_to": "evidence", "aggregatable": True, "example": "SY-2410"},
            {"key": "state_word", "label": "打样状态", "type": "enum", "required": True,
             "map_to": "status", "aggregatable": True, "example": "打样中"},
            {"key": "fabric_note", "label": "面料备注", "type": "text", "required": False,
             "map_to": "display_only", "aggregatable": False, "example": "待补货"},
        ],
        "status_map": {"待打样": "planned", "打样中": "in_progress", "等面料": "blocked", "已完成": "done"},
        "rules": {"evidence_required": True, "progress_range": [0, 100]},
        "meta": {"author_position_code": "DESIGN_LEAD", "version": 1,
                 "visibility": "department", "review": {"status": "draft"}},
    }
    descriptor.update(overrides)
    return descriptor


known = {"known_org_unit_codes": {"DESIGN"}, "known_position_codes": {"DESIGN_LEAD"}}

check("valid descriptor passes", validate_descriptor(base(), **known) == [])

missing_key = base()
del missing_key["name"]
check("missing required key rejected",
      any(p.startswith("name:") for p in validate_descriptor(missing_key, **known)))

bad_map = base()
bad_map["fields"][4] = {"key": "fabric_note", "label": "面料备注", "type": "text",
                        "map_to": "display_only", "aggregatable": True, "example": "x"}
check("display_only cannot aggregate",
      any("display_only requires aggregatable=false" in p for p in validate_descriptor(bad_map, **known)))

unknown_map = base()
unknown_map["fields"][1]["map_to"] = "vibes"
check("unknown map_to rejected", any("not a standard field" in p for p in validate_descriptor(unknown_map, **known)))

bad_status = base(status_map={"打样中": "in_progress", "等面料": "waiting"})
check("non-standard status rejected", any("not one of" in p for p in validate_descriptor(bad_status, **known)))

dangling = base()
dangling["modules"][0]["source_fields"] = ["ghost"]
check("unknown source field rejected",
      any("unknown field" in p for p in validate_descriptor(dangling, **known)))

custom_ok = base()
custom_ok["modules"].append({
    "key": "sampling_board", "type": "custom", "title": "打样看板", "order": 5,
    "source_fields": ["title"],
    "custom": {"purpose": "款号与状态一起看", "composed_of": ["list_block", "progress_bar"],
               "display": "table", "declares_logic": False},
})
check("constrained custom module allowed", validate_descriptor(custom_ok, **known) == [])

custom_logic = base()
custom_logic["modules"].append({
    "key": "rogue", "type": "custom", "title": "抓取", "order": 6, "source_fields": ["title"],
    "custom": {"purpose": "抓外部数据", "composed_of": ["list_block"], "display": "table",
               "declares_logic": True},
})
check("custom module declaring logic rejected",
      any("declares_logic must be false" in p for p in validate_descriptor(custom_logic, **known)))

smuggled = base()
smuggled["modules"][0]["script"] = "import os; os.system('rm -rf /')"
check("smuggled script rejected",
      any("forbidden key" in p for p in validate_descriptor(smuggled, **known)))

public_unapproved = base()
public_unapproved["meta"]["visibility"] = "public"
check("public template needs approval",
      any("status=approved" in p for p in validate_descriptor(public_unapproved, **known)))

public_approved = base()
public_approved["meta"]["visibility"] = "public"
public_approved["meta"]["review"] = {"status": "approved", "reviewed_by": "GM", "reviewed_at": "2026-10-13"}
check("approved public template allowed", validate_descriptor(public_approved, **known) == [])

no_evidence = base()
no_evidence["fields"] = [f for f in no_evidence["fields"] if f["map_to"] != "evidence"]
check("interface without evidence cannot aggregate",
      any("cannot be summarised" in p for p in validate_descriptor(no_evidence, **known)))

unknown_org = base(org_unit_code="NOPE")
check("unknown department rejected",
      any("unknown department" in p for p in validate_descriptor(unknown_org, **known)))

try:
    assert_descriptor(base(), **known)
    check("assert_descriptor passes silently", True)
except DescriptorError as exc:
    check("assert_descriptor passes silently", False, str(exc))

try:
    assert_descriptor(base(org_unit_code="NOPE"), **known)
    check("assert_descriptor raises with reasons", False)
except DescriptorError as exc:
    check("assert_descriptor raises with reasons", "unknown department" in str(exc))

if failures:
    print("")
    print("FAILED:", ", ".join(failures))
    raise SystemExit(1)
print("")
print("all descriptor checks passed")
