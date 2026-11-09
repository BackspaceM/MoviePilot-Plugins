"""RouterOS v7 REST 客户端：只覆盖 DNS static 与连通性校验。

不依赖第三方 SDK，HTTP 传输由调用方注入（宿主 AsyncRequestUtils），
响应解析为纯函数，便于离线测试。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

DNS_STATIC_PATH = "/rest/ip/dns/static"
IDENTITY_PATH = "/rest/system/identity"


@dataclass
class DnsStaticRecord:
    """一条 DNS static 记录。"""

    id: str = ""
    name: str = ""
    address: str = ""
    record_type: str = ""
    ttl: str = ""
    comment: str = ""
    disabled: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "address": self.address,
            "type": self.record_type,
            "ttl": self.ttl,
            "comment": self.comment,
        }


def basic_auth_header(username: str, password: str) -> Dict[str, str]:
    """构造 RouterOS 要求的 Basic 认证头。"""
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def normalize_base_url(host: str, port: int = 443, use_ssl: bool = True) -> str:
    """把用户输入的地址规范成 `scheme://host[:port]`（剥掉用户多写的 /rest）。"""
    value = (host or "").strip().rstrip("/")
    if not value:
        return ""
    if "://" in value:
        scheme, rest = value.split("://", 1)
        base = f"{scheme.lower()}://{rest.rstrip('/')}"
    else:
        scheme = "https" if use_ssl else "http"
        default_port = 443 if use_ssl else 80
        base = f"{scheme}://{value}" if not port or int(port) == default_port else f"{scheme}://{value}:{int(port)}"
    if base.lower().endswith("/rest"):
        base = base[: -len("/rest")]
    return base.rstrip("/")


def _pick_id(payload: Any) -> str:
    """RouterOS REST 用 `.id`，个别网关会返回转义写法 `\\.id`，两者都兼容。"""
    if not isinstance(payload, dict):
        return ""
    for key in (".id", "\\.id", "id"):
        value = payload.get(key)
        if value:
            return str(value)
    return ""


def parse_records(payload: Any) -> List[DnsStaticRecord]:
    """解析 /ip/dns/static 的列表响应，跳过不合规条目。"""
    if not isinstance(payload, list):
        return []
    records: List[DnsStaticRecord] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        address = str(item.get("address") or "").strip()
        record_type = str(item.get("type") or "").strip().upper()
        if not name or not address:
            continue
        if record_type and record_type not in ("A", "AAAA"):
            continue
        records.append(
            DnsStaticRecord(
                id=_pick_id(item),
                name=name,
                address=address,
                record_type=record_type,
                ttl=str(item.get("ttl") or ""),
                comment=str(item.get("comment") or ""),
                disabled=str(item.get("disabled") or "").lower() in ("true", "yes", "1"),
            )
        )
    return records


def extract_record_id(payload: Any) -> str:
    """从 PUT 响应里取新建记录的 .id。"""
    if isinstance(payload, dict):
        return _pick_id(payload)
    if isinstance(payload, list) and payload:
        return _pick_id(payload[0])
    return ""


def describe_error(status: int, body: Any) -> str:
    """把 REST 错误翻译成可执行的中文提示。"""
    detail = ""
    if isinstance(body, dict):
        detail = str(body.get("detail") or body.get("message") or body.get("error") or "")
    elif isinstance(body, str):
        detail = body.strip()[:200]
    if status in (401, 403):
        return f"认证失败（HTTP {status}）：检查用户名/密码，并确认该用户拥有 read/write/rest-api 权限"
    if status == 404:
        return "接口不存在（HTTP 404）：RouterOS 需为 v7 且已启用 REST（/ip service 中 www 或 www-ssl）"
    if status == 400:
        return f"参数被拒绝（HTTP 400）：{detail or '检查域名与 TTL 是否符合 RouterOS 语法'}"
    if status >= 500:
        return f"RouterOS 内部错误（HTTP {status}）：{detail or '请查看路由器日志'}"
    return f"请求失败（HTTP {status}）：{detail or '无响应体'}"


class RouterOSClient:
    """最小可用的 RouterOS REST 封装。"""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout: float = 10.0,
        verify: bool = False,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.verify = verify
        self.last_error = ""

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        headers.update(basic_auth_header(self.username, self.password))
        return headers

    async def request(
        self,
        client: Any,
        method: str,
        path: str,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, Any]:
        """发一次 REST 请求，返回 (状态码, 响应体)。"""
        response = await client.request(
            method,
            self._url(path),
            headers=self._headers(),
            json=json_body,
            timeout=self.timeout,
        )
        if response is None:
            self.last_error = "无法连接路由器：检查地址/端口、证书校验与网络可达性"
            return 0, None
        status = int(getattr(response, "status_code", 0) or 0)
        body: Any = None
        try:
            body = response.json()
        except Exception:  # noqa: BLE001 - 空响应或非 JSON 时保留文本
            try:
                body = response.text
            except Exception:  # noqa: BLE001
                body = ""
        if status >= 400:
            self.last_error = describe_error(status, body)
        return status, body

    async def check(self, client: Any) -> Tuple[bool, str]:
        """校验连通性、认证与 REST 可用性。"""
        status, body = await self.request(client, "GET", IDENTITY_PATH)
        if status == 200 and isinstance(body, dict):
            name = body.get("name") or "RouterOS"
            version = body.get("version") or "v7"
            return True, f"连接成功：{name}（{version}）"
        return False, self.last_error or "连接失败"

    async def list_records(self, client: Any, comment: Optional[str] = None) -> List[DnsStaticRecord]:
        """列出 DNS static，可按备注精确过滤。"""
        path = DNS_STATIC_PATH
        if comment:
            path = f"{path}?comment={comment}"
        status, body = await self.request(client, "GET", path)
        if status != 200:
            return []
        return parse_records(body)

    async def list_by_name(self, client: Any, name: str) -> List[DnsStaticRecord]:
        """列出指定域名的全部记录，用于按域名清理。"""
        status, body = await self.request(client, "GET", f"{DNS_STATIC_PATH}?name={name}")
        if status != 200:
            return []
        return [item for item in parse_records(body) if item.name == name]

    async def delete_record(self, client: Any, record_id: str) -> Tuple[bool, str]:
        """按 .id 删除一条记录。"""
        if not record_id:
            return False, "记录 ID 为空"
        path = f"{DNS_STATIC_PATH}/{record_id}"
        status, _ = await self.request(client, "DELETE", path)
        if status in (200, 204):
            return True, ""
        return False, self.last_error or f"删除失败（HTTP {status}）"

    async def add_record(
        self,
        client: Any,
        record: Dict[str, str],
    ) -> Tuple[bool, str, str]:
        """新增一条 DNS static，返回 (成功, .id, 错误)。"""
        status, body = await self.request(client, "PUT", DNS_STATIC_PATH, json_body=record)
        if status in (200, 201):
            return True, extract_record_id(body), ""
        return False, "", self.last_error or f"写入失败（HTTP {status}）"

    async def apply_domain(
        self,
        client: Any,
        domain: str,
        addresses: Sequence[str],
        records: Sequence[Dict[str, str]],
        *,
        cleanup_names: bool = False,
    ) -> Dict[str, Any]:
        """
        用一批新记录替换某域名的旧记录。

        清理范围：
        - 默认只删 comment 命中本插件的记录（不碰用户手写的同名记录）
        - `cleanup_names=True` 时额外清理同名 A 记录，适合希望完全接管解析的场景
        """
        result: Dict[str, Any] = {"domain": domain, "deleted": 0, "added": 0, "errors": []}
        comment = records[0]["comment"] if records else ""
        stale = await self.list_records(client, comment=comment) if comment else []
        if cleanup_names:
            by_name = await self.list_by_name(client, domain)
            existing = {item.id for item in stale}
            stale = list(stale) + [item for item in by_name if item.id not in existing]
        for item in stale:
            ok, err = await self.delete_record(client, item.id)
            if ok:
                result["deleted"] += 1
            elif err:
                result["errors"].append(err)
        for record in records:
            ok, _record_id, err = await self.add_record(client, record)
            if ok:
                result["added"] += 1
            elif err:
                result["errors"].append(err)
        result["addresses"] = list(addresses)
        return result
