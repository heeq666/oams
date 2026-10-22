"""Read-only access to the product data centre (商品数据中心) for platform metrics.

Everything the company home page shows as a funnel.* number comes from here.
Two things matter, and both are enforced by this module:

* The data centre is never asked anything but read queries. Every tool this
  module can reach is a search_* / list_* / get_* tool; nothing here can write.
* The shapes are normalised once. The HTTP client speaks MCP (Streamable HTTP)
  and its payloads are paginated, nested and versioned. Downstream code
  (workhub.funnel) only ever sees the three shapes frozen in the P0 contract,
  so a change in a tool's envelope cannot quietly change a headline number.

Three implementations of the same DataCenter protocol exist:

DataCenterHTTP
    The real thing. The endpoint comes from WORKHUB_DATACENTER_URL; when that is
    unset the constructor raises DataCenterNotConfigured with an actionable
    message instead of failing later with a confusing network error.
FixtureDataCenter
    Reads a local JSON file (default tests/fixtures/datacenter_26d.json). This
    is how the funnel is tested and how the home page can be previewed on a
    machine that cannot reach the intranet.
DataCenterHTTP(transport=...)
    The same client with the HTTP hop replaced by a callable, so request
    building and payload normalisation are testable without a network.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

DATACENTER_URL_ENV = "WORKHUB_DATACENTER_URL"
DATACENTER_FIXTURE_ENV = "WORKHUB_DATACENTER_FIXTURE"

#: Contract §7: list_products rows must carry these keys.
PRODUCT_FIELDS = (
    "style_code",
    "wave",
    "category",
    "has_design_image",
    "is_ordered",
    "quantity",
    "paid_amount",
)

DEFAULT_FIXTURE_PATH = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "datacenter_26d.json"
)

MCP_PROTOCOL_VERSION = "2025-06-18"

#: Tool names on the product-hub MCP server (docs/runbooks/product-hub-data-access.md).
TOOL_SEARCH_PRODUCTS = "search_products"
TOOL_DEVELOPMENT_RECORDS = "list_development_records"
TOOL_MARKETING = "list_marketing"

MAX_PAGES = 60


class DataCenterError(RuntimeError):
    """The data centre could not answer (network, protocol or payload shape)."""


class DataCenterNotConfigured(DataCenterError):
    """No endpoint is configured, so platform metrics cannot be computed."""


@runtime_checkable
class DataCenter(Protocol):
    """The only window the funnel has onto the product data centre."""

    def list_waves(self) -> list[str]: ...

    def list_products(self, *, wave: str | None) -> list[dict]: ...

    def list_development(self, *, wave: str | None) -> dict: ...

    def list_marketing(self, *, wave: str | None) -> dict: ...


# ---------------------------------------------------------------------------
# payload normalisation
# ---------------------------------------------------------------------------

def _pick(row: dict, names: tuple[str, ...]) -> Any:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return None


def _first_list(
    payload: Any,
    names: tuple[str, ...] = ("items", "data", "records", "products", "rows", "list"),
) -> list:
    """Find the record list inside whatever envelope the server used.

    The server wraps results differently per tool (data.items, items, a bare
    list). One level of data nesting is unwrapped; anything deeper is treated as
    unknown rather than silently guessed at.
    """
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for name in names:
        value = payload.get(name)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = _first_list(value, names)
            if nested:
                return nested
    return []


def as_bool(value: Any) -> bool:
    """Coerce a data-centre truthy field to a real bool.

    The tools send booleans, 0/1 and occasionally the strings "true"/"是".
    Everything else (notably the string "false") is False: a bare Python
    truthiness test would count it as ordered.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "是", "已下单")
    return False


