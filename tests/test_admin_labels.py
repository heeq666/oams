"""The settings screen prints machine keys out loud unless someone translates them.

Two vocabularies need it: audit event types (`interface.approved`) and
permission grants (`workforce.reports.submit`). The rule these tests protect is
that a *known* key turns into a short sentence and an *unknown* key is shown
as-is -- a new event type must never be silently relabelled into something it is
not.
"""

from workhub import admin_labels


def test_known_events_read_as_actions():
    assert admin_labels.describe_event("interface.approved") == "通过了页面版本"
    assert admin_labels.describe_event("interface.rejected") == "退回了页面版本"
    assert admin_labels.describe_event("org_unit.created") == "新建了部门"


def test_unknown_event_is_passed_through_not_guessed():
    assert admin_labels.describe_event("interface.teleported") == "interface.teleported"
    assert admin_labels.describe_event("") == "—"
    assert admin_labels.describe_event(None) == "—"


def test_capability_reads_as_noun_verb():
    assert admin_labels.describe_capability("workforce.reports", "submit") == "汇报·提交"
    assert admin_labels.describe_capability("workforce.interfaces", "review") == "界面·审核"


def test_capability_falls_back_one_half_at_a_time():
    # unknown resource: both halves are printed, so the reader can see why
    assert admin_labels.describe_capability("payroll.runs", "read") == "payroll.runs.read"
    # known resource, unknown action: the noun still helps
    assert admin_labels.describe_capability("workforce.reports", "export") == "汇报·export"


def test_capability_summary_counts_the_rest():
    grants = [
        {"resource": "workforce.reports", "action": "read"},
        {"resource": "workforce.reports", "action": "submit"},
        {"resource": "workforce.work_items", "action": "manage"},
        {"resource": "workforce.interfaces", "action": "review"},
        {"resource": "workforce.audit", "action": "read"},
    ]
    text = admin_labels.summarize_capabilities(grants)
    assert text.startswith("汇报·查看 · 汇报·提交 · 工作项·管理"), text
    assert text.endswith("（另有 2 项）"), text


def test_capability_summary_drops_duplicate_wording():
    grants = [
        {"resource": "workforce.reports", "action": "read"},
        {"resource": "workforce.reports", "action": "read"},
    ]
    assert admin_labels.summarize_capabilities(grants) == "汇报·查看"


def test_position_without_grants_says_so():
    assert admin_labels.summarize_capabilities([]) == "没有额外权限"
