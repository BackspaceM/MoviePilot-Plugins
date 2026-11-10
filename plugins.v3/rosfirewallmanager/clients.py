"""RouterOS 防火墙可视化管理器 —— RouterOS v7 REST 客户端。

封装 filter/nat/mangle/raw 四张表的读取、单条规则新增/修改/启停/移动/删除、
以及整表导出快照。所有请求通过注入的 app.sdk.network.AsyncRequestUtils 发起，
统一做超时、异常收敛与试运行(dry_run)处理；宿主相关导入全部延迟且容错，
便于脱离 MP 离线测试。

REST 语义（已核实）：
- GET  /rest/ip/firewall/{table}            -> 有序规则数组（顺序=执行顺序）
- PUT  /rest/ip/firewall/{table}            -> 新增（追加末尾；带 place-before 可插到指定规则前）
- PATCH /rest/ip/firewall/{table}/{id}      -> 修改（disabled / comment / place-before 等）
- DELETE /rest/ip/firewall/{table}/{id}     -> 删除
- place-before=<首条 id> 置顶，=*ffffffff 置底（见 core.compute_move_place）
"""

from __future__ import annotations

import base64
import re
from typing import Any, Dict, List, Optional, Tuple

from .core import PLACE_BOTTOM, TABLES, Rule, choose_insert_place, compute_move_place


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


