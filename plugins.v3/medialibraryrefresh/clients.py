"""Emby / Jellyfin / Plex 精准刷新客户端。

分级策略（每级失败自动回退）：
- 精准级：Emby/Jellyfin 用 `POST /Library/Media/Updated`（单文件增量，支持
  Created/Modified/Deleted，不做全库扫描）；Plex 用
  `GET /library/sections/{key}/refresh?path=<父目录>`（按库局部扫描）。
- 父目录级：Emby `POST /Library/Refresh?Path=<目录>`；Plex 按目录逐个刷新。
- 库级/全库级：Emby `POST /Library/Refresh`；Plex 对每个媒体库发起刷新。

所有请求都带超时；任何异常都被收敛成 ServerResult，不向事件循环外抛。
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.sdk.network import AsyncRequestUtils

from .core import (
    RefreshItem,
    ServerConfig,
    collapse_parents,
    join_url,
    normalize_path,
    parent_dir,
)


@dataclass
class ServerResult:
    """单台服务器的一轮刷新结果。"""

    server: str
    server_type: str
    ok: bool = False
    level: str = ""
    requested: int = 0
    succeeded: int = 0
    message: str = ""
    details: List[str] = field(default_factory=list)
    duration: float = 0.0

    @property
    def level_label(self) -> str:
        return {
            "item": "精准单文件", "parent": "父目录", "library": "库级",
            "full": "全库", "check": "连通性检测", "dry": "试运行",
        }.get(self.level, "未执行")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "server": self.server, "server_type": self.server_type,
            "ok": self.ok, "level": self.level, "level_label": self.level_label,
            "requested": self.requested, "succeeded": self.succeeded,
            "message": self.message, "details": list(self.details),
            "duration": round(self.duration, 3),
        }


class _DryResponse:
    """试运行时的假响应，避免发出真实请求。"""

    status_code = 204

    @staticmethod
    def json() -> Dict[str, Any]:
        return {}

    text = ""


def parse_plex_libraries(xml_text: str) -> List[Dict[str, Any]]:
    """解析 Plex `/library/sections` 的 MediaContainer XML。"""
    if not xml_text or not xml_text.strip():
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    libraries: List[Dict[str, Any]] = []
    for node in root.findall(".//Directory"):
        key = str(node.get("key") or "").strip()
        if not key or not key.isdigit():
            continue
        locations: List[str] = []
        for location in node.findall(".//Location"):
            path = normalize_path(location.get("path"))
            if path:
                locations.append(path)
        libraries.append({
            "key": key,
            "title": str(node.get("title") or key),
            "type": str(node.get("type") or ""),
            "locations": sorted(set(locations), key=len, reverse=True),
        })
    return libraries


def match_library(path: Any, libraries: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """按最长 location 前缀把媒体服务器侧路径归属到 Plex 媒体库。"""
    target = normalize_path(path)
    if not target:
        return None
    best: Optional[Dict[str, Any]] = None
    best_len = -1
    for library in libraries:
        for raw in library.get("locations") or []:
            location = normalize_path(raw).rstrip("/")
            if not location:
                continue
            hit = target == location or target.startswith(location + "/")
            if hit and len(location) > best_len:
                best, best_len = library, len(location)
    return best


def _status_ok(status: int) -> bool:
    return 200 <= status < 300


class BaseClient:
    """刷新客户端公共骨架。"""

    def __init__(self, server: ServerConfig, *, timeout: float = 8.0,
                 verify: bool = False, dry_run: bool = False,
                 fallback: str = "parent") -> None:
        self.server = server
        self.timeout = max(float(timeout), 1.0)
        self.verify = bool(verify)
        self.dry_run = bool(dry_run)
        self.fallback = fallback if fallback in ("none", "parent", "library", "full") else "parent"
        self._http = AsyncRequestUtils(timeout=self.timeout, verify=self.verify)

    @property
    def label(self) -> str:
        return self.server.label

    def _headers(self) -> Dict[str, str]:
        return {}

    def _endpoint(self, path: str) -> str:
        return "/" + str(path).lstrip("/")

    async def _request(self, method: str, endpoint: str, *,
                       params: Optional[Dict[str, Any]] = None,
                       json_body: Optional[Any] = None) -> Tuple[Optional[Any], str]:
        url = join_url(self.server.url, endpoint)
        if not url:
            return None, "服务器地址为空"
        if self.dry_run:
            return _DryResponse(), ""
        headers = self._headers()
        kwargs: Dict[str, Any] = {"headers": headers, "timeout": self.timeout}
        if params:
            kwargs["params"] = params
        if json_body is not None:
            kwargs["json"] = json_body
        try:
            response = await self._http.request(method, url, **kwargs)
        except Exception as err:  # noqa: BLE001 - 单个服务器失败不影响其它服务器
            return None, f"{type(err).__name__}: {err}"
        if response is None:
            return None, "请求无响应（连接失败或超时）"
        status = int(getattr(response, "status_code", 0) or 0)
        if not _status_ok(status):
            return response, f"HTTP {status}"
        return response, ""

    async def check(self) -> Tuple[bool, str]:
        raise NotImplementedError

    async def _refresh_precise(self, items: Sequence[RefreshItem],
                               result: ServerResult) -> Tuple[bool, str]:
        raise NotImplementedError

    async def _refresh_dirs(self, dirs: Sequence[str],
                            result: ServerResult) -> Tuple[bool, str]:
        raise NotImplementedError

    async def _refresh_all(self, result: ServerResult) -> Tuple[bool, str]:
        raise NotImplementedError

    async def refresh(self, items: Sequence[RefreshItem]) -> ServerResult:
        """执行一次分级刷新；失败按配置逐级回退。"""
        started = time.time()
        result = ServerResult(
            server=self.server.name, server_type=self.server.type,
            requested=len(items),
        )
        if not items:
            result.ok, result.level, result.message = True, "item", "无待刷新项"
            result.duration = time.time() - started
            return result
        if self.dry_run:
            result.ok, result.level = True, "dry"
            result.succeeded = len(items)
            result.message = f"试运行：将向 {len(items)} 个路径发起精准刷新"
            result.details.append(
                "、".join(self.server.map_path(item.path) for item in list(items)[:8])
            )
            result.duration = time.time() - started
            return result

        ok, message = await self._refresh_precise(items, result)
        if ok:
            result.ok, result.level = True, "item"
            result.succeeded = len(items)
            result.message = message or "精准刷新已提交"
            result.duration = time.time() - started
            return result
        result.details.append(f"精准刷新失败：{message or '未知原因'}")

        if self.fallback in ("parent", "library", "full"):
            dirs = collapse_parents(items, self.server)
            ok, message = await self._refresh_dirs(dirs, result)
            if ok:
                result.ok, result.level = True, "parent"
                result.succeeded = len(items)
                result.message = message or f"已回退刷新 {len(dirs)} 个父目录"
                result.duration = time.time() - started
                return result
            result.details.append(f"父目录回退失败：{message or '未知原因'}")

        if self.fallback in ("library", "full"):
            ok, message = await self._refresh_all(result)
            if ok:
                result.ok, result.level = True, "full"
                result.succeeded = len(items)
                result.message = message or "已回退全库刷新"
                result.duration = time.time() - started
                return result
            result.details.append(f"全库回退失败：{message or '未知原因'}")

        result.ok = False
        result.level = ""
        result.message = "所有刷新层级均失败"
        result.duration = time.time() - started
        return result

    async def refresh_all(self) -> ServerResult:
        """定时对账用的全库刷新入口。"""
        started = time.time()
        result = ServerResult(server=self.server.name, server_type=self.server.type)
        ok, message = await self._refresh_all(result)
        result.ok, result.level = ok, ("full" if ok else "")
        result.message = message or ("全库刷新已提交" if ok else "全库刷新失败")
        result.duration = time.time() - started
        return result


class EmbyClient(BaseClient):
    """Emby / Jellyfin：Library/Media/Updated 单文件增量刷新。"""

    def _headers(self) -> Dict[str, str]:
        return {
            "X-Emby-Token": self.server.token,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _endpoint(self, path: str) -> str:
        clean = str(path).lstrip("/")
        prefix = "" if self.server.type == "jellyfin" else "emby/"
        return "/" + prefix + clean

    async def check(self) -> Tuple[bool, str]:
        response, error = await self._request("GET", self._endpoint("System/Info"))
        if error:
            return False, error
        name, version = self.server.name, ""
        try:
            data = response.json() or {}
            name = str(data.get("ServerName") or self.server.name)
            version = str(data.get("Version") or "")
        except Exception:  # noqa: BLE001 - 检测阶段不依赖响应体
            pass
        return True, f"{name} {version}".strip()

    async def _refresh_precise(self, items: Sequence[RefreshItem],
                               result: ServerResult) -> Tuple[bool, str]:
        updates = [
            {"Path": self.server.map_path(item.path), "UpdateType": item.update_type}
            for item in items
        ]
        result.details.append(
            f"POST {self._endpoint('Library/Media/Updated')} · {len(updates)} 个文件级更新"
        )
        response, error = await self._request(
            "POST", self._endpoint("Library/Media/Updated"),
            json_body={"Updates": updates},
        )
        if error:
            return False, error
        return True, f"Emby 已接收 {len(updates)} 条文件级更新（不触发全库扫描）"

    async def _refresh_dirs(self, dirs: Sequence[str],
                            result: ServerResult) -> Tuple[bool, str]:
        if not dirs:
            return False, "无可回退的父目录"
        failures: List[str] = []
        for directory in dirs:
            _, error = await self._request(
                "POST", self._endpoint("Library/Refresh"),
                params={"Path": directory},
            )
            if error:
                failures.append(f"{directory}: {error}")
            else:
                result.details.append(f"父目录刷新已提交：{directory}")
        if failures:
            return False, "；".join(failures[:3])
        return True, f"已刷新 {len(dirs)} 个父目录"

    async def _refresh_all(self, result: ServerResult) -> Tuple[bool, str]:
        _, error = await self._request("POST", self._endpoint("Library/Refresh"))
        if error:
            return False, error
        result.details.append("全库刷新已提交：POST /Library/Refresh")
        return True, "Emby 全库刷新已提交"


class PlexClient(BaseClient):
    """Plex：按库 + 父目录局部扫描。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._libraries: Optional[List[Dict[str, Any]]] = None

    def _headers(self) -> Dict[str, str]:
        return {"X-Plex-Token": self.server.token, "Accept": "application/xml"}

    async def _load_libraries(self) -> Tuple[List[Dict[str, Any]], str]:
        if self._libraries is not None:
            return self._libraries, ""
        response, error = await self._request("GET", "/library/sections")
        if error:
            return [], error
        text = str(getattr(response, "text", "") or "")
        libraries = parse_plex_libraries(text)
        if not libraries:
            return [], "未解析到任何媒体库（响应格式异常或无权限）"
        self._libraries = libraries
        return libraries, ""

    async def check(self) -> Tuple[bool, str]:
        libraries, error = await self._load_libraries()
        if error:
            # /library/sections 可能被反代拦截，退回 Plex 身份接口再探一次
            response, identity_error = await self._request("GET", "/identity")
            if identity_error:
                return False, f"{error}；identity: {identity_error}"
            return True, f"{self.server.name} 可连通（未解析到媒体库列表）"
        return True, f"{self.server.name} 可连通，共 {len(libraries)} 个媒体库"

    async def _refresh_precise(self, items: Sequence[RefreshItem],
                               result: ServerResult) -> Tuple[bool, str]:
        libraries, error = await self._load_libraries()
        if error:
            return False, error
        targets: Dict[Tuple[str, str], str] = {}
        unmatched: List[str] = []
        for item in items:
            remote = self.server.map_path(item.path)
            library = match_library(remote, libraries)
            if not library:
                unmatched.append(remote)
                continue
            targets[(str(library["key"]), parent_dir(remote))] = str(library.get("title") or "")
        if unmatched:
            return False, f"{len(unmatched)} 个路径未匹配到媒体库，例如 {unmatched[0]}"
        if not targets:
            return False, "没有可用的刷新目标"
        failures: List[str] = []
        for (key, directory), title in targets.items():
            _, request_error = await self._request(
                "GET", f"/library/sections/{key}/refresh",
                params={"path": directory},
            )
            if request_error:
                failures.append(f"{title}/{directory}: {request_error}")
            else:
                result.details.append(f"库 {key}({title}) 局部刷新：{directory}")
        if failures:
            return False, "；".join(failures[:3])
        return True, f"Plex 已提交 {len(targets)} 个目录的局部刷新"

    async def _refresh_dirs(self, dirs: Sequence[str],
                            result: ServerResult) -> Tuple[bool, str]:
        libraries, error = await self._load_libraries()
        if error:
            return False, error
        keys = {str(lib["key"]) for lib in libraries}
        matched = 0
        failures: List[str] = []
        for directory in dirs:
            library = match_library(directory, libraries)
            if library:
                _, request_error = await self._request(
                    "GET", f"/library/sections/{library['key']}/refresh",
                    params={"path": directory},
                )
                if request_error:
                    failures.append(f"{directory}: {request_error}")
                else:
                    matched += 1
                    result.details.append(f"父目录刷新已提交：{directory}")
        if failures:
            return False, "；".join(failures[:3])
        if matched == 0:
            # 目录不属于任何库时不冒充父目录级成功，交给配置的库级/全库回退处理
            return False, f"{len(dirs)} 个父目录均不属于任何媒体库（共 {len(keys)} 个库）"
        return True, f"已刷新 {matched} 个父目录"

    async def _refresh_all(self, result: ServerResult) -> Tuple[bool, str]:
        libraries, error = await self._load_libraries()
        if error:
            return False, error
        failures: List[str] = []
        for library in libraries:
            _, request_error = await self._request(
                "GET", f"/library/sections/{library['key']}/refresh"
            )
            if request_error:
                failures.append(f"库 {library['key']}: {request_error}")
        if failures:
            return False, "；".join(failures[:3])
        result.details.append(f"已刷新全部 {len(libraries)} 个媒体库")
        return True, f"Plex 已刷新全部 {len(libraries)} 个媒体库"


def build_client(server: ServerConfig, *, timeout: float = 8.0, verify: bool = False,
                 dry_run: bool = False, fallback: str = "parent") -> BaseClient:
    """按服务器类型构造客户端。"""
    cls = PlexClient if server.is_plex else EmbyClient
    return cls(server, timeout=timeout, verify=verify, dry_run=dry_run, fallback=fallback)
