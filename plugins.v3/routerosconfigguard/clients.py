"""RouterOS 配置备份与漂移检测 —— 外部系统客户端。

RouterOSGuardClient：通过注入的 AsyncRequestUtils
- 读取各配置分区 + 连通性校验（漂移检测数据源，只读）；
- 在 RouterOS 本机生成系统备份（/system/backup/save → .backup）与配置导出
  （/export → .rsc），并支持列举 / 删除文件以做轮转。

HTTP 传输由调用方注入（宿主 AsyncRequestUtils），本类只负责 URL/鉴权/解析，
便于离线单测注入假响应。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .core import SECTION_MAP


def normalize_base_url(host: str, port: int = 443, use_ssl: bool = True) -> str:
    """把用户输入的地址规范化为 scheme://host[:port]（剥掉多写的 /rest）。"""
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


def basic_auth_header(username: str, password: str) -> Dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def describe_error(status: int, body: Any) -> str:
    """把 REST 错误翻译成可执行的中文提示。"""
    detail = ""
    if isinstance(body, dict):
        detail = str(body.get("detail") or body.get("message") or body.get("error") or "")
    elif isinstance(body, str):
        detail = body.strip()[:200]
    if status in (401, 403):
        return f"认证失败（HTTP {status}）：检查用户名/密码，并确认该用户具备 read / rest-api 权限"
    if status == 404:
        return "接口不存在（HTTP 404）：需 RouterOS v7 并启用 REST（/ip/service 中 www 或 www-ssl）"
    if status == 400:
        return f"参数被拒绝（HTTP 400）：{detail or '检查路径是否正确'}"
    if status >= 500:
        return f"RouterOS 内部错误（HTTP {status}）：{detail or '请查看路由器日志'}"
    return f"请求失败（HTTP {status}）：{detail or '无响应体'}"


@dataclass
class FetchResult:
    ok_count: int = 0
    fail_count: int = 0
    rows: Dict[str, List[Any]] = field(default_factory=dict)
    errors: Dict[str, str] = field(default_factory=dict)
    identity: Dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.fail_count == 0


