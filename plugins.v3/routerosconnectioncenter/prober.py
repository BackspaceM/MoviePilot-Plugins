"""RouterOS 连接中心 —— 一键体检引擎。

只用标准库，不依赖宿主的 AsyncRequestUtils，便于离线注入假响应做单测。
体检按顺序覆盖：配置完整性 → TCP 连通 → REST 可用性 → 认证权限 → 版本 →
业务分区读权限；每一项都给出中文结论，失败项附「可复制到 RouterOS 终端执行」的修复命令。
"""

from __future__ import annotations

import base64
import json
import re
import socket
import ssl
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_FAIL = "fail"
LEVEL_INFO = "info"

_LEVEL_TEXT = {
    LEVEL_OK: "通过",
    LEVEL_WARN: "注意",
    LEVEL_FAIL: "失败",
    LEVEL_INFO: "提示",
}

# 常见失败对应的修复命令（RouterOS v7 CLI）
FIX_ENABLE_REST = [
    "/ip/service/enable www-ssl",
    "/ip/service/set www-ssl port=443",
    "/ip/service/print",
]
FIX_USER = [
    "/user/group/add name=mp-api policy=read,write,api,!ftp,!local,!ssh,!telnet,!winbox,!web",
    "/user/add name=mp-api group=mp-api password=请换成强密码",
    "/user/print",
]
FIX_FIREWALL = [
    "/ip/firewall/filter/print where chain=input",
    "# 确认 input 链放行来自 MoviePilot 容器 IP 的 TCP 443（www-ssl）",
    "/ip/service/print",
]
FIX_CERT = [
    "# 自签证书：在连接中心把「校验证书」关闭，或把 CA 导入 RouterOS",
    "/certificate/print",
]
FIX_VERSION = [
    "/system/resource/print",
    "/system/package/print",
    "# REST API 需要 RouterOS v7；v6 请升级后再使用基于 REST 的插件",
]


def normalize(row: Dict[str, Any]) -> Dict[str, Any]:
    """把一行共享配置规范化成体检所需的连接参数。"""
    raw = str(row.get("address") or "").strip().rstrip("/")
    scheme = "https" if bool(row.get("ssl", True)) else "http"
    host = raw
    port: Any = row.get("port") or (443 if scheme == "https" else 80)
    if "://" in raw:
        scheme, rest = raw.split("://", 1)
        scheme = scheme.lower()
        rest = rest.split("/", 1)[0]
        if ":" in rest and not rest.startswith("["):
            host, _, p = rest.rpartition(":")
            if p.isdigit():
                port = p
        else:
            host = rest
    else:
        host = raw.split("/", 1)[0]
        if ":" in host and not host.startswith("["):
            host, _, p = host.rpartition(":")
            if p.isdigit():
                port = p
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 443 if scheme == "https" else 80
    if not 1 <= port <= 65535:
        port = 443 if scheme == "https" else 80
    default = 443 if scheme == "https" else 80
    base = f"{scheme}://{host}" if port == default else f"{scheme}://{host}:{port}"
    return {
        "name": str(row.get("name") or "").strip(),
        "host": host,
        "port": port,
        "scheme": scheme,
        "base": base,
        "username": str(row.get("username") or "").strip(),
        "password": str(row.get("password") or ""),
        "verify": bool(row.get("verify", False)),
        "note": str(row.get("note") or ""),
    }


def auth_headers(conn: Dict[str, Any]) -> Dict[str, str]:
    token = base64.b64encode(
        f"{conn['username']}:{conn['password']}".encode("utf-8")
    ).decode("ascii")
    return {"Authorization": f"Basic {token}", "Accept": "application/json"}