class RouterOSFirewallClient:
    """通过 RouterOS v7 REST 管理防火墙四张表。"""

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
        h = basic_auth_header(self._username, self._password)
        h["Content-Type"] = "application/json"
        return h

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

    @staticmethod
    def _status_ok(resp: Any, codes=(200, 201, 204)) -> bool:
        return resp is not None and int(getattr(resp, "status_code", 0) or 0) in codes

    async def check(self, client: Any = None) -> Tuple[bool, str]:
        try:
            resp = await self._request("GET", "/rest/system/identity", client=client)
            ok = self._status_ok(resp)
            if ok:
                try:
                    info = resp.json() or {}
                    name = info.get("name") if isinstance(info, dict) else ""
                    ver = info.get("version") if isinstance(info, dict) else ""
                    msg = "RouterOS 连接正常"
                    if name:
                        msg += f"（{name}"
                        if ver:
                            msg += f" v{ver}"
                        msg += "）"
                    return True, msg
                except Exception:  # noqa: BLE001
                    return True, "RouterOS 连接正常"
            return False, f"HTTP {getattr(resp, 'status_code', '?')} {self.last_error}"
        except Exception as err:  # noqa: BLE001
            return False, f"连接失败：{type(err).__name__} {err}"

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    async def list_table(self, table: str, client: Any = None) -> List[Dict[str, Any]]:
        table = str(table).lower()
        if table not in TABLES:
            return []
        try:
            resp = await self._request("GET", f"/rest/ip/firewall/{table}", client=client)
            if not self._status_ok(resp):
                return []
            data = resp.json()
            return data if isinstance(data, list) else []
        except Exception:  # noqa: BLE001
            return []

    async def list_all(self, client: Any = None,
                       tables: Optional[List[str]] = None) -> List[Rule]:
        """读取四张表全部规则，返回带 table 标记的规范 Rule 列表（保持各表内顺序）。"""
        targets = [t for t in (tables or TABLES) if t in TABLES]
        out: List[Rule] = []
        for table in targets:
            rows = await self.list_table(table, client=client)
            for raw in rows:
                if isinstance(raw, dict):
                    out.append(Rule.from_raw(raw, table))
        return out

    # ------------------------------------------------------------------
    # 单条操作
    # ------------------------------------------------------------------

    async def add_rule(self, table: str, payload: Dict[str, Any],
                       place_before: Optional[str] = None,
                       client: Any = None) -> Tuple[bool, str]:
        table = str(table).lower()
        if table not in TABLES:
            return False, f"未知表 {table}"
        body = dict(payload or {})
        if place_before:
            body["place-before"] = str(place_before)
        label = f"{body.get('chain', '?')}｜{body.get('action', '?')}｜{body.get('comment', '') or '无备注'}"
        if self._dry_run:
            return True, f"试运行：将新增 {label}" + (
                f"（插到 {place_before} 之前）" if place_before else "（追加末尾）")
        try:
            resp = await self._request("PUT", f"/rest/ip/firewall/{table}", json=body, client=client)
            return (True, f"已新增 {label}") if self._status_ok(resp) else (
                False, f"新增 {label} 失败 HTTP {getattr(resp, 'status_code', '?')}")
        except Exception as err:  # noqa: BLE001
            return False, f"新增 {label} 异常：{type(err).__name__} {err}"

    async def patch_rule(self, table: str, rule_id: str, changes: Dict[str, Any],
                         client: Any = None) -> Tuple[bool, str]:
        table = str(table).lower()
        if table not in TABLES:
            return False, f"未知表 {table}"
        if not rule_id:
            return False, "缺少规则 id"
        body = dict(changes or {})
        desc = ",".join(f"{k}={v}" for k, v in body.items())
        if self._dry_run:
            return True, f"试运行：将修改 {rule_id} {desc}"
        try:
            resp = await self._request("PATCH", f"/rest/ip/firewall/{table}/{rule_id}",
                                       json=body, client=client)
            return (True, f"{rule_id} 已修改 {desc}") if self._status_ok(resp) else (
                False, f"{rule_id} 修改失败 HTTP {getattr(resp, 'status_code', '?')}")
        except Exception as err:  # noqa: BLE001
            return False, f"{rule_id} 修改异常：{type(err).__name__} {err}"

    async def set_disabled(self, table: str, rule_id: str, disabled: bool,
                           client: Any = None) -> Tuple[bool, str]:
        return await self.patch_rule(table, rule_id, {"disabled": bool(disabled)}, client=client)

    async def set_comment(self, table: str, rule_id: str, comment: str,
                          client: Any = None) -> Tuple[bool, str]:
        return await self.patch_rule(table, rule_id, {"comment": str(comment or "")}, client=client)

    async def move_rule(self, table: str, rules: Sequence[Rule], rule_id: str,
                        mode: str, client: Any = None) -> Tuple[bool, str]:
        """移动规则。rules 为同表全部规则（有序），mode=up/down/top/bottom。"""
        table = str(table).lower()
        if table not in TABLES:
            return False, f"未知表 {table}"
        ids = [r.id for r in rules if r.table == table]
        need, place, note = compute_move_place(ids, rule_id, mode)
        if not need:
            return True, note  # 已在边界，无需改动（视为成功，按钮不会"点了没反应"）
        return await self.patch_rule(table, rule_id, {"place-before": str(place)}, client=client)

    async def remove_rule(self, table: str, rule_id: str,
                          client: Any = None) -> Tuple[bool, str]:
        table = str(table).lower()
        if table not in TABLES:
            return False, f"未知表 {table}"
        if not rule_id:
            return False, "缺少规则 id"
        if self._dry_run:
            return True, f"试运行：将删除 {rule_id}"
        try:
            resp = await self._request("DELETE", f"/rest/ip/firewall/{table}/{rule_id}", client=client)
            return (True, f"{rule_id} 已删除") if self._status_ok(resp) else (
                False, f"{rule_id} 删除失败 HTTP {getattr(resp, 'status_code', '?')}")
        except Exception as err:  # noqa: BLE001
            return False, f"{rule_id} 删除异常：{type(err).__name__} {err}"

    # ------------------------------------------------------------------
    # 快照导出
    # ------------------------------------------------------------------

    async def export_snapshot(self, client: Any = None,
                              tables: Optional[List[str]] = None) -> Dict[str, Any]:
        """导出四表规则快照（存到 MP plugin data，便于回滚对照）。"""
        import time
        rules = await self.list_all(client=client, tables=tables)
        return {
            "ts": time.time(),
            "tables": [t for t in (tables or TABLES) if t in TABLES],
            "count": len(rules),
            "rules": [r.to_dict() for r in rules],
        }

    async def save_routeros_backup(self, name: str, client: Any = None) -> Tuple[bool, str]:
        """在 RouterOS 本机创建配置备份（/system/backup/save），用于操作前留后路。"""
        clean = "".join(c for c in str(name or "rfm") if c.isalnum() or c in "_-") or "rfm"
        if self._dry_run:
            return True, f"试运行：将在 RouterOS 创建备份 {clean}"
        try:
            resp = await self._request("POST", "/rest/system/backup/save",
                                       json={"name": clean}, client=client)
            return (True, f"已在 RouterOS 创建备份 {clean}") if self._status_ok(resp) else (
                False, f"RouterOS 备份失败 HTTP {getattr(resp, 'status_code', '?')}")
        except Exception as err:  # noqa: BLE001
            return False, f"RouterOS 备份异常：{type(err).__name__} {err}"


def build_firewall_client(address: str, username: str, password: str, http: Any,
                          port: int = 443, use_ssl: bool = True, timeout: int = 10,
                          dry_run: bool = False) -> RouterOSFirewallClient:
    return RouterOSFirewallClient(address, username, password, client=http, port=port,
                                  use_ssl=use_ssl, timeout=timeout, dry_run=dry_run)