class RouterOSGuardClient:
    """RouterOS REST 只读客户端：拉取分区配置用于备份。

    HTTP 传输由调用方注入（宿主 AsyncRequestUtils），本类只负责 URL/鉴权/解析，
    便于离线单测注入假响应。
    """

    def __init__(self, address: str, username: str, password: str,
                 client: Any = None, port: int = 443, use_ssl: bool = True,
                 timeout: float = 10.0, verify: bool = False):
        self.username = username
        self.password = password
        self.client = client
        self.timeout = timeout
        self.verify = verify
        self.base_url = normalize_base_url(address, port, use_ssl)
        self.last_error = ""

    def _url(self, path: str) -> str:
        return f"{self.base_url}/rest{path}"

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        headers.update(basic_auth_header(self.username, self.password))
        return headers

    async def request(self, method: str, path: str, client: Any = None,
                      json_body: Optional[Dict[str, Any]] = None) -> Tuple[int, Any]:
        """发一次 REST 请求，返回 (状态码, 响应体)。"""
        transport = client or self.client
        if transport is None or not self.base_url:
            self.last_error = "RouterOS 地址为空或网络模块不可用"
            return 0, None
        try:
            response = await transport.request(
                method, self._url(path),
                headers=self._headers(), json=json_body, timeout=self.timeout,
            )
        except Exception as err:  # noqa: BLE001
            self.last_error = f"连接异常：{type(err).__name__} {err}"
            return 0, None
        if response is None:
            self.last_error = "无法连接路由器：检查地址/端口、证书校验与网络可达性"
            return 0, None
        status = int(getattr(response, "status_code", 0) or 0)
        body: Any = None
        try:
            body = response.json()
        except Exception:  # noqa: BLE001
            try:
                body = response.text
            except Exception:  # noqa: BLE001
                body = ""
        if status >= 400:
            self.last_error = describe_error(status, body)
        return status, body

    async def check(self, client: Any = None) -> Tuple[bool, str]:
        """连通性与认证校验（GET /system/identity）。"""
        status, body = await self.request("GET", "/system/identity", client=client)
        if status == 200 and isinstance(body, dict):
            name = body.get("name") or "RouterOS"
            version = body.get("version") or "v7"
            return True, f"连接成功：{name}（{version}）"
        return False, self.last_error or "连接失败"

    async def fetch(self, section_keys: List[str], client: Any = None,
                    max_rows: int = 2000) -> FetchResult:
        """按分区键抓取配置。单个分区失败仅记录，不影响其余分区。"""
        transport = client or self.client
        result = FetchResult()
        # 先取系统标识作为快照元信息
        status, ident = await self.request("GET", "/system/identity", client=transport)
        if status == 200 and isinstance(ident, dict):
            result.identity = {
                "name": str(ident.get("name") or ""),
                "version": str(ident.get("version") or ""),
            }
        for key in section_keys:
            sec = SECTION_MAP.get(key)
            if not sec:
                result.errors[key] = "未知分区"
                result.fail_count += 1
                continue
            status, body = await self.request("GET", sec.path, client=transport)
            if status != 200:
                result.errors[key] = self.last_error or f"HTTP {status}"
                result.fail_count += 1
                continue
            if isinstance(body, list):
                rows = body[:max_rows]
                truncated = len(body) > max_rows
                result.rows[key] = rows
                result.ok_count += 1
                if truncated:
                    result.errors[key] = f"分区行数超过 {max_rows}，已截断（建议缩小备份范围）"
            elif isinstance(body, dict):
                # 单条配置（如 identity / discovery-settings）
                result.rows[key] = [body]
                result.ok_count += 1
            else:
                result.rows[key] = []
                result.ok_count += 1
        return result

    # ---- RouterOS 本机备份（可选，受 backup_target 控制）----

    async def backup_save(self, name: str, password: str = "",
                          client: Any = None) -> Tuple[bool, str]:
        """在 RouterOS 本机生成二进制系统备份 .backup（POST /system/backup/save）。

        密码为空时 RouterOS 会生成未加密备份（有安全风险），因此调用方应配置
        backup_password；此处仅在有密码时下发 password 字段。
        """
        body: Dict[str, Any] = {"name": name}
        if password:
            body["password"] = password
        status, resp = await self.request("POST", "/system/backup/save",
                                           client=client, json_body=body)
        if status in (200, 201):
            return True, f"{name}.backup"
        return False, self.last_error or f"系统备份失败（HTTP {status}）"

    async def export_config(self, name: str, client: Any = None) -> Tuple[bool, str]:
        """导出可读配置脚本 .rsc（POST /export，文件写入 RouterOS 本机）。"""
        status, _ = await self.request("POST", "/export", client=client,
                                        json_body={"file": name})
        if status in (200, 201):
            return True, f"{name}.rsc"
        return False, self.last_error or f"配置导出失败（HTTP {status}）"

    async def list_files(self, client: Any = None) -> List[Dict[str, Any]]:
        """列举 RouterOS 文件（GET /file），用于备份文件轮转。失败返回空列表。"""
        status, body = await self.request("GET", "/file", client=client)
        if status == 200 and isinstance(body, list):
            return [f for f in body if isinstance(f, dict)]
        return []

    async def delete_file(self, file_id: str, client: Any = None) -> Tuple[bool, str]:
        """按 .id 删除 RouterOS 文件（DELETE /file/{.id}）。"""
        if not file_id:
            return False, "文件 ID 为空"
        status, _ = await self.request("DELETE", f"/file/{file_id}", client=client)
        if status in (200, 204):
            return True, ""
        return False, self.last_error or f"删除文件失败（HTTP {status}）"


def build_routeros_client(address: str, username: str, password: str,
                          http: Any, port: int = 443, use_ssl: bool = True,
                          timeout: float = 10.0) -> RouterOSGuardClient:
    return RouterOSGuardClient(address, username, password, client=http,
                               port=port, use_ssl=use_ssl, timeout=timeout)
