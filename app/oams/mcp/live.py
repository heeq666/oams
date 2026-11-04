"""Three MCP tools that act on a running OAMS over HTTP.

Why these three live apart from `tools.py`: the tools over there are a contract
sandbox (an in-memory store, no database). These three must do *real* work --
create a page in a department and send it for review -- and they do it the same
way an external AI would: with a ticket, over `/v1`.

    MCP client  ->  these handlers  ->  HTTP (/v1, Authorization: Bearer <票>)  ->  OAMS

The rule that follows from it: **no handler in this file opens a database
connection**. What the tools can do is exactly what the ticket can do, so a
person revoking the ticket in 「我的授权」 takes the tools' power away in the same
second. A tool that reached into PostgreSQL itself would survive that revocation,
and the audit trail would name nobody.

Configuration (no silent fallbacks -- if it is missing, the tool says so):

    OAMS_BASE_URL    站点前缀，如 http://127.0.0.1:8000
    OAMS_TOKEN_FILE  票文件，默认 ~/.config/oams/token.json（与外部 AI 用同一份）

The ticket file is the one `skills/oams-connect/SKILL.md` describes: a human
approves an app code once, the app exchanges it for a token, and the token is
written there. When it is missing the tool refuses instead of quietly becoming
the sandbox -- an AI that thinks it published a page when it only filled a
memory dict is worse than one that got an error.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from oams.mcp.tools import (
    ERR_DESCRIPTOR_INVALID,
    ERR_INVALID_ARGUMENTS,
    ToolResult,
    ToolSpec,
    _fail,
    _ok,
    _schema,
)

#: 票文件：与外部 AI 同一份。取票流程见 skills/oams-connect/SKILL.md。
DEFAULT_TOKEN_FILE = "~/.config/oams/token.json"
#: 单次 HTTP 调用的等待时间。站点在本机，20 秒足够，也不会把 MCP 调用挂死。
DEFAULT_TIMEOUT = 20.0

#: 配置不全（没有站点地址、没有票、票到期）——调用方该去补什么，写在校验里。
ERR_LIVE_CONFIG = "LIVE_CONFIG_ERROR"
#: 站点拒绝了这次调用（HTTP 4xx/5xx），detail 原样带回去。
ERR_LIVE_API = "LIVE_API_ERROR"


class LiveConfigError(RuntimeError):
    """Configuration is missing or stale; the message says what to fix."""


class LiveApiError(RuntimeError):
    """The site refused the call. Carries the status code and the site's own words."""

    def __init__(self, status: int, message: str, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.payload = payload or {}


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LiveConfig:
    base_url: str
    token: str
    token_file: Path
    app_name: str | None = None
    member: str | None = None
    scopes: tuple[str, ...] = ()
    expires_at: str | None = None


def _token_path(explicit: str | Path | None = None) -> Path:
    raw = str(explicit or os.environ.get("OAMS_TOKEN_FILE") or DEFAULT_TOKEN_FILE)
    return Path(os.path.expanduser(raw))


def _parse_expiry(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def load_config(token_file: str | Path | None = None) -> LiveConfig:
    """Read the ticket and the site prefix, or raise with the exact thing to fix."""
    base_url = (os.environ.get("OAMS_BASE_URL") or "").strip().rstrip("/")
    if not base_url:
        raise LiveConfigError(
            "没配 OAMS_BASE_URL：工具不知道站点在哪。给它地址再试，例如 "
            "OAMS_BASE_URL=http://127.0.0.1:8000。")
    path = _token_path(token_file)
    if not path.is_file():
        raise LiveConfigError(
            f"还没拿到票：票文件 {path} 不存在。先按 skills/oams-connect/SKILL.md 取一张"
            "（人点一次「允许」），票会写到这个文件；也可以用 OAMS_TOKEN_FILE 指到别处。")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise LiveConfigError(f"票文件 {path} 读不出来（{error}）：重新取一次票。") from error
    if not isinstance(raw, dict) or not str(raw.get("token") or "").strip():
        raise LiveConfigError(f"票文件 {path} 里没有 token 字段：重新取一次票。")
    expires_at = raw.get("expires_at")
    moment = _parse_expiry(expires_at)
    if moment is not None and moment <= datetime.now(timezone.utc):
        raise LiveConfigError(
            f"这张票已到期（{expires_at}）：按 skills/oams-connect/SKILL.md 重新取一张，"
            "再调一次这个工具。")
    scopes = raw.get("scopes") or []
    return LiveConfig(
        base_url=base_url,
        token=str(raw["token"]).strip(),
        token_file=path,
        app_name=raw.get("app_name"),
        member=raw.get("member"),
        scopes=tuple(str(scope) for scope in scopes),
        expires_at=str(expires_at) if expires_at else None,
    )


# --------------------------------------------------------------------------- #
# HTTP client (stdlib only: an MCP server should not need another dependency)
# --------------------------------------------------------------------------- #
def _detail_of(payload: Any, raw: str, status: int) -> str:
    if isinstance(payload, dict):
        for key in ("error", "detail", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, list):
                joined = "；".join(str(item) for item in value)
                if joined.strip():
                    return joined
    return raw.strip() or f"HTTP {status}"


class OamsLive:
    """The ticket, applied to the site's own `/v1` endpoints."""

    def __init__(self, config: LiveConfig, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.config = config
        self.timeout = timeout

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        url = self.config.base_url + path
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.config.token}")
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw) if raw.strip() else None
            except ValueError:
                payload = None
            raise LiveApiError(error.code, _detail_of(payload, raw, error.code),
                               payload if isinstance(payload, dict) else None) from error
        except urllib.error.URLError as error:
            raise LiveConfigError(
                f"连不上 OAMS（{self.config.base_url}）：{getattr(error, 'reason', error)}。"
                "站点没起、端口不对，都会长这样。") from error
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError:
            raise LiveApiError(200, f"站点回的不是 JSON：{raw[:200]}") from None
        return parsed if isinstance(parsed, dict) else {"value": parsed}

    def me(self) -> dict[str, Any]:
        return self.request("GET", "/v1/me")

    def create_interface(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/v1/interfaces", body)

    def get_interface(self, interface_id: str) -> dict[str, Any]:
        return self.request("GET", f"/v1/interfaces/{interface_id}")

    def submit_interface(self, interface_id: str) -> dict[str, Any]:
        return self.request("POST", f"/v1/interfaces/{interface_id}/submit")


def _client() -> OamsLive:
    return OamsLive(load_config())


def _guarded(handler: Callable[[Any, dict[str, Any]], ToolResult]) -> Callable[[Any, dict[str, Any]], ToolResult]:
    """Config and HTTP failures become a tool refusal with the site's own words."""

    def wrapper(context: Any, arguments: dict[str, Any]) -> ToolResult:
        try:
            return handler(context, arguments)
        except LiveConfigError as error:
            return _fail(ERR_LIVE_CONFIG, str(error))
        except LiveApiError as error:
            extra = {"status": error.status}
            extra.update(error.payload or {})
            return _fail(ERR_LIVE_API, f"站点拒绝了这次调用：{error.message}", **extra)

    wrapper.__name__ = getattr(handler, "__name__", "tool")
    wrapper.__doc__ = handler.__doc__
    return wrapper


# --------------------------------------------------------------------------- #
# Descriptor problems, in Chinese
# --------------------------------------------------------------------------- #
#: 校验器的问题码 -> 人话标签。
CODE_LABELS = {
    "DATA_RULE_VIOLATION": "数据规则",
    "FIELD_UNMAPPED": "字段没映射",
    "STATUS_UNMAPPED": "状态没映射",
    "METRIC_UNDEFINED": "指标没定义",
    "MODULE_NOT_WHITELISTED": "模块不在白名单",
    "CAPABILITY_ESCALATION": "权限越界",
    "LAYOUT_TOO_DEEP": "嵌套太深",
    "SCHEMA_VERSION": "文档版本",
}

_PROBLEM_RE = re.compile(r"^\[(?P<code>[A-Z_]+)\]\s*(?P<rest>.*)$", re.S)

#: 常见英文问题句 -> 中文解释。命中不了的保留原文，不吞掉。
_MESSAGE_RULES: tuple[tuple[str, Callable[[str], str]], ...] = (
    ("unknown department", lambda m: f"部门码 {_tail(m, 'unknown department')} 不在你可建的部门里，"
                                     "先用 oams_departments 看有哪些"),
    ("unknown position", lambda m: f"岗位码 {_tail(m, 'unknown position')} 不在这个部门里，"
                                   "先用 oams_departments 看这个部门在用的岗位码"),
    ("must list at least one position", lambda m: "position_codes 至少要写一个岗位码"),
    ("must contain at least one field", lambda m: "fields 至少要写一个字段（title / status / evidence 这些）"),
    ("key is required", lambda m: "这个字段缺 key：每个字段都要有稳定的英文 key"),
    ("duplicate field key", lambda m: f"字段 key {_tail(m, 'duplicate field key')} 写了两遍"),
    ("duplicate metric binding", lambda m: f"指标绑定重复了：{_tail(m, 'duplicate metric binding')}"),
    ("unmapped", lambda m: "有字段没有 map_to：显示用的字段也要指认一个落库位置"),
    ("must map to a standard field", lambda m: "map_to 要指向标准字段，不能自造落库字段"),
    ("must be an object", lambda m: "这里要写成一个 JSON 对象，不是字符串或数组"),
    ("must be a list", lambda m: "这里要写成一个列表"),
    ("status_map", lambda m: "状态映射没写全：部门自己的词要映到 planned / in_progress / blocked / done 四个标准态"),
    ("unknown status", lambda m: f"{_tail(m, 'unknown status')} 不是标准状态：只认 planned、in_progress、blocked、done"),
    ("not in the module whitelist", lambda m: f"模块 {_tail(m, 'module')} 不在白名单里，只能从模块注册表里选"),
    ("unknown module", lambda m: f"模块 {_tail(m, 'unknown module')} 不存在，只能从模块注册表里选"),
    ("department's namespace", lambda m: "指标名要以 dept.<本部门代码>. 开头，别引用别的部门的指标"),
    ("map_to is required", lambda m: "这个字段缺 map_to：它的值要落到哪个标准字段"),
    ("is not a registered module",
     lambda m: f"模块 {_tail(m, 'type')} 不在白名单里；能选哪 21 个用 list_modules 看"),
    ("no field maps to",
     lambda m: f"没有字段映射到 {_tail(m, 'no field maps to')}："
               "页面靠这三个标准字段做标题、状态与凭据，缺了就渲染不出"),
    ("must not be empty", lambda m: "这一项不能留空"),
    ("不认识的键", lambda m: m),
)

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


_QUOTED_RE = re.compile(r"'([^']*)'|\"([^\"]*)\"")


def _tail(message: str, marker: str) -> str:
    """The thing the validator named: it quotes it (`unknown department 'NOPE'`)."""
    index = message.find(marker)
    segment = message if index < 0 else message[index + len(marker):]
    quoted = _QUOTED_RE.search(segment)
    if quoted:
        return quoted.group(1) or quoted.group(2) or "（空）"
    return segment.strip(" :：'\"`,") or "（空）"


def explain_problem(problem: str) -> str:
    """Turn one validator line into a sentence a person can act on."""
    match = _PROBLEM_RE.match(problem.strip())
    code = match.group("code") if match else ""
    rest = match.group("rest").strip() if match else problem.strip()
    where = ""
    if ":" in rest:
        head, tail = rest.split(":", 1)
        if head and " " not in head.strip():
            where, rest = head.strip(), tail.strip()
    for marker, translate in _MESSAGE_RULES:
        if marker in rest:
            sentence = translate(rest)
            break
    else:
        # 校验器有一半的话本来就是中文，那些照抄，别再加一层「原话」。
        sentence = rest if _CJK_RE.search(rest) else f"校验器原话：{rest}"
    label = CODE_LABELS.get(code, code or "问题")
    suffix = f"（位置 {where}）" if where else ""
    return f"{label}：{sentence}{suffix}"


def _problem_list(problems: list[str]) -> str:
    lines = [f"发现 {len(problems)} 条问题，改完再调一次 oams_check_descriptor："]
    for index, problem in enumerate(problems, start=1):
        lines.append(f"{index}. {explain_problem(problem)}")
    return "\n".join(lines)


def _descriptor_argument(arguments: dict[str, Any]) -> dict[str, Any] | None:
    descriptor = arguments.get("descriptor")
    if isinstance(descriptor, str):
        try:
            descriptor = json.loads(descriptor)
        except ValueError:
            return None
    return descriptor if isinstance(descriptor, dict) else None


def _validate_locally(descriptor: dict[str, Any], org_codes: set[str] | None,
                      position_codes: set[str] | None) -> list[str]:
    """Same two passes the contract uses: field/metric semantics, then module tree."""
    from oams.descriptor import validate_descriptor
    from oams.mcp.tools import validate_module_tree_tool

    problems = list(validate_descriptor(descriptor, known_org_unit_codes=org_codes,
                                        known_position_codes=position_codes) or [])
    modules = descriptor.get("modules")
    if isinstance(modules, list):
        problems.extend(str(item) for item in validate_module_tree_tool(modules) or [])
    # 两条校验链会在同一个节点上报同一句话（模块白名单就是这样），去重但保序。
    return list(dict.fromkeys(problems))


def _codes_from(me: dict[str, Any], org_code: str | None) -> tuple[set[str], set[str] | None]:
    units = me.get("writable_org_units") or []
    org_codes = {str(unit.get("org_code")) for unit in units if unit.get("org_code")}
    target = org_code or me.get("org_unit_code")
    scoped = [unit for unit in units if unit.get("org_code") == target] if target else []
    chosen = scoped or units
    if not chosen:
        return org_codes, None
    return org_codes, {str(position.get("position_code"))
                       for unit in chosen
                       for position in (unit.get("positions") or [])
                       if position.get("position_code")}


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
@_guarded
def _departments(context: Any, arguments: dict[str, Any]) -> ToolResult:
    me = _client().me()
    units = me.get("writable_org_units") or []
    app = me.get("app") or {}
    lines = [f"你是 {me.get('display_name')}（成员 {me.get('member_id')}）。"]
    if app:
        labels = app.get("scope_labels") or {}
        scopes = "、".join(labels.get(scope, scope) for scope in (app.get("scopes") or [])) or "（一个都没有）"
        lines.append(f"这张票是「{app.get('app_name')}」换来的，范围：{scopes}；到期 {app.get('valid_until')}。")
    else:
        lines.append("这张票不是接入方换来的，是普通登录会话。")
    if app and "interfaces:draft" not in (app.get("scopes") or []):
        lines.append("注意：这张票没有「新建草稿页面并送审」，oams_submit_page 会被拒。")
    lines.append(me.get("writable_note") or "")
    for unit in units:
        positions = "、".join(str(item.get("position_code")) for item in (unit.get("positions") or []))
        lines.append(f"- {unit.get('org_code')}（{unit.get('name')}）：岗位 {positions or '（没有在用岗位）'}")
    if units:
        lines.append("写法：descriptor 里 org_unit_code 填上面的部门码，position_codes 填该部门列出的岗位码；"
                     "写完先 oams_check_descriptor，再 oams_submit_page。")
    return _ok(summary="\n".join(lines), member_id=me.get("member_id"),
               display_name=me.get("display_name"), writable_all=me.get("writable_all"),
               org_units=units, app=app or None)


@_guarded
def _check_descriptor(context: Any, arguments: dict[str, Any]) -> ToolResult:
    descriptor = _descriptor_argument(arguments)
    if descriptor is None:
        return _fail(ERR_INVALID_ARGUMENTS,
                     "descriptor 要是一个 JSON 对象；传了字符串的话得是合法 JSON。")
    org_code = str(arguments.get("org_unit_code") or descriptor.get("org_unit_code") or "").strip() or None
    unverified = ""
    org_codes: set[str] | None = None
    position_codes: set[str] | None = None
    try:
        me = _client().me()
    except (LiveConfigError, LiveApiError) as error:
        unverified = f"\n没连上 OAMS（{error}），这次没验的两条是：部门码在不在库里、岗位码在不在这个部门。"
    else:
        org_codes, position_codes = _codes_from(me, org_code)
    problems = _validate_locally(descriptor, org_codes, position_codes)
    if problems:
        return _fail(ERR_DESCRIPTOR_INVALID, _problem_list(problems) + unverified,
                     problems=[explain_problem(item) for item in problems], raw=problems)
    return _ok(summary="校验通过：字段映射、状态映射、模块白名单、容器深度都没问题。"
                       + ("组织这一层也验过了。" if org_codes else "")
                       + unverified,
               problems=[], verified_org_codes=bool(org_codes))


@_guarded
def _submit_page(context: Any, arguments: dict[str, Any]) -> ToolResult:
    interface_id = str(arguments.get("interface_id") or "").strip()
    descriptor = _descriptor_argument(arguments)
    slug = str(arguments.get("slug") or "").strip()
    name = str(arguments.get("name") or "").strip()
    org_code = str(arguments.get("org_unit_code") or "").strip() or None
    if interface_id and descriptor is not None:
        return _fail(ERR_INVALID_ARGUMENTS,
                     "给 interface_id 是「只送审已有的草稿」，就别再给 descriptor 了；"
                     "要建新的就只给 slug、name、descriptor。")
    if not interface_id:
        if descriptor is None or not slug or not name:
            return _fail(ERR_INVALID_ARGUMENTS,
                         "建新页要三样：slug、name、descriptor。只想给已有草稿送审，给 interface_id 就行。")
        if isinstance(descriptor, dict):
            org_code = org_code or str(descriptor.get("org_unit_code") or "").strip() or None

    client = _client()
    created: dict[str, Any] | None = None
    if not interface_id:
        # 先在本地过一遍校验：不合格就别在库里留半张页。
        try:
            me = client.me()
        except (LiveConfigError, LiveApiError):
            org_codes = position_codes = None
        else:
            org_codes, position_codes = _codes_from(me, org_code)
        problems = _validate_locally(descriptor, org_codes, position_codes)
        if problems:
            return _fail(ERR_DESCRIPTOR_INVALID,
                         "描述符没过校验，没有建任何东西。\n" + _problem_list(problems),
                         problems=[explain_problem(item) for item in problems], raw=problems)
        body: dict[str, Any] = {"slug": slug, "name": name, "descriptor": descriptor}
        if org_code:
            body["org_unit_code"] = org_code
        created = client.create_interface(body)
        interface_id = str(created.get("interface_id") or "")
        if not interface_id:
            return _fail(ERR_LIVE_API, f"站点说建好了，却没给 interface_id：{created}")

    try:
        submitted = client.submit_interface(interface_id)
    except LiveApiError as error:
        hint = (f"草稿已经建好（界面 {interface_id}），送审这一步没过：{error.message}。"
                f"页面还在库里、平台没有删除接口——改掉原因后用 interface_id={interface_id} "
                "再调一次 oams_submit_page（这次只送审，不会重复建页）。"
                if created is not None else
                f"送审没过：{error.message}。这一页没有变动；要重试就再给 interface_id={interface_id}。")
        return _fail(ERR_LIVE_API, hint, status=error.status, interface_id=interface_id,
                     created=created is not None, **{k: v for k, v in (error.payload or {}).items()
                                                     if k not in {"status", "interface_id", "created"}})

    # 站点说成不算成：回读一次，状态是它现在真的样子。
    try:
        verified = client.get_interface(interface_id)
    except (LiveConfigError, LiveApiError) as error:
        verified, verify_note = None, f"回读没成功（{error.message}），去页面右上角「待我审」看一眼。"
    else:
        state = str(verified.get("status") or "")
        verify_note = (f"回读确认：库里 status={state}，在部门的待审列表里。"
                       if state == "pending" else
                       f"回读到的状态是 {state}，不是 pending——别当成已经送出去了。")

    url = str(submitted.get("url") or "")
    summary = (f"已建草稿并送审：{submitted.get('name') or name}（{submitted.get('slug') or slug}），"
               f"界面 {interface_id}。{verify_note}")
    if created is None:
        summary = summary.replace("已建草稿并送审：", "已送审：")
    summary += "审核要人点——AI 不能自己批。"
    return _ok(summary=summary, interface_id=interface_id,
               org_unit_id=submitted.get("org_unit_id"), slug=submitted.get("slug"),
               name=submitted.get("name"), status=submitted.get("status") or "pending",
               verified_status=(verified or {}).get("status"),
               url=(client.config.base_url + url) if url else None,
               created=created is not None)


#: The three tools. `capability=""` is deliberate: these are guarded by the ticket
#: on the site, not by the sandbox capability tokens the other tools use.
LIVE_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="oams_departments",
        description=(
            "看你自己是谁、这张票代表哪个应用、能给哪些部门建页，以及每个部门在用的岗位码。"
            "写页面之前先调它：org_unit_code 与 position_codes 都从这里取，别猜。"),
        input_schema=_schema({}),
        capability="",
        handler=_departments,
        read_only=True,
    ),
    ToolSpec(
        name="oams_check_descriptor",
        description=(
            "本地跑一遍 OAMS 的描述符校验：字段映射、状态映射、模块白名单、容器深度，"
            "部门码与岗位码从 /v1/me 取。返回中文问题清单；没有问题时才算能上架。"),
        input_schema=_schema({
            "descriptor": {"type": "object",
                           "description": "完整的描述符对象（JSON），不是文件路径"},
            "org_unit_code": {"type": "string",
                              "description": "可选：只按这个部门验岗位码；不给就用 descriptor 里那个"},
        }, required=["descriptor"]),
        capability="",
        handler=_check_descriptor,
        read_only=True,
    ),
    ToolSpec(
        name="oams_submit_page",
        description=(
            "一步到位：先过校验，再在指定部门建一张草稿页并送审（部门一审）。"
            "给 slug + name + descriptor 建新页；只给 interface_id 则给已有草稿重试送审。"),
        input_schema=_schema({
            "slug": {"type": "string", "description": "页面地址里的英文短名，例如 it-repair-tickets"},
            "name": {"type": "string", "description": "页面标题，给人看的"},
            "descriptor": {"type": "object", "description": "完整的描述符对象（JSON）"},
            "org_unit_code": {"type": "string",
                              "description": "部门代码，例如 IT；不给则读 descriptor 顶层的 org_unit_code"},
            "interface_id": {"type": "string",
                             "description": "已有草稿的界面编号：只送审，不再建页"},
        }),
        capability="",
        handler=_submit_page,
    ),
)

LIVE_TOOLS_BY_NAME = {spec.name: spec for spec in LIVE_TOOL_SPECS}


def build_live_registry(**kwargs: Any):
    """The registry the stdio server hands out: the 12 sandbox tools + these three.

    Kept as a function so a test can build one with its own store/audit and
    inspect it; the defaults of `build_registry` stay at 12 so the sandbox
    contract tests keep pinning exactly what they pinned before.
    """
    from oams.mcp.tools import build_registry

    return build_registry(extra_specs=LIVE_TOOL_SPECS, **kwargs)
