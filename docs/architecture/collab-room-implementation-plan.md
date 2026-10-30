# 协作室 · 实现清单

> 状态：**本版已实现**（2026-10-27 立，2026-10-30 按实现回填；原清单有三处与实现不符，已在 §1.1、§4、§5 就地改正）
> 前置：设计已定稿，见 `docs/design/design-system-v1.md` §6 与 §7「协作室」硬约束、Figma 页 `02 · 组件库` 的协作室段（`146:4401`）。
> 定位一句话：**协作室是页面的讨论层，不是第二个系统。** 它只解决「口径互通」，不做审批、不做采纳、不授予任何权限。

---

## 1. 范围

### 这一版要做

一条能用的最小链路：

1. 右下角入口按钮，显示服务端算出来的总未读数
2. 点开 = 底部两栏弹层，左栏会话列表、右栏消息流
3. 能看、能发、点开会话即清零未读
4. 三类会话都在：部门管理群、页面汇报群、私聊

### 这一版不做（但接口要留位）

| 项 | 为什么现在不做 | 留什么位 |
|---|---|---|
| @ 提及 | 要先有「成员行」的检索与选择器 | `collab_message.mentions uuid[]` 字段先建，写入留空 |
| 搜索（会话/消息/同事） | 三样混搜是一个独立检索面，牵涉索引与排序 | 面板的搜索框先只做「筛会话名」，服务端保留 `q` 参数 |
| 加载更早 | 先只取最近一批，够验证「有没有人说话」 | 消息接口**从第一天就收 `before_seq`**，前端的加载行组件已在 Figma 备好 |
| AI 代发（MCP 写消息） | 先让人的链路跑通，再加 agent 身份 | `author_kind`、`agent_model` 两列先建 |
| 已读回执、对方正在输入 | 都是实时能力，与「不做实时推送」冲突 | 不做 |

**不做实时推送**是本版的明确取舍：未读在下一次拉取时对齐（进页面、开面板、切会话）。理由是要长连接，而收益只是徽章早两秒消失。真要实时，是另一个决定，不藏在这次里。

### 1.1 本版已完成（2026-10-30 回填）

上面「这一版要做」的四条都已实现：

1. 右下角入口按钮，显示服务端算出来的总未读数。
2. 点开 = 底部两栏弹层，左栏会话列表、右栏消息流。
3. 能看、能发；点开会话即清零未读。
4. 三类会话都在：管理群（`manage`）、汇报群（`fill`）、私聊（`dm`）。

剩余已知缺口（三条，都在后端）：

1. 发送失败暂无可见反馈（幂等已备、前端未接）。
2. 面板关闭重开复用旧 DOM，未读数仍是旧值。
3. `unread_total` 存在 N+1 查询。

另有一条测试缺口：`can_see` / 幂等 / `mark_read` / `unread_total` 四条核心规则尚无覆盖。

---

## 2. 数据模型（迁移 `0011_collab_threads.sql`）

沿用现有约定：schema `workhub`、主键 uuid、`created_at timestamptz`、状态用 text + CHECK 而不是 enum。

### 2.1 会话

```sql
CREATE TABLE collab_threads (
    thread_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind         text NOT NULL CHECK (kind IN ('manage', 'fill', 'dm')),
    -- manage: 按部门;fill: 按页面;dm: 两个人
    org_unit_id  uuid REFERENCES org_units(org_unit_id) ON DELETE CASCADE,
    interface_id uuid REFERENCES interfaces(interface_id) ON DELETE CASCADE,
    member_a     uuid REFERENCES workforce_members(member_id),
    member_b     uuid REFERENCES workforce_members(member_id),
    status       text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'closed')),
    title        text,                    -- dm 不存标题,显示对方名字
    created_at   timestamptz NOT NULL DEFAULT now(),
    CHECK (
        (kind = 'manage' AND org_unit_id IS NOT NULL AND interface_id IS NULL)
     OR (kind = 'fill'   AND interface_id IS NOT NULL)
     OR (kind = 'dm'     AND member_a IS NOT NULL AND member_b IS NOT NULL AND member_a < member_b)
    )
);
```

