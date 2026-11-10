"""RouterOS 场景模板一键部署 —— 通用 v7 REST 客户端。

与防火墙管理器只操作四张表不同，这里要写「任意配置分区」：
GET    /rest/<section>              读取现有条目
PUT    /rest/<section>              新增条目（防火墙规则支持 place-before）
DELETE /rest/<section>/<id>         删除指定 id（回滚只删本次新建的条目）
POST   /rest/system/backup/save    改动前在 RouterOS 本机留系统备份

网络层通过注入的 app.sdk.network.AsyncRequestUtils 发起，导入全部延迟容错，
便于脱离 MoviePilot 宿主做离线测试。
"""
from __future__ import annotations

import base64
import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .templates import SECTIONS


def normalize_base_url(host: str, port: int = 443, use_ssl: bool = True) -> str:
    """规范化为 scheme://host[:port]，忽略用户误带的 /rest 等路径。"""
    text = str(host or "").strip()
    if not text:
        return ""
    if "://" in text:
        match = re.match(r"^(https?)://([^/:]+)(?::(\d+))?", text, re.IGNORECASE)
        if not match:
            return ""
        scheme, netloc = match.group(1).lower(), match.group(2)
        chosen_port = match.group(3) or str(port)
    else:
        scheme = "https" if use_ssl else "http"
        netloc = text.split("/")[0]
        chosen_port = str(port)
    if (scheme == "https" and chosen_port == "443") or (scheme == "http" and chosen_port == "80"):
        return f"{scheme}://{netloc}"
    return f"{scheme}://{netloc}:{chosen_port}"


def basic_auth_header(username: str, password: str) -> Dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def extract_created_id(data: Any) -> str:
    """从 PUT 响应里取出新建条目 id；RouterOS 不同分区返回 .id / ret / 对象数组。"""
    if isinstance(data, dict):
        for key in (".id", "ret", "id"):
            value = data.get(key)
            if value:
                return str(value)
    elif isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            for key in (".id", "ret", "id"):
                if first.get(key):
                    return str(first[key])
        elif isinstance(first, str):
            return first
    return ""


