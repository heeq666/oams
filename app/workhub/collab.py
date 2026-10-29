"""协作室：会话、消息、已读位。

为什么要单独一个模块而不是塞进 web.py：协作室的三条规则（谁能看见哪个群、
什么算未读、消息只能发不能改）是被反复问到的判断题，写成模块里的函数之后，
页面、将来的 MCP 工具、以及测试引用的是同一份答案。放在路由里就会出现
第二个判断，然后两处慢慢分叉。

三条规则，这里是怎么落实的：

1. **群不做权限边界**。能看见一个页面的人就看得见这个页面的群。可见性由
   `can_see` 一个函数算，不由「是否在群里」算——所以入群这件事本身不带来任何
   能力，也没有「加人」这个动作。
2. **未读只有一份**。`collab_reads.last_seq` 是唯一来源，未读数永远由它算出来，
   不存计数器。跨设备一致是免费的，而不是需要同步的第二份状态。
3. **消息 append-only**。这个模块里没有任何 UPDATE 或 DELETE 打在 collab_messages
   上。要「改」一条消息，唯一的办法是再发一条。

会话不是预先建好的：左栏列的是「对象」（部门 / 页面 / 同事），点进去那一刻才
建线程。否则每个页面都挂一个空群，左栏立刻变成垃圾场，而且分不清「这个群没人
说过话」和「这个群根本不存在」——这两种状态界面完全不同。
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from workhub.security import (
    ADMIN_RESOURCES,
    active_org_unit_ids,
    administered_org_unit_ids,
    has_company_scope,
)

#: 一条消息的长度上限。与迁移里的 CHECK 一致；前端先拦，不要等数据库报错。
MAX_BODY = 4000

#: 三类会话，与迁移里的 CHECK 同名。分组顺序就是左栏从上到下的顺序。
KIND_MANAGE = "manage"
KIND_FILL = "fill"
KIND_DM = "dm"
KIND_ORDER = (KIND_MANAGE, KIND_FILL, KIND_DM)
KIND_LABELS = {KIND_MANAGE: "管理群", KIND_FILL: "汇报群", KIND_DM: "私聊"}

#: 通讯录的两段。管理员在前——找人时先问的是「这事该谁定」，不是「谁在」。
DIRECTORY_ADMINS = "admins"
DIRECTORY_PEERS = "peers"
DIRECTORY_ORDER = (DIRECTORY_ADMINS, DIRECTORY_PEERS)
DIRECTORY_LABELS = {DIRECTORY_ADMINS: "部门管理员", DIRECTORY_PEERS: "同事"}
# 一次发多少人：一屏左栏大约装得下 7 行，20 是「往下滚两屏才要第二次」的量。
# 公司只有几十人时这一屏就是全部，没必要为了少发 10 行再去拉一次。
DIRECTORY_CHUNK = 20


class CollabError(Exception):
    """一次协作室操作被拒绝。message 直接给界面看。"""


# --------------------------------------------------------------------------- #
# 谁看得见什么
# --------------------------------------------------------------------------- #

def visible_objects(conn, member_id: Any) -> list[dict[str, Any]]:
    """这个人在协作室里能看见的全部对象，按「管理群 / 汇报群 / 私聊」分组。

    返回的是**对象**不是线程：`thread_id` 有值说明已经有人说过话，
    为 None 说明点进去才会建。左栏两种都列——管理群只有一两个，让管理员
    先看见「这个群存在」，比让他先发现「没有群」要好。

    私聊不在这里列：私聊没有可枚举的「该有的对象」，只有已经开过的对话。
    """
    rows: list[dict[str, Any]] = []
    admin_of = administered_org_unit_ids(conn, member_id)
    company = has_company_scope(conn, member_id)

    # --- 管理群：一个部门一个，成员是能改这个部门页面的人 ---
    if company or admin_of:
        params: list[Any] = []
        where = "o.status = 'active' AND o.unit_type <> 'company'"
        if not company:
            where += " AND o.org_unit_id = ANY(%s)"
            params.append(list(admin_of))
        for row in conn.execute(
            f"""SELECT o.org_unit_id, o.name AS org_name,
                       t.thread_id, t.status AS thread_status
                  FROM workhub.org_units o
                  LEFT JOIN workhub.collab_threads t
                         ON t.org_unit_id = o.org_unit_id AND t.kind = 'manage'
                 WHERE {where}
                 ORDER BY o.name""", params).fetchall():
            rows.append({
                "kind": KIND_MANAGE,
                "org_unit_id": row["org_unit_id"],
                "interface_id": None,
                "title": f"{row['org_name']} · 管理群",
                "thread_id": row["thread_id"] if row["thread_status"] == "active" else None,
            })

    # --- 汇报群：一个页面一个，成员是这页的填报岗位 + 该部门管理员 ---
    scope_sql, scope_params = _fill_scope_sql(conn, member_id)
    if scope_sql is not None:
        for row in conn.execute(
            f"""SELECT i.interface_id, i.name, o.name AS org_name,
                       t.thread_id, t.status AS thread_status
                  FROM workhub.interfaces i
                  JOIN workhub.org_units o USING (org_unit_id)
                  LEFT JOIN workhub.collab_threads t
                         ON t.interface_id = i.interface_id AND t.kind = 'fill'
                 WHERE i.status = 'active' AND ({scope_sql})
                 ORDER BY o.name, i.name""", scope_params).fetchall():
            rows.append({
                "kind": KIND_FILL,
                "org_unit_id": None,
                "interface_id": row["interface_id"],
                "title": f"{row['name']} · 汇报群",
                "thread_id": row["thread_id"] if row["thread_status"] == "active" else None,
            })

    _attach_last_message(conn, member_id, rows)
    return rows


def _fill_scope_sql(conn, member_id: Any) -> tuple[str | None, list[Any]]:
    """汇报群的可见范围：我的岗位填的页面，或我是这个部门的管理员。

    返回 (SQL 片段, 参数)。片段为 None 表示一个汇报群都看不见。
    """
    parts: list[str] = []
    params: list[Any] = []
    mine = conn.execute(
        """SELECT DISTINCT p.position_id FROM workhub.position_assignments a
             JOIN workhub.positions p USING (position_id)
            WHERE a.member_id = %s AND a.status IN ('active', 'scheduled')
              AND a.starts_at <= now() AND (a.ends_at IS NULL OR now() < a.ends_at)
              AND p.status = 'active'""", (member_id,)).fetchall()
    position_ids = [row["position_id"] for row in mine]
    if position_ids:
        parts.append("i.position_id = ANY(%s)")
        params.append(position_ids)
    admin_of = administered_org_unit_ids(conn, member_id)
    if has_company_scope(conn, member_id):
        parts.append("true")
    elif admin_of:
        parts.append("i.org_unit_id = ANY(%s)")
        params.append(list(admin_of))
    if not parts:
        return None, []
    return " OR ".join(parts), params


def _attach_last_message(conn, member_id: Any, rows: list[dict[str, Any]]) -> None:
    """给每个对象补上「最后一条消息」和「未读数」。

    一条 SQL 拉全部，而不是每个线程查一次：左栏是打开面板就要渲染的东西，
    在模板里循环查库会让面板的打开速度跟群的数量成正比。
    """
    thread_ids = [row["thread_id"] for row in rows if row["thread_id"]]
    by_thread: dict[Any, dict[str, Any]] = {}
    if thread_ids:
        for row in conn.execute(
            """SELECT t.thread_id,
                      m.body AS last_body, m.created_at AS last_at,
                      m.author_kind AS last_author_kind,
                      (SELECT count(*) FROM workhub.collab_messages x
                        WHERE x.thread_id = t.thread_id
                          AND x.seq > coalesce(r.last_seq, 0)
                          AND x.author_id IS DISTINCT FROM %s) AS unread
                 FROM workhub.collab_threads t
                 LEFT JOIN LATERAL (
                     SELECT body, created_at, author_kind FROM workhub.collab_messages
                      WHERE thread_id = t.thread_id ORDER BY seq DESC LIMIT 1
                 ) m ON true
                 LEFT JOIN workhub.collab_reads r
                        ON r.thread_id = t.thread_id AND r.member_id = %s
                WHERE t.thread_id = ANY(%s)""",
            (member_id, member_id, thread_ids)).fetchall():
            by_thread[row["thread_id"]] = row
    for row in rows:
        info = by_thread.get(row["thread_id"], {})
        row["last_body"] = info.get("last_body")
        row["last_at"] = info.get("last_at")
        row["unread"] = info.get("unread") or 0


def visible_dms(conn, member_id: Any) -> list[dict[str, Any]]:
    """已经开过的私聊。私聊没有「该有的对象」，只有说过话的对话。"""
    rows = conn.execute(
        """SELECT t.thread_id, t.created_at,
                  CASE WHEN t.member_a = %s THEN t.member_b ELSE t.member_a END AS peer_id,
                  p.display_name AS peer_name, p.employee_code AS peer_code
             FROM workhub.collab_threads t
             JOIN workhub.workforce_members p
               ON p.member_id = CASE WHEN t.member_a = %s THEN t.member_b ELSE t.member_a END
            WHERE t.kind = 'dm' AND t.status = 'active'
              AND (t.member_a = %s OR t.member_b = %s)
            ORDER BY t.created_at DESC""",
        (member_id, member_id, member_id, member_id)).fetchall()
    out = [{
        "kind": KIND_DM,
        "thread_id": row["thread_id"],
        "peer_id": row["peer_id"],
        "title": row["peer_name"],
        "org_unit_id": None,
        "interface_id": None,
    } for row in rows]
    _attach_last_message(conn, member_id, out)
    return out


# --------------------------------------------------------------------------- #
# 通讯录：跟谁还没说过话，也得能找得到
#
# 左栏的会话列表只列「已经存在的对象 + 已经说过的私聊」，所以一个从没和人
# 私聊过的人，在协作室里没有任何入口去找同事。通讯录补的是这一个洞：它在
# 同一栏里换成「全公司还在职的人」，点谁都跟谁开一条私聊。
#
# 两段而不是一段：找人时先问的是「这事该谁定」，能改别人进度的人要先被看见。
# 分组读的是 grants（ADMIN_RESOURCES），不是岗位名——「负责人」这三个字谁都能
# 写进 title，但能不能动别人的东西只有 grants 说了算。跟 admin_level 同一份
# 清单，所以通讯录里的分组和别的页面上的角色徽章不会互相打架。
# --------------------------------------------------------------------------- #

_DIRECTORY_SQL = """SELECT m.member_id, m.display_name,
          (SELECT p.title FROM workhub.position_assignments a
             JOIN workhub.positions p USING (position_id)
            WHERE a.member_id = m.member_id
              AND a.status IN ('active', 'scheduled')
              AND a.starts_at <= now()
              AND (a.ends_at IS NULL OR now() < a.ends_at)
            ORDER BY a.starts_at DESC LIMIT 1) AS position_title,
          (SELECT o.name FROM workhub.position_assignments a
             JOIN workhub.positions p USING (position_id)
             JOIN workhub.org_units o USING (org_unit_id)
            WHERE a.member_id = m.member_id
              AND a.status IN ('active', 'scheduled')
              AND a.starts_at <= now()
              AND (a.ends_at IS NULL OR now() < a.ends_at)
            ORDER BY a.starts_at DESC LIMIT 1) AS org_name,
          EXISTS (SELECT 1 FROM workhub.permission_grants g
                   WHERE g.revoked_at IS NULL
                     AND g.effect = 'allow'
                     AND g.action = 'manage'
                     AND g.resource = ANY(%s)
                     AND g.valid_from <= now()
                     AND (g.valid_until IS NULL OR now() < g.valid_until)
                     AND ((g.subject_type = 'member' AND g.member_id = m.member_id)
                       OR (g.subject_type = 'position' AND EXISTS (
                            SELECT 1 FROM workhub.position_assignments pa
                             WHERE pa.position_id = g.position_id
                               AND pa.member_id = m.member_id
                               AND pa.status IN ('active', 'scheduled'))))) AS is_admin
     FROM workhub.workforce_members m
    WHERE m.employment_status = 'active'
      AND m.member_id <> %s
    ORDER BY m.display_name"""


def directory(conn, member_id: Any, q: str | None = None,
              offset: int = 0, limit: int | None = None,
              after: str | None = None) -> dict[str, Any]:
    """全公司在职的其他人，按「部门管理员 / 同事」分两段。

    只看在职、只排除自己：通讯录给的是「找人」，不是人员台账，所以不按
    `workforce.members:read` 卡——那条权限管的是改人（工号、岗位、授权），
    这里只露出姓名、部门、岗位这三样本来就挂在组织架构上的东西。

    已经说过的私聊会把未读挂回人身上：从通讯录点进去和从会话列表点进去，
    看到的是同一个数。

    分片：`limit` 给了就只回这一窗，`offset` 是已经发下去的行数。`offset` 走的是
    过滤之后的名册，所以「搜一个词再往下滚」滚的是搜索结果，不是全公司。
    `after` 不用传——下一窗该不该再画分组标题，由这一窗最后一行属于哪组决定，
    返回在 `after` 里。
    """
    people: list[dict[str, Any]] = []
    for row in conn.execute(_DIRECTORY_SQL, (list(ADMIN_RESOURCES), member_id)).fetchall():
        org = row["org_name"] or "还没归到部门"
        title = row["position_title"] or "还没分配岗位"
        people.append({
            "kind": KIND_DM,
            "member_id": row["member_id"],
            "title": row["display_name"],
            "subtitle": f"{org} · {title}",
            "avatar": (row["display_name"] or "人")[:1],
            "is_dm": True,
            "is_admin": bool(row["is_admin"]),
            "thread_id": None,
            "unread": 0,
            # 一律走 open：私聊是「取或建」，说过话的接着上次说，没说过话的这一刻建。
            "href": "/collab/open?kind=dm&peer_id=" + str(row["member_id"]),
        })
    total = {key: 0 for key in DIRECTORY_ORDER}
    for person in people:
        total[DIRECTORY_ADMINS if person["is_admin"] else DIRECTORY_PEERS] += 1

    by_peer = _dm_by_peer(conn, member_id)
    for person in people:
        known = by_peer.get(person["member_id"])
        if known:
            person["thread_id"] = known["thread_id"]
            person["unread"] = known["unread"] or 0
            # 说过的私聊直接指线程，不再绕 open 去「取或建」：左栏补选中态是拿
            # 右栏的 data-thread-id 去 href 里对上号的，href 里没有线程 id 就对不上，
            # 人会以为自己点了个没反应的东西。
            person["href"] = "/collab/threads/" + str(known["thread_id"])

    needle = (q or "").strip().lower()
    if needle:
        people = [p for p in people if needle in p["title"].lower()
                  or needle in p["subtitle"].lower()]

    window = people[offset:] if limit is None else people[offset:offset + limit]
    groups = []
    for key in DIRECTORY_ORDER:
        rows = [p for p in window
                if p["is_admin"] == (key == DIRECTORY_ADMINS)]
        if rows:
            groups.append({"key": key, "label": DIRECTORY_LABELS[key], "rows": rows,
                           "continued": False})
    # 这一窗的第一行接着上一窗的最后一组时，不能再画一次分组标题——滚动加载
    # 一屏就多一个「同事」抬头，看起来像换了一节。
    if groups and after and groups[0]["key"] == after:
        groups[0]["continued"] = True
    loaded = offset + len(window)
    return {"groups": groups, "total_admins": total[DIRECTORY_ADMINS],
            "total_peers": total[DIRECTORY_PEERS], "q": q or "",
            "offset": offset, "has_more": loaded < len(people),
            "next_offset": loaded,
            # 下一窗的 after 由这一窗最后一行定；空窗就沿用上一窗的，别把分组切断。
            "after": groups[-1]["key"] if groups else after}


def _dm_by_peer(conn, member_id: Any) -> dict[Any, dict[str, Any]]:
    """我已经说过的私聊，按对方 member_id 索引。一条 SQL，不按人查。"""
    rows = conn.execute(
        """SELECT t.thread_id,
                  CASE WHEN t.member_a = %s THEN t.member_b ELSE t.member_a END AS peer_id,
                  (SELECT count(*) FROM workhub.collab_messages x
                    WHERE x.thread_id = t.thread_id
                      AND x.seq > coalesce(r.last_seq, 0)
                      AND x.author_id IS DISTINCT FROM %s) AS unread
             FROM workhub.collab_threads t
             LEFT JOIN workhub.collab_reads r
                    ON r.thread_id = t.thread_id AND r.member_id = %s
            WHERE t.kind = 'dm' AND t.status = 'active'
              AND (t.member_a = %s OR t.member_b = %s)""",
        (member_id, member_id, member_id, member_id, member_id)).fetchall()
    return {row["peer_id"]: row for row in rows}


def can_see(conn, member_id: Any, thread: dict[str, Any]) -> bool:
    """能不能看这个会话。**这是唯一的判断点**。

    独立成函数而不是写进各条 SQL，是因为它同时被三处用：页面、将来的 MCP 工具、
    以及写消息前的检查。三处各写一遍，早晚有一处写宽松了。
    """
    kind = thread["kind"]
    if kind == KIND_DM:
        return member_id in (thread["member_a"], thread["member_b"])
    if kind == KIND_MANAGE:
        return (has_company_scope(conn, member_id)
                or thread["org_unit_id"] in administered_org_unit_ids(conn, member_id))
    if kind == KIND_FILL:
        interface_id = thread["interface_id"]
        if interface_id is None:
            return False
        row = conn.execute(
            """SELECT i.org_unit_id, i.position_id FROM workhub.interfaces i
                WHERE i.interface_id = %s AND i.status = 'active'""",
            (interface_id,)).fetchone()
        if row is None:
            return False
        if has_company_scope(conn, member_id):
            return True
        if row["org_unit_id"] in administered_org_unit_ids(conn, member_id):
            return True
        if row["position_id"] is None:
            return False
        held = conn.execute(
            """SELECT 1 FROM workhub.position_assignments a
                WHERE a.member_id = %s AND a.position_id = %s
                  AND a.status IN ('active', 'scheduled')
                  AND a.starts_at <= now() AND (a.ends_at IS NULL OR now() < a.ends_at)""",
            (member_id, row["position_id"])).fetchone()
        return held is not None
    return False


# --------------------------------------------------------------------------- #
# 建会话：点进去那一刻才建
# --------------------------------------------------------------------------- #

def ensure_thread(conn, member_id: Any, *, kind: str,
                  org_unit_id: Any = None, interface_id: Any = None,
                  peer_id: Any = None) -> dict[str, Any]:
    """取或建一个会话，返回整行。建之前先确认这个人有权进这个会话。

    「取或建」而不是「先查再建」：两个管理员同时点开同一个群时，INSERT 撞上
    部分唯一索引是正常结果，不是错误。
    """
    if kind == KIND_MANAGE:
        if org_unit_id is None:
            raise CollabError("管理群需要指定部门。")
        if not (has_company_scope(conn, member_id)
                or org_unit_id in administered_org_unit_ids(conn, member_id)):
            raise CollabError("只有这个部门的管理员能进这个管理群。")
        conn.execute(
            """INSERT INTO workhub.collab_threads (kind, org_unit_id, title)
               VALUES ('manage', %s, %s)
               ON CONFLICT (org_unit_id) WHERE kind = 'manage' DO NOTHING""",
            (org_unit_id, _manage_title(conn, org_unit_id)))
        row = _fetch_thread(conn, kind=kind, org_unit_id=org_unit_id)
    elif kind == KIND_FILL:
        if interface_id is None:
            raise CollabError("汇报群需要指定页面。")
        conn.execute(
            """INSERT INTO workhub.collab_threads (kind, interface_id, title)
               VALUES ('fill', %s, %s)
               ON CONFLICT (interface_id) WHERE kind = 'fill' DO NOTHING""",
            (interface_id, _fill_title(conn, interface_id)))
        row = _fetch_thread(conn, kind=kind, interface_id=interface_id)
    elif kind == KIND_DM:
        if peer_id is None:
            raise CollabError("私聊需要指定对方。")
        if peer_id == member_id:
            raise CollabError("不能和自己私聊。")
        low, high = sorted([member_id, peer_id], key=str)
        conn.execute(
            """INSERT INTO workhub.collab_threads (kind, member_a, member_b)
               VALUES ('dm', %s, %s)
               ON CONFLICT (member_a, member_b) WHERE kind = 'dm' DO NOTHING""",
            (low, high))
        row = _fetch_thread(conn, kind=kind, member_a=low, member_b=high)
    else:
        raise CollabError(f"未知的会话类型：{kind}")
    if row is None:
        raise CollabError("会话没能建起来，请刷新再试。")
    if not can_see(conn, member_id, row):
        raise CollabError("这个会话不开放给你。")
    return row


def _manage_title(conn, org_unit_id: Any) -> str:
    name = conn.execute("SELECT name FROM workhub.org_units WHERE org_unit_id = %s",
                        (org_unit_id,)).fetchone()
    return f"{name['name'] if name else '部门'} · 管理群"


def _fill_title(conn, interface_id: Any) -> str:
    row = conn.execute("SELECT name FROM workhub.interfaces WHERE interface_id = %s",
                       (interface_id,)).fetchone()
    return f"{row['name'] if row else '页面'} · 汇报群"


def _fetch_thread(conn, **keys: Any) -> dict[str, Any] | None:
    where = " AND ".join(f"{column} = %s" for column in keys)
    return conn.execute(
        f"""SELECT thread_id, kind, org_unit_id, interface_id, member_a, member_b,
                   status, title, created_at
              FROM workhub.collab_threads
             WHERE {where} AND status = 'active'""", tuple(keys.values())).fetchone()


def get_thread(conn, thread_id: Any) -> dict[str, Any] | None:
    return conn.execute(
        """SELECT thread_id, kind, org_unit_id, interface_id, member_a, member_b,
                  status, title, created_at
             FROM workhub.collab_threads WHERE thread_id = %s""", (thread_id,)).fetchone()


def thread_header(conn, member_id: Any, thread: dict[str, Any]) -> dict[str, Any]:
    """群头顶上那几行字：群名（不带类型后缀）+ 类型徽章 + 副标题 + 成员数。

    群名里不再带「· 管理群 / · 汇报群」：类型由旁边那枚徽章说，标题里再说一遍
    就是同一行写两次。左栏的会话行不挂徽章，所以那边的标题仍带后缀。

    私聊没有成员数（就两个人），群有。这个差别在界面上是「有没有那枚 4 人字样」，
    所以在这里就算好，不让模板自己判断。
    """
    header = {"kind": thread["kind"], "kind_label": KIND_LABELS[thread["kind"]],
              "title": thread["title"], "subtitle": "", "member_count": None}
    if thread["kind"] == KIND_MANAGE:
        row = conn.execute("SELECT name FROM workhub.org_units WHERE org_unit_id = %s",
                           (thread["org_unit_id"],)).fetchone()
        header["title"] = row["name"] if row else "管理群"
    elif thread["kind"] == KIND_FILL:
        row = conn.execute(
            """SELECT i.name, o.name AS org_name FROM workhub.interfaces i
                JOIN workhub.org_units o USING (org_unit_id)
               WHERE i.interface_id = %s""", (thread["interface_id"],)).fetchone()
        if row:
            header["title"] = row["name"]
            header["subtitle"] = row["org_name"]
        else:
            header["title"] = "汇报群"
    else:
        peer_id = thread["member_b"] if thread["member_a"] == member_id else thread["member_a"]
        row = conn.execute(
            "SELECT display_name, employee_code FROM workhub.workforce_members WHERE member_id = %s",
            (peer_id,)).fetchone()
        header["title"] = row["display_name"] if row else "私聊"
        header["peer_id"] = peer_id
    if thread["kind"] != KIND_DM:
        header["member_count"] = _member_count(conn, thread)
    # 副标题和人数合成一行：两个都没有时，模板里那一行整个不出现。
    header["meta"] = " · ".join(part for part in (
        header["subtitle"],
        f"{header['member_count']} 人" if header["member_count"] else "",
    ) if part)
    return header


def _member_count(conn, thread: dict[str, Any]) -> int:
    """群里有几个人。管理群 = 部门管理员；汇报群 = 填报岗位的人 + 部门管理员。

    只是个数字，用来给群头一枚「N 人」——不参与任何可见性判断。
    """
    if thread["kind"] == KIND_MANAGE:
        return conn.execute(
            """SELECT count(DISTINCT a.member_id) AS n
                 FROM workhub.position_assignments a
                 JOIN workhub.positions p USING (position_id)
                WHERE p.org_unit_id = %s AND a.status IN ('active', 'scheduled')
                  AND a.starts_at <= now() AND (a.ends_at IS NULL OR now() < a.ends_at)""",
            (thread["org_unit_id"],)).fetchone()["n"]
    row = conn.execute(
        """SELECT org_unit_id, position_id FROM workhub.interfaces WHERE interface_id = %s""",
        (thread["interface_id"],)).fetchone()
    if row is None:
        return 0
    # 汇报群的人数 = 这页的填报岗位的人 + 该部门的管理员。部门没有任何岗位绑定时
    # 退回「整个部门」——宁可能看见的人数偏多，也不要显示 0 人。
    return conn.execute(
        """SELECT count(DISTINCT a.member_id) AS n
             FROM workhub.position_assignments a
             JOIN workhub.positions p USING (position_id)
            WHERE p.org_unit_id = %s AND a.status IN ('active', 'scheduled')
              AND a.starts_at <= now() AND (a.ends_at IS NULL OR now() < a.ends_at)
              AND (%s::uuid IS NULL OR p.position_id = %s)""",
        (row["org_unit_id"], row["position_id"], row["position_id"])).fetchone()["n"]


# --------------------------------------------------------------------------- #
# 读消息
# --------------------------------------------------------------------------- #

def list_messages(conn, thread_id: Any, *, before_seq: int | None = None,
                  limit: int = 30) -> list[dict[str, Any]]:
    """最近一批消息，按 seq 升序返回（贴输入框的是最新的一条）。

    `before_seq` 从第一天就收：界面上「加载更早」已经画好，只是这一版不接。
    现在留着一个参数是零成本的，以后补分页时不用改调用方。
    """
    params: list[Any] = [thread_id]
    clause = ""
    if before_seq is not None:
        clause = " AND m.seq < %s"
        params.append(before_seq)
    params.append(limit)
    rows = conn.execute(
        f"""SELECT m.message_id, m.seq, m.body, m.created_at,
                   m.author_id, m.author_kind, m.agent_model, m.mentions,
                   w.display_name AS author_name
              FROM workhub.collab_messages m
              LEFT JOIN workhub.workforce_members w ON w.member_id = m.author_id
             WHERE m.thread_id = %s{clause}
             ORDER BY m.seq DESC LIMIT %s""", params).fetchall()
    return list(reversed(rows))


def post_message(conn, thread: dict[str, Any], member_id: Any, body: str, *,
                 client_msg_id: str | None = None,
                 author_kind: str = "human",
                 agent_model: str | None = None,
                 mentions: list[Any] | None = None) -> dict[str, Any]:
    """写一条消息。返回落库的那条。

    `client_msg_id` 是断网重发的幂等键：同一个 id 只落一条，第二次进来直接
    返回已有的那条，不报错。界面上「发送失败」的红色感叹号靠的就是它——
    重发不是「再发一条」，是「把这一条发成功」。
    """
    body = (body or "").strip()
    if not body:
        raise CollabError("消息不能是空的。")
    if len(body) > MAX_BODY:
        raise CollabError(f"消息太长了，最多 {MAX_BODY} 个字。")
    if author_kind not in ("human", "agent"):
        raise CollabError(f"未知的发言身份：{author_kind}")
    if author_kind == "agent" and not agent_model:
        raise CollabError("AI 代发必须写明是哪个模型。")

    if client_msg_id:
        existing = conn.execute(
            """SELECT message_id, seq, body, created_at FROM workhub.collab_messages
                WHERE thread_id = %s AND client_msg_id = %s""",
            (thread["thread_id"], client_msg_id)).fetchone()
        if existing is not None:
            return dict(existing)

    # 锁住线程行再取 seq：两个并发写入拿不到同一个号，也不需要序列。
    conn.execute("SELECT 1 FROM workhub.collab_threads WHERE thread_id = %s FOR UPDATE",
                 (thread["thread_id"],))
    seq = conn.execute(
        "SELECT coalesce(max(seq), 0) + 1 AS next FROM workhub.collab_messages WHERE thread_id = %s",
        (thread["thread_id"],)).fetchone()["next"]
    row = conn.execute(
        """INSERT INTO workhub.collab_messages
               (thread_id, seq, author_id, author_kind, agent_model, body, mentions, client_msg_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING message_id, seq, body, created_at""",
        (thread["thread_id"], seq, member_id, author_kind, agent_model, body,
         [str(m) for m in (mentions or [])], client_msg_id)).fetchone()
    out = dict(row)
    out["author_id"] = member_id
    out["author_kind"] = author_kind
    out["agent_model"] = agent_model
    out["author_name"] = conn.execute(
        "SELECT display_name FROM workhub.workforce_members WHERE member_id = %s",
        (member_id,)).fetchone()["display_name"]
    return out


# --------------------------------------------------------------------------- #
# 未读
# --------------------------------------------------------------------------- #

def mark_read(conn, thread_id: Any, member_id: Any) -> int:
    """把这个会话读到最新一条，返回写的 last_seq。

    只往前走，不往回退：两个标签页同时开着，慢的那个不能把快的那页读过的
    重新标成未读。
    """
    latest = conn.execute(
        "SELECT coalesce(max(seq), 0) AS n FROM workhub.collab_messages WHERE thread_id = %s",
        (thread_id,)).fetchone()["n"]
    row = conn.execute(
        """INSERT INTO workhub.collab_reads (thread_id, member_id, last_seq, read_at)
           VALUES (%s, %s, %s, now())
           ON CONFLICT (thread_id, member_id) DO UPDATE
              SET last_seq = GREATEST(workhub.collab_reads.last_seq, EXCLUDED.last_seq),
                  read_at = now()
           RETURNING last_seq""",
        (thread_id, member_id, latest)).fetchone()
    return row["last_seq"]


def unread_total(conn, member_id: Any) -> int:
    """入口按钮上那个数。

    只数这个人看得见的会话里的未读。看不见的群不该出现在数字里——否则徽章
    会显示一个点开也找不到的数字。
    """
    rows = conn.execute(
        """SELECT t.thread_id, t.kind, t.org_unit_id, t.interface_id, t.member_a, t.member_b,
                  (SELECT count(*) FROM workhub.collab_messages x
                    WHERE x.thread_id = t.thread_id
                      AND x.seq > coalesce(r.last_seq, 0)
                      AND x.author_id IS DISTINCT FROM %s) AS unread
             FROM workhub.collab_threads t
             LEFT JOIN workhub.collab_reads r
                    ON r.thread_id = t.thread_id AND r.member_id = %s
            WHERE t.status = 'active'""", (member_id, member_id)).fetchall()
    total = 0
    for row in rows:
        if row["unread"] and can_see(conn, member_id, row):
            total += row["unread"]
    return total


def last_seen_seq(conn, thread_id: Any, member_id: Any) -> int:
    """上一次读到第几条。界面用它给「提及竖线」和日期分隔定位。

    没有已读记录时返回 0：一条都没读过，所以整个会话都算「新的」。
    """
    row = conn.execute(
        "SELECT last_seq FROM workhub.collab_reads WHERE thread_id = %s AND member_id = %s",
        (thread_id, member_id)).fetchone()
    return row["last_seq"] if row else 0