三个唯一索引，保证「同类会话一个对象只有一个」：

```sql
CREATE UNIQUE INDEX collab_threads_manage_uq ON collab_threads(org_unit_id)
    WHERE kind = 'manage';
CREATE UNIQUE INDEX collab_threads_fill_uq   ON collab_threads(interface_id)
    WHERE kind = 'fill';
CREATE UNIQUE INDEX collab_threads_dm_uq     ON collab_threads(member_a, member_b)
    WHERE kind = 'dm';
```

**不建「部门全员群」**。部门成员本来就能看本部门全部条目，再开一个全员群只会变成灌水区。

### 2.2 消息

```sql
CREATE TABLE collab_messages (
    message_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    thread_id     uuid NOT NULL REFERENCES collab_threads(thread_id) ON DELETE CASCADE,
    seq           bigint NOT NULL,        -- 线程内单调递增,排序与未读都靠它
    author_id     uuid REFERENCES workforce_members(member_id),
    author_kind   text NOT NULL DEFAULT 'human' CHECK (author_kind IN ('human', 'agent')),
    agent_model   text,                   -- 'Codex' / 'WorkBuddy';human 为 NULL
    body          text NOT NULL CHECK (length(body) BETWEEN 1 AND 4000),
    mentions      uuid[] NOT NULL DEFAULT '{}',
    client_msg_id text,                   -- 前端生成,重发幂等
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (thread_id, seq),
    UNIQUE (thread_id, client_msg_id)
);
```

- **append-only**：没有 `updated_at`、没有 `deleted_at`。消息只能发，不能改也不能撤回（设计已定）。
- `seq` 由服务端在插入时 `SELECT coalesce(max(seq),0)+1 FROM collab_messages WHERE thread_id = %s FOR UPDATE`（锁线程行，不要用序列，序列会跨线程留洞）。
- `client_msg_id` + 唯一约束 = 断网重试不会发两条。

### 2.3 已读

```sql
CREATE TABLE collab_reads (
    thread_id  uuid NOT NULL REFERENCES collab_threads(thread_id) ON DELETE CASCADE,
    member_id  uuid NOT NULL REFERENCES workforce_members(member_id) ON DELETE CASCADE,
    last_seq   bigint NOT NULL DEFAULT 0,
    read_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (thread_id, member_id)
);
```

**未读只有一份、存在服务端**：`未读数 = count(messages where seq > last_seq and author_id <> me)`。自己发的不算。跨设备天然一致。

### 2.4 索引

```sql
CREATE INDEX collab_messages_thread_seq ON collab_messages(thread_id, seq DESC);
CREATE INDEX collab_threads_org  ON collab_threads(org_unit_id) WHERE status = 'active';
CREATE INDEX collab_threads_iface ON collab_threads(interface_id) WHERE status = 'active';
```

---

## 3. 权限：不新增权限点

**群不做权限边界**——能看这个页面的人就能看这个页面的群。

| 会话 | 谁看得见 | 对应现有判断 |
|---|---|---|
| 管理群（`kind=manage`） | 该部门的管理员 | `administered_org_unit_ids()` 含该部门，或 `has_company_scope()` |
| 汇报群（`kind=fill`） | 该页面 `position_id` 指向的岗位的人 + 该部门管理员 | `interface_entries.reported_by` 同款判断 + 部门管理员 |
| 私聊（`kind=dm`） | 两位成员本人 | `member_id IN (member_a, member_b)` |

三条必须写进代码注释的规则：

1. **群里的「通过」不算通过**。审批只能回 `/reviews`，协作室没有任何写 `interfaces.status` 的路径。
2. **入群不等于拿到权限**。可见性由上面那张表算，不由「是否在群里」算。
3. **群解散不删消息**。`status='closed'` 只关入口，`DELETE` 一律不用。

---

## 4. 服务端路由（`app/workhub/web.py`）

全部返回 htmx 片段，主面板一次渲染骨架。沿用 `_require()` / `_denied()` / `connection()`。