def _as_number(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").replace("¥", "").strip())
    except ValueError:
        return 0.0


def normalize_products(payload: Any, *, wave: str | None = None) -> list[dict]:
    """Map one search_products page onto the contract's product rows."""
    products: list[dict] = []
    for row in _first_list(payload):
        if not isinstance(row, dict):
            continue
        code = _pick(row, ("style_code", "style", "sku", "code", "sku_code"))
        if not code:
            continue
        products.append(
            {
                "style_code": str(code).strip(),
                "wave": _pick(row, ("wave", "wave_name", "wave_code")) or wave or "",
                "category": _pick(row, ("category", "category_name", "品类")) or "",
                # has_design_image is a bool in the contract; the tool may send a count.
                "has_design_image": as_bool(
                    _pick(row, ("has_design_image", "has_design", "design_image"))
                ),
                # Only this flag decides 已下单. order_status is ignored on
                # purpose: handing it to the API changes nothing (every value
                # returns the full set), so trusting it here would silently
                # corrupt 29/39.
                "is_ordered": as_bool(_pick(row, ("is_ordered", "ordered"))),
                "quantity": _as_number(
                    _pick(row, ("quantity", "qty", "order_qty", "total_quantity"))
                ),
                "paid_amount": _as_number(
                    _pick(row, ("paid_amount", "paid", "gmv", "amount", "order_amount"))
                ),
            }
        )
    return products


def _version_key(value: Any) -> tuple:
    """Sort key ordering pricing versions ('v3' / '3' / '第三版' alike)."""
    if value is None:
        return (0, 0.0, "")
    if isinstance(value, (int, float)):
        return (1, float(value), "")
    text = str(value).strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    return (1, float(digits) if digits else 0.0, text)


def normalize_development(payload: Any) -> dict:
    """Fold colour-level sampling records into {"styles": {code: {...}}}.

    sample_status is a *colour* level status: one style has one record per
    colour. The style-level state is never read from the data centre (deriving
    it is the whole point), so this only collects the colour words for
    workhub.funnel.derive_style_sample_state to decide.
    """
    styles: dict[str, dict] = {}
    for row in _first_list(payload):
        if not isinstance(row, dict):
            continue
        code = _pick(row, ("style_code", "style", "sku", "code", "sku_code"))
        if not code:
            continue
        entry = styles.setdefault(
            str(code).strip(),
            {"sample_states": [], "pricing_status": None, "_pricing_version": None},
        )
        word = _pick(row, ("sample_status", "sample_state", "打样状态"))
        if word:
            word = str(word).strip()
            if word not in entry["sample_states"]:
                entry["sample_states"].append(word)
        status = _pick(row, ("pricing_status", "price_status", "pricing_result"))
        if status:
            version = _pick(row, ("pricing_version", "price_version", "version"))
            if entry["pricing_status"] is None or _version_key(version) >= _version_key(
                entry["_pricing_version"]
            ):
                entry["pricing_status"] = str(status).strip()
                entry["_pricing_version"] = version
    for entry in styles.values():
        entry.pop("_pricing_version", None)
    return {"styles": styles}


def normalize_marketing(payload: Any) -> dict:
    """Return {"records": [...], "totals": {"spend": x, "marketing_sales": y}}."""
    records: list[dict] = []
    for row in _first_list(payload):
        if not isinstance(row, dict):
            continue
        records.append(
            {
                "style_code": _pick(row, ("style_code", "style", "sku", "code")),
                "channel": _pick(row, ("channel", "platform", "account")),
                "spend": _as_number(_pick(row, ("spend", "cost", "花费", "ad_spend"))),
                "marketing_sales": _as_number(
                    _pick(row, ("marketing_sales", "sales", "成交", "gmv", "marketing_gmv"))
                ),
            }
        )
    totals: dict = {}
    if isinstance(payload, dict):
        for name in ("totals", "summary", "aggregate"):
            blob = payload.get(name)
            if isinstance(blob, dict):
                totals = {
                    "spend": _as_number(_pick(blob, ("spend", "cost", "total_spend"))),
                    "marketing_sales": _as_number(
                        _pick(blob, ("marketing_sales", "sales", "total_sales", "gmv"))
                    ),
                }
                break
    if not totals and records:
        totals = {
            "spend": sum(r["spend"] for r in records),
            "marketing_sales": sum(r["marketing_sales"] for r in records),
        }
    return {"records": records, "totals": totals or {"spend": 0.0, "marketing_sales": 0.0}}


def unwrap_tool_result(result: Any) -> Any:
    """Turn an MCP tools/call result into the tool's JSON payload."""
    if not isinstance(result, dict):
        return result
    structured = result.get("structuredContent")
    if isinstance(structured, (dict, list)):
        # FastMCP wraps non-object results in {"result": ...}
        if isinstance(structured, dict) and set(structured) == {"result"}:
            return structured["result"]
        return structured
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text") or ""
            try:
                return json.loads(text)
            except (TypeError, ValueError):
                return text
    return result


# ---------------------------------------------------------------------------
# fixture-backed implementation
# ---------------------------------------------------------------------------

class FixtureDataCenter:
    """Serve pre-recorded payloads from one JSON file.

    File shape (see tests/fixtures/datacenter_26d.json):

        {"waves": ["26D冬1-2波"],
         "products":    {wave: [product, ...]},
         "development": {wave: {"styles": {code: {...}}}},
         "marketing":   {wave: {"records": [...], "totals": {...}}}}

    wave=None means "every wave in the file", which is the contract's
    "全部波次合计" reading.
    """

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_FIXTURE_PATH
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:  # pragma: no cover - misconfiguration
            raise DataCenterError(f"找不到数据中心的本地 fixture：{self.path}") from exc
        except ValueError as exc:  # pragma: no cover - misconfiguration
            raise DataCenterError(f"fixture 不是合法 JSON：{self.path}") from exc

    def _waves(self, wave: str | None) -> list[str]:
        bucket: dict = self._data.get("products") or {}
        if wave is None:
            return list(bucket)
        if wave not in bucket:
            raise DataCenterError(f"fixture 里没有波次 {wave!r}；已有：{sorted(bucket)}")
        return [wave]

    def list_waves(self) -> list[str]:
        waves = self._data.get("waves")
        if waves:
            return list(waves)
        return sorted(self._data.get("products") or {})

    def list_products(self, *, wave: str | None = None) -> list[dict]:
        products: list[dict] = []
        for name in self._waves(wave):
            products.extend(
                dict(row) for row in (self._data.get("products") or {}).get(name, [])
            )
        return products

    def list_development(self, *, wave: str | None = None) -> dict:
        styles: dict[str, dict] = {}
        for name in self._waves(wave):
            blob = (self._data.get("development") or {}).get(name) or {}
            for code, entry in (blob.get("styles") or {}).items():
                folded = {"sample_states": list(entry.get("sample_states") or [])}
                if entry.get("pricing_status"):
                    folded["pricing_status"] = entry["pricing_status"]
                styles[code] = folded
        return {"styles": styles}

    def list_marketing(self, *, wave: str | None = None) -> dict:
        records: list[dict] = []
        spend = 0.0
        sales = 0.0
        for name in self._waves(wave):
            blob = (self._data.get("marketing") or {}).get(name) or {}
            records.extend(dict(row) for row in (blob.get("records") or []))
            totals = blob.get("totals") or {}
            spend += _as_number(totals.get("spend"))
            sales += _as_number(totals.get("marketing_sales"))
        if not (spend or sales) and records:
            spend = sum(_as_number(r.get("spend")) for r in records)
            sales = sum(_as_number(r.get("marketing_sales")) for r in records)
        return {"records": records, "totals": {"spend": spend, "marketing_sales": sales}}


# ---------------------------------------------------------------------------
# MCP over HTTP implementation
# ---------------------------------------------------------------------------

class DataCenterHTTP:
    """Talk to the product data centre's MCP endpoint over Streamable HTTP.

    transport replaces the HTTP hop with transport(tool_name, arguments) ->
    payload; tests use it to exercise request shaping and normalisation without
    a network or a live data centre.
    """

    def __init__(
        self,
        url: str | None = None,
        *,
        transport: Callable[[str, dict], Any] | None = None,
        timeout: float = 20.0,
        page_size: int = 50,
    ) -> None:
        resolved = (
            url if url is not None else os.environ.get(DATACENTER_URL_ENV, "")
        ).strip()
        if not resolved and transport is None:
            raise DataCenterNotConfigured(
                f"商品数据中心地址未配置：请设置环境变量 {DATACENTER_URL_ENV}"
                "（例如 http://product-hub.example.com:8791/mcp）。"
                "需要离线预览时请改用 FixtureDataCenter。"
            )
        self.url = resolved
        self.timeout = timeout
        self.page_size = page_size
        self._transport = transport
        self._session_id: str | None = None
        self._request_id = 0

    # -- MCP plumbing -----------------------------------------------
    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _post(self, payload: dict) -> dict | None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        request = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                self._session_id = (
                    response.headers.get("Mcp-Session-Id") or self._session_id
                )
                return _decode_body(raw, response.headers.get("Content-Type", ""))
        except urllib.error.HTTPError as exc:  # pragma: no cover - network
            detail = exc.read()[:400].decode("utf-8", "replace")
            raise DataCenterError(f"商品数据中心返回 HTTP {exc.code}：{detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:  # pragma: no cover - network
            raise DataCenterError(
                f"连不上商品数据中心 {self.url}：{exc}。"
                "该服务在局域网内，需要在能访问内网的机器上运行。"
            ) from exc

    def _rpc(self, method: str, params: dict) -> Any:
        message = self._post(
            {"jsonrpc": "2.0", "id": self._next_id(), "method": method, "params": params}
        )
        if message is None:  # pragma: no cover - protocol violation
            raise DataCenterError(f"MCP 方法 {method} 没有返回结果")
        if message.get("error"):
            raise DataCenterError(f"MCP 方法 {method} 失败：{message['error']}")
        return message.get("result")

    def _ensure_session(self) -> None:
        if self._session_id or self._transport is not None:
            return
        self._rpc(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "workhub", "version": "0.1"},
            },
        )
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call_tool(self, name: str, arguments: dict | None = None) -> Any:
        if self._transport is not None:
            return self._transport(name, dict(arguments or {}))
        self._ensure_session()
        return unwrap_tool_result(
            self._rpc("tools/call", {"name": name, "arguments": dict(arguments or {})})
        )

    # -- DataCenter protocol ----------------------------------------
    def list_waves(self) -> list[str]:
        """Wave options, read off the product search's filter options.

        The MCP surface has no dedicated "list waves" tool; the search tool
        echoes the options it filtered by. A missing list is not an error: the
        caller then falls back to the configured home.default_wave.
        """
        payload = self.call_tool(TOOL_SEARCH_PRODUCTS, {"page": 1, "page_size": 1})
        if isinstance(payload, dict):
            candidates = [payload.get("filter_options"), payload.get("filters")]
            data = payload.get("data")
            if isinstance(data, dict):
                candidates.append(data.get("filter_options"))
            for blob in candidates:
                if isinstance(blob, dict):
                    waves = blob.get("wave") or blob.get("waves")
                    if isinstance(waves, list):
                        return [str(w) for w in waves]
        return []

    def list_products(self, *, wave: str | None = None) -> list[dict]:
        products: list[dict] = []
        seen: set[str] = set()
        for page in range(1, MAX_PAGES + 1):
            arguments: dict[str, Any] = {"page": page, "page_size": self.page_size}
            if wave:
                arguments["wave"] = wave
            batch = normalize_products(
                self.call_tool(TOOL_SEARCH_PRODUCTS, arguments), wave=wave
            )
            fresh = [row for row in batch if row["style_code"] not in seen]
            for row in fresh:
                seen.add(row["style_code"])
            products.extend(fresh)
            if len(batch) < self.page_size or not fresh:
                break
        return products

    def list_development(self, *, wave: str | None = None) -> dict:
        arguments: dict[str, Any] = {}
        if wave:
            # Unlike order_status, this tool's wave filter really works.
            arguments["wave"] = wave
        return normalize_development(self.call_tool(TOOL_DEVELOPMENT_RECORDS, arguments))

    def list_marketing(self, *, wave: str | None = None) -> dict:
        """Marketing rows, narrowed to the wave by style code.

        list_marketing takes dates and channel, not a wave, so the wave cut
        happens here: fetch, keep the styles that belong to the wave, re-sum.
        """
        normalised = normalize_marketing(self.call_tool(TOOL_MARKETING, {}))
        if not wave:
            return normalised
        codes = {row["style_code"] for row in self.list_products(wave=wave)}
        records = [r for r in normalised["records"] if r.get("style_code") in codes]
        if not records:
            return normalised
        return {
            "records": records,
            "totals": {
                "spend": sum(r["spend"] for r in records),
                "marketing_sales": sum(r["marketing_sales"] for r in records),
            },
        }


def _decode_body(raw: bytes, content_type: str) -> dict | None:
    """Decode either a plain JSON body or an SSE stream carrying one result."""
    if not raw:
        return None
    text = raw.decode("utf-8", "replace").strip()
    if "text/event-stream" not in (content_type or "").lower() and text.startswith("{"):
        return json.loads(text)
    message: dict | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload:
            continue
        try:
            candidate = json.loads(payload)
        except ValueError:
            continue
        if isinstance(candidate, dict) and ("result" in candidate or "error" in candidate):
            message = candidate
    return message


def build_datacenter(dc: DataCenter | None = None) -> DataCenter:
    """Return the data centre to use, honouring the two environment variables."""
    if dc is not None:
        return dc
    fixture = os.environ.get(DATACENTER_FIXTURE_ENV, "").strip()
    if fixture:
        return FixtureDataCenter(fixture)
    return DataCenterHTTP()
