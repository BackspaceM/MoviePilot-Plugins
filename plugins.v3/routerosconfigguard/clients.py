"""RouterOS 配置备份与漂移检测 —— 外部系统客户端。

包含：
- RouterOSGuardClient：通过注入的 AsyncRequestUtils 读取各配置分区 + 连通性校验。
- ServiceMonitorHelper：封装 MoviePilot 宿主的服务发现门面（DownloaderHelper /
  MediaServerHelper），为配置页下拉框提供"已连接实例"选项，并对勾选实例做在线巡检。

宿主相关导入全部做 try/except 降级并延迟到方法内，便于脱离 MP 环境离线测试。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .core import Section, SECTION_MAP, ServiceHealth


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

    async def request(self, method: str, path: str, client: Any = None) -> Tuple[int, Any]:
        """发一次 REST 请求，返回 (状态码, 响应体)。"""
        transport = client or self.client
        if transport is None or not self.base_url:
            self.last_error = "RouterOS 地址为空或网络模块不可用"
            return 0, None
        try:
            response = await transport.request(
                method, self._url(path),
                headers=self._headers(), timeout=self.timeout,
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


# ---------------------------------------------------------------------------
# MoviePilot 宿主服务发现（下拉框数据源 + 在线巡检）
# ---------------------------------------------------------------------------

DOWNLOADER_TYPES = ("qbittorrent", "transmission")
MEDIA_TYPES = ("emby", "plex", "jellyfin")


class ServiceMonitorHelper:
    """封装宿主 app.sdk.services 的门面，提供：

    - downloader_options() / media_options()：给配置页 VSelect 的 {title,value} 列表；
    - check(names, ...)：对勾选实例做在线巡检，返回 ServiceHealth 列表。

    所有宿主导入延迟且容错，宿主不可用或版本差异时降级为空，不影响插件主流程。
    """

    # ---- 下拉框数据源：已连接的下载器 ----
    @staticmethod
    def downloader_options() -> List[Dict[str, str]]:
        items: List[Dict[str, str]] = []
        # 优先 ServiceConfigHelper（能按 enabled + type 精确过滤）
        try:
            from app.sdk.services import ServiceConfigHelper  # type: ignore
            for conf in (ServiceConfigHelper.get_downloader_configs() or []):
                if not getattr(conf, "enabled", False):
                    continue
                name = getattr(conf, "name", "") or ""
                stype = str(getattr(conf, "type", "") or "").lower()
                if name and stype in DOWNLOADER_TYPES:
                    items.append({"title": f"{name}（{stype}）", "value": name})
        except Exception:  # noqa: BLE001
            items = []
        if not items:
            # 回退：DownloaderHelper().get_configs()
            try:
                from app.sdk.services import DownloaderHelper  # type: ignore
                for name, conf in (DownloaderHelper().get_configs() or {}).items():
                    stype = str(getattr(conf, "type", "") or "").lower()
                    if name and (not stype or stype in DOWNLOADER_TYPES):
                        items.append({"title": f"{name}（{stype or '下载器'}）", "value": str(name)})
            except Exception:  # noqa: BLE001
                pass
        return items

    # ---- 下拉框数据源：已连接的媒体服务器 ----
    @staticmethod
    def media_options() -> List[Dict[str, str]]:
        items: List[Dict[str, str]] = []
        try:
            from app.sdk.services import MediaServerHelper  # type: ignore
            for name, conf in (MediaServerHelper().get_configs() or {}).items():
                stype = str(getattr(conf, "type", "") or "").lower()
                if name and (not stype or stype in MEDIA_TYPES):
                    items.append({"title": f"{name}（{stype or '媒体'}）", "value": str(name)})
        except Exception:  # noqa: BLE001
            pass
        return items

    # ---- 在线巡检 ----
    @staticmethod
    def check(downloader_names: Optional[List[str]] = None,
              media_names: Optional[List[str]] = None) -> List[ServiceHealth]:
        health: List[ServiceHealth] = []
        health.extend(ServiceMonitorHelper._check_group("下载器", downloader_names, "downloader"))
        health.extend(ServiceMonitorHelper._check_group("媒体服务器", media_names, "media"))
        return health

    @staticmethod
    def _check_group(category: str, names: Optional[List[str]], kind: str) -> List[ServiceHealth]:
        out: List[ServiceHealth] = []
        names = [str(n).strip() for n in (names or []) if str(n).strip()]
        if not names:
            return out
        try:
            from app.sdk.services import DownloaderHelper, MediaServerHelper  # type: ignore
            if kind == "downloader":
                services = DownloaderHelper().get_services(name_filters=names) or {}
            else:
                services = MediaServerHelper().get_services(name_filters=names) or {}
        except Exception as err:  # noqa: BLE001
            return [ServiceHealth(category=category, name=n, online=False,
                                  detail=f"服务发现不可用：{type(err).__name__}") for n in names]

        by_name = {str(k): v for k, v in services.items()}
        for name in names:
            service = by_name.get(name)
            if service is None:
                out.append(ServiceHealth(category=category, name=name, online=False,
                                         detail="未在 MP 已连接实例中找到（若为手动补充项属正常）"))
                continue
            stype = str(getattr(service, "type", "") or "")
            if not stype:
                conf = getattr(service, "config", None)
                stype = str(getattr(conf, "type", "") or "")
            instance = getattr(service, "instance", None)
            if instance is None:
                out.append(ServiceHealth(category=category, name=name, stype=stype,
                                         online=False, detail="实例不存在"))
                continue
            try:
                inactive = bool(instance.is_inactive())
            except Exception:  # noqa: BLE001
                inactive = False
            out.append(ServiceHealth(
                category=category, name=name, stype=stype,
                online=not inactive, detail="在线" if not inactive else "未连接/离线",
            ))
        return out


def build_routeros_client(address: str, username: str, password: str,
                          http: Any, port: int = 443, use_ssl: bool = True,
                          timeout: float = 10.0) -> RouterOSGuardClient:
    return RouterOSGuardClient(address, username, password, client=http,
                               port=port, use_ssl=use_ssl, timeout=timeout)