| 方法 | 路径 | 返回 | 说明 | `web.py` |
|---|---|---|---|---|
| GET | `/collab` | 整页 | 面板骨架 + 左栏列表；未选会话时右栏是**空态**（没有默认会话，用户自己点）。只打开面板不标已读 | `:2098` |
| GET | `/collab/side` | 片段 | 左栏容器（`view=threads\|directory`）：搜索、切视图、通讯录滚动都换这一段 | `:2139` |
| GET | `/collab/threads` | 片段 | 只要会话那一版的左栏列表，留给测试与文档里的旧链接 | `:2154` |
| GET | `/collab/open` | 片段 | 首屏：点一个「还没人说过话」的对象，这一刻建线程并落已读 | `:2166` |
| GET | `/collab/threads/{thread_id}` | 片段 | 右栏：群头 + 消息列表 + 输入区；打开即落已读 | `:2194` |
| GET | `/collab/threads/{thread_id}/messages` | 片段 | `before_seq` 可选；带出加载行 | `:2213` |
| POST | `/collab/threads/{thread_id}/messages` | 片段 | 新消息 + 输入区复位；失败时草稿回填 | `:2239` |
| GET | `/collab/unread.json` | JSON | `{total: n}`，入口按钮用 | `:2269` |

八条全部是 `@router.get` / `@router.post`，不是 `@app`。按表验收时八条都要点到，`/collab/open`（首屏建线程 + 落已读）和 `/collab/side`（左栏分片）最容易漏。

**打开会话 = 已读，不单独提供 `POST /read`。** `GET /collab/threads/{thread_id}` 时服务端直接落 `read_at`（写 `last_seq = max(seq)`）；首次进群的 `GET /collab/open` 同样在服务端落已读。htmx 场景下省掉一次往返，这是有意的。验收口径：打开会话后 `unread.json` 里该会话归零。

**入口按钮的未读数从哪里来**：不强求实时。进任何页面时由 `base.html` 的服务端上下文带出（跟 `request.state.nav` 同一处算），开面板后再刷一次。不做轮询。

会话列表 SQL 的要点（一次查完，别在模板里 N+1）：

```sql
WITH visible AS (
    SELECT t.thread_id, t.kind, t.title, t.interface_id, t.org_unit_id, t.created_at,
           CASE t.kind WHEN 'manage' THEN 1 WHEN 'fill' THEN 2 ELSE 3 END AS grp
      FROM collab_threads t
     WHERE t.status = 'active' AND ( /* 上面第 3 节那三种可见性 */ )
)
SELECT v.*, m.body AS last_body, m.created_at AS last_at,
       (SELECT count(*) FROM collab_messages x
         WHERE x.thread_id = v.thread_id
           AND x.seq > coalesce(r.last_seq, 0)
           AND x.author_id <> %(me)s) AS unread
  FROM visible v
  LEFT JOIN LATERAL (SELECT body, created_at FROM collab_messages
                      WHERE thread_id = v.thread_id ORDER BY seq DESC LIMIT 1) m ON true
  LEFT JOIN collab_reads r ON r.thread_id = v.thread_id AND r.member_id = %(me)s
 ORDER BY v.grp, coalesce(m.created_at, v.created_at) DESC;
```

**会话在什么时候被创建**：没人说话之前不建行。左栏的群来自「你可见的页面」与「你所在的部门」，点进去才 `INSERT ... ON CONFLICT DO NOTHING` 建线程——否则每个页面都挂一个空群，左栏立刻变成垃圾场，且没法区分「这个群还没人说过话」和「这个群不存在」。

---

## 5. 前端：组件对着 Figma 抄，不重新设计

| 页面元素 | Figma 组件 | 节点 |
|---|---|---|
| 右下角入口 | 协作室 · 入口按钮（3 变体） | `146:4418` |
| 左栏一行会话 | 协作室 · 会话行（类型×状态×提及） | `148:6356` |
| 会话分组标题 | 分组标题 · 协作室 | `172:11318` |
| 右栏群头 | 协作室 · 群头（带返回 / 无返回） | `172:11317` |
| 一条消息 | 协作消息条（发言×状态×提及） | `146:4445` |
| 消息内输入框 | 协作室 · 输入框（单行 / 多行） | `172:11629` |
| 加载更早 | 协作室 · 加载更早（2 变体） | `148:9391` |
| 空群 / 取数失败 | 两栏面板的两个视图变体 | `173:11988` / `173:12150` |
| 整块面板 | 协作室 · 两栏面板（10 变体） | `148:7054` |

