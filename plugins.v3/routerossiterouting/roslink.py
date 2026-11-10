"""RouterOS 连接中心 —— 跨插件共享连接配置（只读端 / 广播端共用）。

所有 RouterOS 插件目录内各嵌一份本模块的副本。由于每个插件的 ``__init__.py``
都位于同一个 plugins 根目录下，``Path(__file__).parent.parent`` 天然指向同一个
共享目录，因此无需依赖任何宿主私有 API 就能读到「连接中心」广播的路由器配置。

配置真相源是连接中心自身的插件配置（由宿主持久化）；这里的 JSON 只是广播通道，
连接中心每次加载/保存配置时都会重建。读取失败时，各插件自动回退到原有的手填
字段 —— 不安装连接中心、或连接中心未启用，行为与改造前完全一致。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_SHARED_NAME = "routeros_connections.json"
_ENV_KEY = "ROUTEROS_CONNECTION_FILE"
_VERSION = 1

# 各插件连接字段命名差异（历史原因）：
#   cfrouteros            -> router_host / router_user / router_verify
#   其余三个 ROS 插件     -> router_address / router_username
_FIELD_STYLES = {
    "std": ("router_address", "router_username"),
    "cf": ("router_host", "router_user"),
}


def shared_candidates() -> List[Path]:
    """共享文件候选路径，读写双方使用同一顺序。"""
    out: List[Path] = []
    env = str(os.environ.get(_ENV_KEY) or "").strip()
    if env:
        out.append(Path(env))
    try:
        # __file__ = <plugins>/<plugin_id>/roslink.py -> parent.parent = <plugins>/
        out.append(Path(__file__).resolve().parent.parent / _SHARED_NAME)
    except Exception:  # noqa: BLE001
        pass
    out.append(Path(tempfile.gettempdir()) / _SHARED_NAME)
    seen = set()
    result = []
    for path in out:
        key = str(path)
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def read_shared() -> Dict[str, Any]:
    for path in shared_candidates():
        try:
            if not path.is_file():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("routers"), list):
                return data
        except Exception:  # noqa: BLE001 - 损坏的文件直接跳过，换下一个候选
            continue
    return {"version": _VERSION, "routers": []}


def write_shared(routers: List[Dict[str, Any]]) -> Tuple[bool, str]:
    """原子写广播文件；逐个候选路径降级，返回 (是否成功, 路径或错误)。"""
    payload = {"version": _VERSION, "routers": routers}
    errors: List[str] = []
    for path in shared_candidates():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(str(tmp), str(path))
            try:
                os.chmod(str(path), 0o600)  # 含明文凭据，仅属主可读
            except Exception:  # noqa: BLE001
                pass
            return True, str(path)
        except Exception as err:  # noqa: BLE001
            errors.append(f"{path}: {err}")
            continue
    return False, "; ".join(errors[:2]) or "无可用写入路径"


def list_routers() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in read_shared().get("routers", []):
        if isinstance(row, dict) and str(row.get("name") or "").strip():
            out.append(row)
    return out


def connection_items() -> List[Dict[str, str]]:
    """配置页下拉项。"""
    items: List[Dict[str, str]] = []
    for row in list_routers():
        name = str(row.get("name") or "").strip()
        addr = str(row.get("address") or "").strip()
        proto = "HTTPS" if bool(row.get("ssl", True)) else "HTTP"
        items.append({"title": f"{name}（{addr} · {proto}）", "value": name})
    return items


def find_router(name: Any) -> Optional[Dict[str, Any]]:
    target = str(name or "").strip()
    if not target:
        return None
    for row in list_routers():
        if str(row.get("name") or "").strip() == target:
            return row
    return None


def connection_overrides(selected: Any, style: str = "std") -> Tuple[Dict[str, Any], str]:
    """把选中的已保存路由器翻译成目标插件的连接字段。

    返回 (覆盖字段, 说明)。找不到时返回 ({}, 原因)，调用方应保持原配置不变。
    """
    target = str(selected or "").strip()
    if not target:
        return {}, ""
    row = find_router(target)
    if row is None:
        return {}, f"已保存路由器「{target}」不存在，已回退到本插件手填的连接参数"
    addr_key, user_key = _FIELD_STYLES.get(style, _FIELD_STYLES["std"])
    overrides: Dict[str, Any] = {
        addr_key: str(row.get("address") or "").strip(),
        "router_port": _safe_port(row.get("port")),
        user_key: str(row.get("username") or "").strip(),
        "router_password": str(row.get("password") or ""),
        "router_ssl": bool(row.get("ssl", True)),
    }
    if style == "cf":
        overrides["router_verify"] = bool(row.get("verify", False))
    note = f"已使用连接中心的「{target}」覆盖本插件连接参数（地址/端口/账号/密码/HTTPS）"
    return overrides, note


def _safe_port(value: Any) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return 443
    return port if 1 <= port <= 65535 else 443


def mask(value: Any) -> str:
    """脱敏显示密码：固定长度掩码，不泄露真实密码长度。"""
    text = str(value or "")
    if not text:
        return "（未填写）"
    if len(text) <= 2:
        return "******"
    return f"{text[0]}******{text[-1]}"


def public_router(row: Dict[str, Any]) -> Dict[str, Any]:
    """详情页/API 用的脱敏视图。"""
    return {
        "name": str(row.get("name") or ""),
        "address": str(row.get("address") or ""),
        "port": _safe_port(row.get("port")),
        "username": str(row.get("username") or ""),
        "password": mask(row.get("password")),
        "ssl": bool(row.get("ssl", True)),
        "verify": bool(row.get("verify", False)),
        "note": str(row.get("note") or ""),
    }


ROUTERS_EXAMPLE = (
    "主路由|192.168.88.1|443|mp-api|你的密码|1|客厅 RB5009\n"
    "旁路由|10.0.0.1|80|admin|另一个密码|0|仅 HTTP，测试用"
)

_TRUE = {"1", "true", "yes", "on", "https", "y"}
_FALSE = {"0", "false", "no", "off", "http", "n"}


def _truthy(value: Any, default: bool = True) -> bool:
    text = str(value if value is not None else "").strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return default


def parse_routers(text: Any) -> Tuple[List[Dict[str, Any]], List[str]]:
    """解析连接中心的路由器清单文本；同时兼容直接粘贴 JSON 数组。

    每行格式：``名称|地址|端口|用户名|密码|HTTPS(1/0)|备注``
    ``#`` 开头为注释，空行忽略；备注可省略，其余 6 项必填。
    返回 (路由器列表, 错误说明列表)。
    """
    raw = str(text or "").strip()
    if not raw:
        return [], []
    if raw.startswith("["):
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                rows: List[Dict[str, Any]] = []
                errors: List[str] = []
                for i, item in enumerate(data, 1):
                    if not isinstance(item, dict):
                        errors.append(f"JSON 第 {i} 项不是对象")
                        continue
                    norm, err = _normalize_row(item, i)
                    if err:
                        errors.append(err)
                    else:
                        rows.append(norm)
                return rows, errors
        except (ValueError, TypeError):
            pass  # 不是合法 JSON，按纯文本解析

    rows: List[Dict[str, Any]] = []
    errors: List[str] = []
    for lineno, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|", 6)]
        if len(parts) < 6:
            errors.append(f"第 {lineno} 行字段不足 6 项：{line[:50]}")
            continue
        item = {
            "name": parts[0], "address": parts[1], "port": parts[2],
            "username": parts[3], "password": parts[4], "ssl": parts[5],
            "note": parts[6] if len(parts) > 6 else "",
        }
        norm, err = _normalize_row(item, lineno)
        if err:
            errors.append(err)
        else:
            rows.append(norm)
    # 名称去重（后面的覆盖前面的，与「同名行优先覆盖」的既有约定一致）
    dedup: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        dedup[str(row["name"])] = row
    return list(dedup.values()), errors


def _normalize_row(item: Dict[str, Any], lineno: int) -> Tuple[Dict[str, Any], str]:
    name = str(item.get("name") or "").strip()
    address = str(item.get("address") or "").strip()
    if not name or not address:
        return {}, f"第 {lineno} 行缺少名称或地址"
    try:
        port = int(str(item.get("port") or "").strip() or 443)
    except (TypeError, ValueError):
        return {}, f"第 {lineno} 行端口不是数字：{item.get('port')}"
    if not 1 <= port <= 65535:
        return {}, f"第 {lineno} 行端口超出 1-65535：{port}"
    row = {
        "name": name,
        "address": address,
        "port": port,
        "username": str(item.get("username") or "").strip(),
        "password": str(item.get("password") or ""),
        "ssl": _truthy(item.get("ssl"), True),
        "verify": bool(item.get("verify", False)),
        "note": str(item.get("note") or "").strip(),
    }
    return row, ""