def default_transport(url: str, headers: Dict[str, str], timeout: float,
                      verify: bool = False) -> Tuple[int, Any]:
    """默认传输：标准库 urllib。返回 (HTTP 状态码或负数错误码, 响应体)。"""
    ctx = None
    if url.startswith("https://"):
        ctx = ssl.create_default_context()
        if not verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return int(resp.status), json.loads(raw)
            except (ValueError, TypeError):
                return int(resp.status), raw[:300]
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", "replace")
        try:
            return int(err.code), json.loads(raw)
        except (ValueError, TypeError):
            return int(err.code), raw[:300]
    except urllib.error.URLError as err:
        reason = str(getattr(err, "reason", err))
        if "CERTIFICATE" in reason.upper() or "SSL" in reason.upper():
            return -2, reason
        return -1, reason
    except Exception as err:  # noqa: BLE001
        return -1, str(err)


def _check(key: str, label: str, level: str, detail: str,
           fix: Optional[List[str]] = None) -> Dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "level": level,
        "level_text": _LEVEL_TEXT.get(level, level),
        "detail": detail,
        "fix": list(fix or []),
    }


def _classify_transport(status: int, body: Any) -> Tuple[str, str, List[str]]:
    """把一次 REST 请求的结果翻译成中文结论与修复命令。"""
    if status == 200:
        return LEVEL_OK, "REST 接口可用，认证通过", []
    if status in (401, 403):
        return LEVEL_FAIL, (
            f"认证或权限失败（HTTP {status}）：账号/密码不对，或该用户缺少 read / api / rest-api 权限"
        ), FIX_USER
    if status == 404:
        return LEVEL_FAIL, (
            "接口不存在（HTTP 404）：REST 需要 RouterOS v7 且 www / www-ssl 服务已启用"
        ), FIX_ENABLE_REST + FIX_VERSION
    if status >= 500:
        return LEVEL_WARN, f"RouterOS 返回服务端错误（HTTP {status}），请查看路由器日志", FIX_VERSION
    if status == -2:
        return LEVEL_FAIL, f"TLS 证书校验失败：{body}", FIX_CERT
    reason = str(body or "无响应")
    low = reason.lower()
    if "timed out" in low or "timeout" in low:
        return LEVEL_FAIL, f"请求超时：{reason}（地址不通或 input 链未放行）", (
            ["# 超时排查：先确认 MP 容器能访问路由器，再检查 www-ssl 服务与 input 链"] + FIX_FIREWALL
        )
    if "refused" in low or "unreachable" in low or "no route" in low:
        return LEVEL_FAIL, f"连接被拒绝/不可达：{reason}", FIX_ENABLE_REST + FIX_FIREWALL
    if "name or service not known" in low or "nodename" in low or "resolve" in low:
        return LEVEL_FAIL, f"域名无法解析：{reason}", ["# 检查地址拼写，或改用 IP 地址"]
    return LEVEL_FAIL, f"请求失败：{reason}", FIX_ENABLE_REST


def _identity(body: Any) -> Dict[str, str]:
    """从 /rest/system/resource 提取设备信息。"""
    if not isinstance(body, dict):
        return {}
    keys = ("version", "architecture-name", "board-name", "cpu", "uptime",
            "total-memory", "free-memory", "platform")
    out: Dict[str, str] = {}
    for key in keys:
        value = body.get(key)
        if value not in (None, ""):
            out[key] = str(value)
    return out