实现约束（照抄 §7 硬约束，不重复发明）：

- **入口是唯一入口**，顶部导航不加第五项。
- 面板**上两角 28、下两角 0**，三档宽 1313 / 960 / 704，高度两档 560 / 891；<900 只留右栏，左上角变「‹」返回。
- 消息列表 `primaryAxisAlignItems: MAX` 贴着输入框排。
- AI 代发时人名后跟浅紫 `model` 徽章；本人发言不带。
- 点开会话即清零、**不画「以下是新消息」的分界线**。

样式落在**独立样式表** `app/workhub/static/workhub-collab.css`（由 `base.html` 单独引入），不并进 `workhub-modules.css`；它同样只用 `--wh-*` 令牌，不写字面颜色和圆角值。模板在 `templates/collab/` 目录：`panel.html`、`thread_list.html`、`thread.html`、`messages.html`、`composer.html`，另有 `side.html`、`directory.html`、`directory_intro.html`、`earlier.html`、`empty.html`、`error.html`、`icons.html`。

---

## 6. 里程碑与顺序

按这个顺序做，每一步都能单独验收：

| # | 交付 | 验收 |
|---|---|---|
| 1 | 迁移 `0011` + 读写三个服务端函数 | `pytest` 跑得动；手工 `INSERT` 两条消息，`seq` 递增 |
| 2 | `/collab` 骨架 + 左栏列表（空态） | 能打开面板，看到三类分组，会话行是组件样子 |
| 3 | 点进会话 + 消息流 + 发送 | 发一条能在另一浏览器看到；`client_msg_id` 重发不重复 |
| 4 | 未读：服务端算 + 入口按钮数字 + 点开清零 | 两个浏览器：A 发，B 的徽章 +1，B 点开，A、B 都归零 |
| 5 | 空态 / 加载失败 / 日期分隔（发送失败反馈本版未接，见 §1.1 缺口 1） | 断网发一条不产生重复消息，重连后重发成功；红色感叹号尚未出现 |
| 6 | 三档宽度（1440 / 1024 / 768） | 三张截图，768 无横向滚动 |

**里程碑 4 是这一版真正的验收点**：未读跨设备一致，才证明「服务端只有一个已读位」这件事落实了。

---

## 7. 下一版（MCP 侧，本次不做）

等人都用起来再加，工具就三个，命名与现有 12 个工具一致：

- `list_collab_threads()` —— 我能看见哪些群、各自未读
- `read_thread(thread_id, before_seq=None)` —— 读消息
- `post_thread_message(thread_id, body, mentions=())` —— 以 `author_kind='agent'`、`agent_model` 写入

三条铁律写在工具说明里（与 huddo 的 `[peer]` 标签同一立场）：

1. **在群里说话不产生任何权限**。agent 能用什么工具，仍由它在 MCP 侧的 capability 决定。
2. **群里的结论不是数据**。要落成条目得走 `interface_entries`，协作室改不了任何页面的字段映射。
3. **agent 不能以人的身份发言**。`author_kind` 必须如实写 `agent`，`agent_model` 必填。

---

## 8. 已知风险

- **左栏会长期空旷**。项目刚起步，管理群一个部门一个、汇报群一个页面一个，私聊没人开。前两周打开大概率只有一两个群。这是正常状态，不要为了「热闹」加部门全员群。
- **不做实时 = 徽章会旧**。员工停在页面上不动，别人发了消息他不知道。可接受，但如果试用反馈「漏看」，第一个该讨论的是要不要给徽章做个低频轮询（30 秒），而不是上 SSE。
- **消息长度上限 4000**。超过就在前端拦，不要等数据库 CHECK 报错。
