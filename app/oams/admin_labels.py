"""Human wording for the two vocabularies the settings screen has to show.

The audit table and the permission table are both stored as machine keys --
`interface.approved`, `workforce.reports.submit`. Printing those keys to a
manager is a small insult; printing a wrong translation is a bigger one. So the
mapping is deliberately partial: a known key gets a short sentence, an unknown
key is shown verbatim rather than guessed at, and the screen stays honest as the
system grows new events.
"""

# Event type -> what happened, phrased so it can follow a person's name.
EVENT_LABELS = {
    "interface.created": "新建了页面版本",
    "interface.review_requested": "提交了页面送审",
    "interface.review_withdrawn": "撤回了送审",
    "interface.approved": "通过了页面版本",
    "interface.rejected": "退回了页面版本",
    "interface_entry.created": "填写了页面条目",
    "org_unit.created": "新建了部门",
    "position.created": "新建了岗位",
    "member.created": "新建了成员",
    "member.role_changed": "调整了岗位",
    "grant.created": "授予了权限",
    "grant.revoked": "收回了权限",
    "work_item.created": "新建了工作项",
    "report.submitted": "提交了汇报",
    "session.issued": "签发了登录令牌",
    "session.revoked": "吊销了登录令牌",
    "sensitive_field.read": "查看了人事敏感字段",
}

# resource -> the noun a manager would use.
CAPABILITY_NOUNS = {
    "workforce.org_units": "部门",
    "workforce.positions": "岗位",
    "workforce.members": "成员",
    "workforce.assignments": "任职",
    "workforce.grants": "权限",
    "workforce.work_items": "工作项",
    "workforce.reports": "汇报",
    "workforce.interfaces": "界面",
    "workforce.audit": "审计",
}

# action -> the verb. `read` is "查看" and not "读", because the sentence is
# about a person's reach, not a database operation.
CAPABILITY_VERBS = {
    "read": "查看",
    "manage": "管理",
    "submit": "提交",
    "review": "审核",
    "review_company": "公司终审",
    "issue": "签发",
}


def describe_event(event_type: str) -> str:
    """What happened, in a few words. Unknown keys come back as they are."""
    return EVENT_LABELS.get(event_type or "", event_type or "—")


def describe_capability(resource: str, action: str) -> str:
    """One capability, e.g. `workforce.reports.submit` -> `汇报·提交`."""
    noun = CAPABILITY_NOUNS.get(resource)
    verb = CAPABILITY_VERBS.get(action)
    if noun and verb:
        return f"{noun}·{verb}"
    if noun:
        return f"{noun}·{action}"
    return f"{resource}.{action}"


def summarize_capabilities(grants, limit: int = 3) -> str:
    """The first few capabilities, then a count of the rest.

    One position holding eighteen grants should read as reach, not as a wall of
    text: the screen shows the first few and says how many more there are.
    """
    labels = []
    for grant in grants:
        label = describe_capability(grant["resource"], grant["action"])
        if label not in labels:
            labels.append(label)
    if not labels:
        return "没有额外权限"
    shown = labels[:limit]
    rest = len(labels) - len(shown)
    text = " · ".join(shown)
    return f"{text}（另有 {rest} 项）" if rest > 0 else text