class RouterOSConfigClient:
    """面向任意 /rest 分区的最小 CRUD 客户端。"""

    def __init__(self, address: str, username: str, password: str,
                 client: Any = None, port: int = 443, use_ssl: bool = True,
                 timeout: int = 10, dry_run: bool = False):
        self._address = address
        self._username = username
        self._password = password
        self._client = client
        self._timeout = timeout
        self._dry_run = dry_run
        self._base = normalize_base_url(address, port, use_ssl)
        self.last_error = ""

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    def _headers(self) -> Dict[str, str]:
        headers = basic_auth_header(self._username, self._password)
        headers["Content-Type"] = "application/json"
        return headers

    async def _request(self, method: str, path: str, client: Any = None, **kwargs: Any) -> Any:
        transport = client if client is not None else self._client
        if transport is None or not self._base:
            self.last_error = "RouterOS 地址为空或网络模块不可用"
            raise RuntimeError(self.last_error)
        try:
            return await transport.request(
                method, f"{self._base}{path}",
                headers=self._headers(), timeout=self._timeout, **kwargs,
            )
        except RuntimeError:
            raise
        except Exception as err:  # noqa: BLE001
            self.last_error = f"{type(err).__name__}: {err}"
            raise

    @staticmethod
    def _status_ok(resp: Any, codes: Sequence[int] = (200, 201, 204)) -> bool:
        return resp is not None and int(getattr(resp, "status_code", 0) or 0) in codes

    @staticmethod
    def _section(section: str) -> str:
        """只接受白名单内的分区，杜绝把用户输入拼进 URL 造成越权写入。"""
        if section not in SECTIONS:
            raise ValueError(f"未登记的 RouterOS 分区：{section}")
        return section

    async def check(self, client: Any = None) -> Tuple[bool, str]:
        try:
            resp = await self._request("GET", "/rest/system/identity", client=client)
            if not self._status_ok(resp):
                return False, f"HTTP {getattr(resp, 'status_code', '?')} {self.last_error}"
            try:
                info = resp.json() or {}
                name = info.get("name") if isinstance(info, dict) else ""
                ver = info.get("version") if isinstance(info, dict) else ""
                return True, f"RouterOS 连接正常（{name}{' v' + ver if ver else ''}）"
            except Exception:  # noqa: BLE001
                return True, "RouterOS 连接正常"
        except Exception as err:  # noqa: BLE001
            return False, f"连接失败：{type(err).__name__} {err}"

    async def list_section(self, section: str, client: Any = None) -> Optional[List[Dict[str, Any]]]:
        """读取分区现有条目；失败返回 None（与「空分区」区分，避免幂等误判）。"""
        path = self._section(section)
        try:
            resp = await self._request("GET", path, client=client)
            if not self._status_ok(resp):
                return None
            data = resp.json()
            if not isinstance(data, list):
                return None
            return [dict(row) for row in data if isinstance(row, dict)]
        except Exception:  # noqa: BLE001 - 读取失败返回 None，由调用方决定降级策略
            return None

    async def create(self, section: str, payload: Dict[str, Any],
                     place_before: str = "", client: Any = None) -> Tuple[bool, str, str]:
        """新增一条配置，返回 (是否成功, 说明, 新建条目 id)。"""
        path = self._section(section)
        body = dict(payload or {})
        if place_before:
            body["place-before"] = str(place_before)
        label = str(body.get("comment") or body.get("name") or section)
        if self._dry_run:
            return True, f"试运行：将写入 {label}" + (f"（{place_before} 之前）" if place_before else ""), ""
        try:
            resp = await self._request("PUT", path, json=body, client=client)
            if not self._status_ok(resp):
                return False, f"写入失败 HTTP {getattr(resp, 'status_code', '?')}：{label}", ""
            created_id = ""
            try:
                created_id = extract_created_id(resp.json())
            except Exception:  # noqa: BLE001
                pass
            return True, f"已写入 {label}", created_id
        except ValueError as err:
            return False, f"写入被拒绝：{err}", ""
        except Exception as err:  # noqa: BLE001
            return False, f"写入异常 {type(err).__name__}：{err}", ""

    async def delete(self, section: str, rule_id: str,
                     client: Any = None) -> Tuple[bool, str]:
        """删除指定 id；供失败自动回滚与手动回滚使用。"""
        path = self._section(section)
        rid = str(rule_id or "").strip()
        if not rid:
            return False, "缺少待删除条目 id"
        if self._dry_run:
            return True, f"试运行：将删除 {path}/{rid}"
        try:
            resp = await self._request("DELETE", f"{path}/{rid}", client=client)
            return (True, f"已回滚 {path}/{rid}") if self._status_ok(resp) else (
                False, f"回滚失败 HTTP {getattr(resp, 'status_code', '?')}：{path}/{rid}")
        except ValueError as err:
            return False, f"回滚被拒绝：{err}"
        except Exception as err:  # noqa: BLE001
            return False, f"回滚异常 {type(err).__name__}：{err}"

    async def save_backup(self, name: str, client: Any = None) -> Tuple[bool, str]:
        """在 RouterOS 本机创建 .backup 系统备份（改动前留后路）。"""
        clean = "".join(c for c in str(name or "mp-a2") if c.isalnum() or c in "_-") or "mp-a2"
        if self._dry_run:
            return True, f"试运行：将在 RouterOS 创建备份 {clean}"
        try:
            resp = await self._request("POST", "/rest/system/backup/save",
                                       json={"name": clean}, client=client)
            return (True, f"已在 RouterOS 创建备份 {clean}") if self._status_ok(resp) else (
                False, f"RouterOS 备份失败 HTTP {getattr(resp, 'status_code', '?')}")
        except Exception as err:  # noqa: BLE001
            return False, f"RouterOS 备份异常 {type(err).__name__}：{err}"

    async def export_snapshot(self, sections: Sequence[str],
                              client: Any = None) -> Dict[str, Any]:
        """导出涉及分区的现有配置，存 MP 用于人工对照（回滚不依赖它）。"""
        out: Dict[str, List[Dict[str, Any]]] = {}
        failed: List[str] = []
        for section in sections:
            try:
                rows = await self.list_section(section, client=client)
            except Exception:  # noqa: BLE001
                rows = None
            if rows is None:
                failed.append(section)
                continue
            out[section] = rows
        return {
            "ts": time.time(), "created": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "sections": out, "failed": failed,
            "count": sum(len(v) for v in out.values()),
        }


def build_config_client(address: str, username: str, password: str, http: Any,
                        port: int = 443, use_ssl: bool = True, timeout: int = 10,
                        dry_run: bool = False) -> RouterOSConfigClient:
    return RouterOSConfigClient(address, username, password, client=http, port=port,
                                use_ssl=use_ssl, timeout=timeout, dry_run=dry_run)
