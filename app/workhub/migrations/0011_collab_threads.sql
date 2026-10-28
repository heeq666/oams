-- 0011 — 协作室：会话、消息、已读位。
--
-- 为什么单独建三张表而不是复用 interface_entries：条目是「某人填的汇报」，
-- 消息是「几个人谈出来的过程」。前者要能汇总进公司口径，后者永远不进汇总。
-- 把讨论塞进条目，等于让每句闲话都成为一条待核对的汇报。
--
-- 三条设计约束直接落在表结构上：
--   1. 消息 append-only —— 没有 updated_at / deleted_at。设计已定「只能发，
--      不能改也不能撤回」，数据库层面就不给这个能力，比靠界面不画按钮可靠。
--   2. 未读只有一个已读位（collab_reads.last_seq），存服务端。跨设备天然一致，
--      不需要第二套同步逻辑。
--   3. 会话按「对象」唯一：一个部门一个管理群、一个页面一个汇报群、两个人一个
--      私聊。用部分唯一索引表达，而不是让应用层自己保证。
CREATE SCHEMA IF NOT EXISTS workhub;
SET LOCAL search_path = workhub, public;

CREATE TABLE collab_threads (
    thread_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- manage = 部门管理群（讨论页面怎么搭）；fill = 页面汇报群（讨论字段怎么填）；
    -- dm = 私聊。三类的产物不同，所以不是同一种东西的三个标签。
    kind          text NOT NULL CHECK (kind IN ('manage', 'fill', 'dm')),
    org_unit_id   uuid REFERENCES org_units(org_unit_id) ON DELETE CASCADE,
    interface_id  uuid REFERENCES interfaces(interface_id) ON DELETE CASCADE,
    -- 私聊的两个成员。约定 member_a < member_b，让同一对人只有一行。
    member_a      uuid REFERENCES workforce_members(member_id) ON DELETE CASCADE,
    member_b      uuid REFERENCES workforce_members(member_id) ON DELETE CASCADE,
    status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'closed')),
    title         text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT collab_threads_shape CHECK (
        (kind = 'manage' AND org_unit_id IS NOT NULL AND interface_id IS NULL)
     OR (kind = 'fill'   AND interface_id IS NOT NULL)
     OR (kind = 'dm'     AND member_a IS NOT NULL AND member_b IS NOT NULL AND member_a < member_b)
    )
);

-- 同类会话对一个对象只能有一个。
CREATE UNIQUE INDEX collab_threads_manage_uq ON collab_threads (org_unit_id)
    WHERE kind = 'manage';
CREATE UNIQUE INDEX collab_threads_fill_uq ON collab_threads (interface_id)
    WHERE kind = 'fill';
CREATE UNIQUE INDEX collab_threads_dm_uq ON collab_threads (member_a, member_b)
    WHERE kind = 'dm';

CREATE INDEX collab_threads_org_idx ON collab_threads (org_unit_id) WHERE status = 'active';
CREATE INDEX collab_threads_iface_idx ON collab_threads (interface_id) WHERE status = 'active';

CREATE TABLE collab_messages (
    message_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    thread_id     uuid NOT NULL REFERENCES collab_threads (thread_id) ON DELETE CASCADE,
    -- 线程内单调递增，排序与未读判断都靠它。用 max(seq)+1 在锁住线程行时取，
    -- 不用序列：序列跨线程留洞，且「第几条」这件事本来是按会话数的。
    seq           bigint NOT NULL,
    -- 发话的人。agent 代发时 author_id 仍是那个人（谁让 AI 说的），
    -- author_kind 如实写 agent —— 不允许 agent 冒充本人发言。
    author_id     uuid REFERENCES workforce_members (member_id) ON DELETE SET NULL,
    author_kind   text NOT NULL DEFAULT 'human' CHECK (author_kind IN ('human', 'agent')),
    agent_model   text,
    body          text NOT NULL CHECK (char_length(body) BETWEEN 1 AND 4000),
    -- @ 提及：本版不写，先建位，免得下一版改表。
    mentions      uuid[] NOT NULL DEFAULT '{}',
    -- 前端生成，断网重发靠它幂等：同一个 client_msg_id 只落一条。
    client_msg_id text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (thread_id, seq),
    UNIQUE (thread_id, client_msg_id),
    CONSTRAINT collab_messages_agent_has_model CHECK (
        author_kind <> 'agent' OR agent_model IS NOT NULL
    )
);

CREATE INDEX collab_messages_thread_seq_idx ON collab_messages (thread_id, seq DESC);

CREATE TABLE collab_reads (
    thread_id  uuid NOT NULL REFERENCES collab_threads (thread_id) ON DELETE CASCADE,
    member_id  uuid NOT NULL REFERENCES workforce_members (member_id) ON DELETE CASCADE,
    -- 已读到第几条。未读数 = 这个线程里 seq > last_seq 且不是自己发的条数。
    last_seq   bigint NOT NULL DEFAULT 0,
    read_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (thread_id, member_id)
);

COMMENT ON TABLE collab_threads IS '协作室会话：manage 部门管理群 / fill 页面汇报群 / dm 私聊';
COMMENT ON TABLE collab_messages IS '协作室消息，append-only：不更新、不删除、不撤回';
COMMENT ON TABLE collab_reads IS '未读的唯一来源：每个成员每个会话一个 last_seq，存服务端';
