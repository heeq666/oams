# 建一页、过一审（一页纸）

给真人试用：**一个部门管理员自己建一页 → 一个部门负责人初审通过**。全程点鼠标，不用装东西。

下面用设计部举例，页面叫「设计部本周进度」。

## 1. 进设计部工作台

登录后点左栏「**设计部**」。页面条最右边是「**＋ 新页面**」。

![部门工作台入口](assets/screenshots/trial-onepager/03-部门工作台-入口.png)

## 2. 填三个名字，贴一段页面描述

- **页面名称**：设计部本周进度
- **页面标识**：`design-weekly-progress`（小写英文，别用中文）
- **填写岗位**：留空就行（留空＝部门里谁都能填）
- **页面描述（JSON）**：把下面整段贴进去

```json
{
  "schema_version": "0.2",
  "interface_id": "design-weekly-progress",
  "name": "设计部本周进度",
  "org_unit_code": "DESIGN",
  "position_codes": ["DESIGNER"],
  "capabilities": ["entries:write", "entries:read:dept", "aggregate:read:dept"],
  "modules": [
    {
      "key": "rows",
      "type": "progress_row",
      "order": 1,
      "module_version": "^1.0",
      "source_fields": ["item", "state", "proof"],
      "aggregatable": true
    }
  ],
  "fields": [
    {"key": "item",  "label": "事项", "type": "text", "required": true, "map_to": "title",    "aggregatable": true, "example": "秋1波主推款定稿"},
    {"key": "state", "label": "状态", "type": "enum", "required": true, "map_to": "status",   "aggregatable": true, "example": "进行中", "options": ["未开始", "进行中", "卡住了", "已完成"]},
    {"key": "proof", "label": "依据", "type": "text", "required": true, "map_to": "evidence", "aggregatable": true, "example": "定稿评审记录 10-30"}
  ],
  "status_map": {"未开始": "planned", "进行中": "in_progress", "卡住了": "blocked", "已完成": "done"},
  "rules": {"evidence_required": true, "blocked_requires_reason": true},
  "meta": {"author_position_code": "DESIGNER", "version": 1, "visibility": "department"}
}
```

**这是 AI 帮你生成的页面描述，整段贴进去就行，一个字都不用改。** 它只有三栏：事项、状态、依据。

填完点「**保存并预览**」（不是「保存」）。

![新建页表单](assets/screenshots/trial-onepager/01-新建页-表单.png)

## 3. 自己先试填一遍，再提交

预览里就能直接填内容试试（事项／状态／依据）。觉得没问题，点「**提交审核**」。

页面状态变成「待提交」/审核中。**这时候你填的东西只在本部门可见，不进公司汇总**，可以随便试、可以撤回。

## 4. 换负责人登录，初审通过

负责人登录后点顶部「**页面审核**」，会看到一张卡片：**设计部 · 设计部本周进度**。两个按钮：

- 「**初审通过**」——通过后这页立刻能用；
- 「**退回部门**」——必须选一个原因类型、写一句哪儿不行。

![审核页](assets/screenshots/trial-onepager/02-审核页-待我审.png)

如果这页要**对外公开**或**进公司汇总**，通过后会提示「**已转到公司终审**」，负责人这边的队列就清空了，等公司那边再签一次。

## 5. 卡住了去哪看

- **作者这边**：回「设计部」工作台，你那一页的标签上有「**待提交**」标记，说明还在等审核。
- **负责人这边**：审核页里没有这张卡，就是**不归你签**（可能已经转到公司终审了）。审核页最下面如果有一张「**别处还在等**」的表，最后一列「等谁签」会写着是部门初审还是公司终审——这张表只有平台权限的人能看到。

---

*这是演示站、演示数据，随便试，试坏了也不影响真实数据。*
