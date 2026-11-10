"""带宽/Queue 智能调度 —— 外部系统客户端。

包含：RouterOS 简单队列的读取/创建/更新/删除与批量编排、实时速率读取、
以及 FastTrack 规则诊断（FastTrack 匹配的流量会绕过 Queue 导致限速失效）。

所有请求通过注入的 app.sdk.network.AsyncRequestUtils 发起，并统一做超时、异常收敛
与试运行处理；宿主相关导入全部延迟且容错，便于脱离 MP 环境离线测试。
"""

from __future__ import annotations

import base64
import re
from typing import Any, Dict, List, Optional, Tuple

from .core import diff_queue_specs


def normalize_base_url(host: str, port: int = 443, use_ssl: bool = True) -> str:
    """规范化为 scheme://host[:port]，忽略用户误带的 /rest 等路径（客户端会自拼 /rest）。"""
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
    # 默认端口省略，保持 URL 简洁
    if (scheme == "https" and chosen_port == "443") or (scheme == "http" and chosen_port == "80"):
        return f"{scheme}://{netloc}"
    return f"{scheme}://{netloc}:{chosen_port}"


def basic_auth_header(username: str, password: str) -> Dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


class RouterOSQueueClient:
    """通过 RouterOS v7 REST 管理"本插件前缀"的简单队列。"""

    def __init__(self, address: str, username: str, password: str,
                 client: Any = None, prefix: str = "bqs", port: int = 443,
                 use_ssl: bool = True, timeout: int = 10, dry_run: bool = False):
        self._address = address
        self._username = username
        self._password = password
        self._client = client
        self._prefix = prefix or "bqs"
        self._timeout = timeout
        self._dry_run = dry_run
        self._base = normalize_base_url(address, port, use_ssl)
        self.last_error = ""

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    def _headers(self) -> Dict[str, str]:
        return basic_auth_header(self._username, self._password)

    async def _request(self, method: str, path: str, client: Any = None, **kwargs):
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

    async def check(self, client: Any = None) -> Tuple[bool, str]:
        try:
            resp = await self._request("GET", "/rest/system/identity", client=client)
            ok = resp is not None and int(getattr(resp, "status_code", 0) or 0) == 200
            return ok, "RouterOS 连接正常" if ok else f"HTTP {getattr(resp, 'status_code', '?')} {self.last_error}"
        except Exception as err:  # noqa: BLE001
            return False, f"连接失败：{type(err).__name__} {err}"

    @staticmethod
    def _status_ok(resp: Any, codes=(200, 201, 204)) -> bool:
        return resp is not None and int(getattr(resp, "status_code", 0) or 0) in codes

    @staticmethod
    def _payload(spec: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "name": spec.get("name"),
            "target": spec.get("target"),
            "max-limit": spec.get("max_limit", "0/0"),
            "limit-at": spec.get("limit_at", "0/0"),
            "priority": int(spec.get("priority", 5)),
            "comment": spec.get("comment", ""),
            "disabled": False,
        }

    async def list_queues(self, client: Any = None) -> List[Dict[str, Any]]:
        try:
            resp = await self._request("GET", "/rest/queue/simple", client=client)
            if not self._status_ok(resp):
                return []
            data = resp.json()
            return data if isinstance(data, list) else []
        except Exception:  # noqa: BLE001
            return []

    async def _create(self, spec: Dict[str, Any]) -> Tuple[bool, str]:
        name = spec.get("name", "")
        if self._dry_run:
            return True, f"试运行：将创建 {name} 上/下限 {spec.get('max_limit')}"
        try:
            resp = await self._request("POST", "/rest/queue/simple", json=self._payload(spec))
            return (True, f"{name} 已创建") if self._status_ok(resp) else (
                False, f"{name} 创建失败 HTTP {getattr(resp, 'status_code', '?')}")
        except Exception as err:  # noqa: BLE001
            return False, f"{name} 创建异常：{type(err).__name__} {err}"

    async def _update(self, row_id: str, spec: Dict[str, Any]) -> Tuple[bool, str]:
        name = spec.get("name", "")
        if self._dry_run:
            return True, f"试运行：将更新 {name} 上/下限 {spec.get('max_limit')}"
        try:
            resp = await self._request("PUT", f"/rest/queue/simple/{row_id}", json=self._payload(spec))
            return (True, f"{name} 已更新") if self._status_ok(resp) else (
                False, f"{name} 更新失败 HTTP {getattr(resp, 'status_code', '?')}")
        except Exception as err:  # noqa: BLE001
            return False, f"{name} 更新异常：{type(err).__name__} {err}"

    async def _delete(self, row_id: str, name: str) -> Tuple[bool, str]:
        if self._dry_run:
            return True, f"试运行：将删除 {name}"
        try:
            resp = await self._request("DELETE", f"/rest/queue/simple/{row_id}")
            return (True, f"{name} 已删除") if self._status_ok(resp) else (
                False, f"{name} 删除失败 HTTP {getattr(resp, 'status_code', '?')}")
        except Exception as err:  # noqa: BLE001
            return False, f"{name} 删除异常：{type(err).__name__} {err}"

    async def ensure_queue(self, spec: Dict[str, Any], client: Any = None) -> Tuple[bool, str]:
        """确保单条期望队列存在且参数一致（内部先读取现状再建/改）。"""
        rows = await self.list_queues(client=client)
        existing = next((r for r in rows if str(r.get("name") or "") == spec.get("name")), None)
        if existing:
            return await self._update(str(existing.get(".id")), spec)
        return await self._create(spec)

    async def apply_specs(self, specs: List[Dict[str, Any]],
                          client: Any = None) -> Dict[str, Any]:
        """按差异批量编排队列：只新建/更新/删除本插件前缀的队列。"""
        rows = await self.list_queues(client=client)
        plan = diff_queue_specs(rows, specs, self._prefix)
        created: List[Dict[str, Any]] = []
        updated: List[Dict[str, Any]] = []
        removed: List[Dict[str, Any]] = []
        errors: List[str] = []
        notes: List[str] = []

        for spec in plan["create"]:
            ok, msg = await self._create(spec)
            (created if ok else errors).append(spec if ok else msg)
            notes.append(msg)
        for upd in plan["update"]:
            ok, msg = await self._update(str(upd.get("id")), upd["spec"])
            (updated if ok else errors).append(upd if ok else msg)
            notes.append(msg)
        for rem in plan["remove"]:
            ok, msg = await self._delete(str(rem.get("id")), rem.get("name", ""))
            (removed if ok else errors).append(rem if ok else msg)
            notes.append(msg)

        return {
            "ok": not errors,
            "profile": "",
            "created": created, "updated": updated, "removed": removed,
            "unchanged": plan["unchanged"], "errors": errors, "notes": notes,
            "dry_run": self._dry_run,
        }

    async def read_rates(self, client: Any = None) -> List[Dict[str, Any]]:
        rows = await self.list_queues(client=client)
        return [r for r in rows if str(r.get("name") or "").startswith(self._prefix + "_")]

    async def check_fasttrack(self, client: Any = None) -> Dict[str, Any]:
        """诊断是否启用了 FastTrack（会绕过 Queue，导致限速不生效）。"""
        try:
            resp = await self._request("GET", "/rest/ip/firewall/filter", client=client)
            if not self._status_ok(resp):
                return {"has_fasttrack": False, "rules": [],
                        "error": f"HTTP {getattr(resp, 'status_code', '?')}"}
            data = resp.json()
            rows = data if isinstance(data, list) else []
            hits: List[str] = []
            for row in rows:
                action = str(row.get("action") or "").lower()
                disabled = str(row.get("disabled") or "false").lower() in ("true", "yes", "1")
                if action == "fasttrack-connection" and not disabled:
                    hits.append(f"{row.get('chain', '?')}｜{row.get('action')}"
                                f"｜{row.get('comment', '') or '无备注'}")
            return {"has_fasttrack": bool(hits), "rules": hits}
        except Exception as err:  # noqa: BLE001
            return {"has_fasttrack": False, "rules": [], "error": f"{type(err).__name__}: {err}"}

    async def clear_our_queues(self, client: Any = None) -> Dict[str, Any]:
        """删除全部本插件前缀的队列（不影响用户手工队列）。"""
        rows = await self.list_queues(client=client)
        removed: List[str] = []
        errors: List[str] = []
        for row in rows:
            name = str(row.get("name") or "")
            if name.startswith(self._prefix + "_"):
                ok, msg = await self._delete(str(row.get(".id")), name)
                (removed if ok else errors).append(name if ok else msg)
        return {"ok": not errors, "removed": removed, "errors": errors, "dry_run": self._dry_run}


def build_queue_client(address: str, username: str, password: str, http: Any,
                       prefix: str = "bqs", port: int = 443, use_ssl: bool = True,
                       timeout: int = 10, dry_run: bool = False) -> RouterOSQueueClient:
    return RouterOSQueueClient(address, username, password, client=http, prefix=prefix,
                               port=port, use_ssl=use_ssl, timeout=timeout, dry_run=dry_run)