def probe_one(row: Dict[str, Any], transport: Any = None, timeout: float = 8.0,
              deep: bool = True) -> Dict[str, Any]:
    """体检单台路由器，返回结构化结果。transport 可注入用于离线测试。"""
    conn = normalize(row)
    call = transport or default_transport
    checks: List[Dict[str, Any]] = []
    identity: Dict[str, str] = {}
    headers = auth_headers(conn)

    # 1) 配置完整性
    missing = [label for label, value in (
        ("地址", conn["host"]), ("用户名", conn["username"]), ("密码", conn["password"])
    ) if not value]
    if missing:
        checks.append(_check("config", "配置完整性", LEVEL_FAIL,
                             f"缺少：{'、'.join(missing)}；请在连接中心补齐后保存",
                             ["# 格式：名称|地址|端口|用户名|密码|HTTPS(1/0)|备注"]))
    else:
        checks.append(_check("config", "配置完整性", LEVEL_OK,
                             f"地址 {conn['host']}:{conn['port']} · {conn['scheme'].upper()} · 账号 {conn['username']}"))

    # 2) TCP 连通
    tcp_ok = False
    try:
        with socket.create_connection((conn["host"], conn["port"]), timeout=min(timeout, 5.0)):
            tcp_ok = True
        checks.append(_check("tcp", "端口连通", LEVEL_OK,
                             f"TCP {conn['host']}:{conn['port']} 可连接"))
    except Exception as err:  # noqa: BLE001
        checks.append(_check("tcp", "端口连通", LEVEL_FAIL,
                             f"无法连接 {conn['host']}:{conn['port']}：{err}",
                             FIX_FIREWALL + FIX_ENABLE_REST))

    # 3) REST + 认证
    if tcp_ok and not missing:
        status, body = call(f"{conn['base']}/rest/system/resource", headers, timeout, conn["verify"])
        level, detail, fix = _classify_transport(status, body)
        checks.append(_check("rest", "REST 接口与认证", level, detail, fix))
        if status == 200:
            identity = _identity(body)

        # 4) 版本
        version = identity.get("version", "")
        if version:
            major = re.match(r"^(\d+)", version)
            if major and int(major.group(1)) < 7:
                checks.append(_check("version", "RouterOS 版本", LEVEL_FAIL,
                                     f"当前 {version}，REST API 需要 v7", FIX_VERSION))
            else:
                checks.append(_check("version", "RouterOS 版本", LEVEL_OK, f"{version}（支持 REST）"))
        elif status == 200:
            checks.append(_check("version", "RouterOS 版本", LEVEL_WARN,
                                 "接口可用但未返回版本号，按 v7 处理"))

        # 5) 业务分区读权限（轻量探测）
        if deep and status == 200:
            st2, _ = call(f"{conn['base']}/rest/interface", headers, timeout, conn["verify"])
            if st2 == 200:
                checks.append(_check("permission", "业务分区读权限", LEVEL_OK,
                                     "可读取 /rest/interface，read 权限正常"))
            elif st2 in (401, 403):
                checks.append(_check("permission", "业务分区读权限", LEVEL_WARN,
                                     "系统资源可读但业务分区受限，写入类插件（Queue/防火墙）可能失败",
                                     FIX_USER))
            else:
                checks.append(_check("permission", "业务分区读权限", LEVEL_INFO,
                                     f"业务分区探测返回 {st2}，跳过该项"))
    else:
        checks.append(_check("rest", "REST 接口与认证", LEVEL_INFO,
                             "前置条件未满足，已跳过 REST 探测"))

    # 6) 设备信息
    if identity:
        summary = " · ".join(
            f"{k}={v}" for k, v in identity.items() if k in
            ("version", "architecture-name", "board-name", "cpu", "uptime")
        )
        checks.append(_check("identity", "设备信息", LEVEL_INFO, summary or "已读取到设备信息"))

    levels = [c["level"] for c in checks]
    overall = LEVEL_FAIL if LEVEL_FAIL in levels else (
        LEVEL_WARN if LEVEL_WARN in levels else LEVEL_OK)
    return {
        "name": conn["name"] or conn["host"],
        "address": conn["host"],
        "port": conn["port"],
        "scheme": conn["scheme"],
        "level": overall,
        "level_text": _LEVEL_TEXT[overall],
        "checks": checks,
        "identity": identity,
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def probe_all(routers: List[Dict[str, Any]], transport: Any = None,
              timeout: float = 8.0, deep: bool = True) -> List[Dict[str, Any]]:
    return [probe_one(row, transport=transport, timeout=timeout, deep=deep)
            for row in routers if isinstance(row, dict)]


def collect_fixes(results: List[Dict[str, Any]]) -> List[str]:
    """汇总去重后的修复命令，供详情页「复制修复命令」区展示。"""
    out: List[str] = []
    for result in results:
        for check in result.get("checks", []):
            if check.get("level") in (LEVEL_FAIL, LEVEL_WARN):
                for cmd in check.get("fix", []):
                    if cmd and cmd not in out:
                        out.append(cmd)
    return out
