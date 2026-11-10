"""RouterOS 统一工作台 —— 聚合层（纯逻辑，零宿主依赖）。

输入是各分区的原始行 Dict[str, List[dict]] 和本地插件状态，输出一份可直接
渲染的快照。这里不碰 app.*，不联网，所有函数都能拿纯 dict / list 离线单测。

速率按两次刷新的 rx-byte / tx-byte 差值算：previous 里带 sample_seconds
（两次刷新间隔）时，rate = 差值 / 间隔；没有 previous 就展示累计字节并注明。
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import sections

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_CRON_FIELD = re.compile(r"^[0-9*,/\-]+$")
_TRUE = {"true", "yes", "1", "on", "y", "enabled", "running"}
_FALSE = {"false", "no", "0", "off", "n", "disabled", "stopped"}
_TAGS = ("MP-B3", "MP-A2")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _num(value: Any, default: float = 0.0) -> float:
    """把 RouterOS 的字符串数值转成 float，顺手认一下 10M / 1.5G 这类单位。"""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value if value is not None else "").strip()
    if not text:
        return default
    match = re.match(r"^(-?\d+(?:\.\d+)?)\s*([kKmMgGtT]?)(?:[iI]?[bB])?$", text)
    if match:
        scale = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}
        return float(match.group(1)) * scale.get(match.group(2).lower(), 1)
    try:
        return float(text)
    except ValueError:
        return default


def _units(value: float) -> Tuple[float, str]:
    n = float(value)
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024.0
        i += 1
    return n, units[i]


def human_bytes(value: Any) -> str:
    """字节数转人可读，如 1.5 GB。"""
    n = _num(value, 0)
    sign = "-" if n < 0 else ""
    size, unit = _units(abs(n))
    return f"{sign}{int(size)} B" if unit == "B" else f"{sign}{size:.1f} {unit}"


def human_rate(value: Any) -> str:
    """每秒字节数转人可读速率，如 300 B/s。"""
    n = _num(value, 0)
    sign = "-" if n < 0 else ""
    size, unit = _units(abs(n))
    return f"{sign}{int(size)} B/s" if unit == "B" else f"{sign}{size:.1f} {unit}/s"


def _human_seconds(total: int) -> str:
    if total <= 0:
        return "0 秒"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    parts: List[str] = []
    if days:
        parts.append(f"{days} 天")
    if hours:
        parts.append(f"{hours} 小时")
    if mins:
        parts.append(f"{mins} 分")
    if secs and not (days or hours):
        parts.append(f"{secs} 秒")
    return "".join(parts) or "0 秒"


def human_uptime(value: Any) -> str:
    """RouterOS uptime 转人可读，兼容 3w2d10h5m / 纯秒数 / 已经写好文案。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _human_seconds(int(value))
    text = str(value if value is not None else "").strip()
    if not text:
        return "-"
    if text.isdigit():
        return _human_seconds(int(text))
    match = re.match(r"^(?:(\d+)w)?(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$",
                     text, re.IGNORECASE)
    if match and any(match.groups()):
        weeks, days, hours, mins, secs = [int(g or 0) for g in match.groups()]
        return _human_seconds(((weeks * 7 + days) * 24 + hours) * 3600 + mins * 60 + secs)
    return clean_text(text, 40)


def clean_text(value: Any, limit: int = 120) -> str:
    """去掉控制字符、压平空白、限长。"""
    text = _CONTROL.sub(" ", str(value if value is not None else ""))
    text = " ".join(text.split())
    try:
        n = max(0, int(limit))
    except (TypeError, ValueError):
        n = 120
    if n and len(text) > n:
        return text[:n - 1] + "…"
    return text


def _truthy(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value if value is not None else "").strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return default


def _field(row: Any, *keys: str, default: Any = "") -> Any:
    if isinstance(row, dict):
        for key in keys:
            value = row.get(key)
            if value is not None and str(value) != "":
                return value
    return default


def _first(raw: Dict[str, Any], section: str) -> Dict[str, Any]:
    rows = raw.get(section)
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return rows[0]
    return {}


def section_rows(raw: Any, section: str, display_limit: int = 40) -> List[Dict[str, Any]]:
    """取某个分区的行，按 display_limit 限长；行统一复制成新 dict。"""
    rows = (raw or {}).get(section) if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return []
    try:
        limit = max(0, int(display_limit))
    except (TypeError, ValueError):
        limit = 40
    return [dict(r) for r in rows[:limit] if isinstance(r, dict)]


def count_rows(raw: Any, section: str) -> int:
    rows = (raw or {}).get(section) if isinstance(raw, dict) else None
    return len(rows) if isinstance(rows, list) else 0


def validate_cron(cron: Any) -> Tuple[bool, str]:
    """校验 5 位 cron，返回 (是否合法, 规范化后的 cron 或原因)。"""
    parts = str(cron or "").strip().split()
    if len(parts) != 5:
        return False, "cron 要 5 段：分 时 日 月 周"
    for part in parts:
        if not _CRON_FIELD.match(part):
            return False, f"cron 段不合法：{part}"
    return True, " ".join(parts)


# ---------------------------------------------------------------------------
# 摘要 / 接口 / 拓扑
# ---------------------------------------------------------------------------

def _router_summary(raw: Dict[str, Any]) -> Dict[str, Any]:
    res = _first(raw, sections.SEC_RESOURCE)
    ident = _first(raw, sections.SEC_IDENTITY)
    cpu = _num(_field(res, "cpu-load", "cpu_load", default=0), 0)
    total_mem = _num(_field(res, "total-memory", "total_memory", default=0), 0)
    free_mem = _num(_field(res, "free-memory", "free_memory", default=0), 0)
    used_mem = max(0.0, total_mem - free_mem)
    uptime_raw = _field(res, "uptime", default="")
    version = clean_text(_field(res, "version", default="") or _field(ident, "version", default=""), 40)
    return {
        "name": clean_text(_field(ident, "name", default=""), 60),
        "version": version,
        "board_name": clean_text(_field(res, "board-name", "board_name", default=""), 60),
        "cpu_load": cpu,
        "cpu_count": int(_num(_field(res, "cpu-count", "cpu_count", default=0), 0)),
        "total_memory": total_mem,
        "free_memory": free_mem,
        "used_memory": used_mem,
        "memory_text": (f"{human_bytes(used_mem)} / {human_bytes(total_mem)}" if total_mem else "-"),
        "cpu_text": f"{cpu:g}%",
        "uptime": clean_text(uptime_raw, 60),
        "uptime_text": human_uptime(uptime_raw),
    }


def _sample_seconds(previous: Optional[dict], now_ts: float) -> float:
    if not isinstance(previous, dict):
        return 1.0
    explicit = _num(previous.get("sample_seconds"), 0)
    if explicit > 0:
        return explicit
    prev_ts = _num(previous.get("ts"), 0)
    if prev_ts > 0 and now_ts > prev_ts:
        return now_ts - prev_ts
    return 1.0


def _interfaces(raw: Dict[str, Any], previous: Optional[dict],
                sample_seconds: float) -> List[Dict[str, Any]]:
    prev_map: Dict[str, Dict[str, Any]] = {}
    if isinstance(previous, dict):
        for item in previous.get("interfaces") or []:
            if isinstance(item, dict):
                prev_map[str(item.get("name") or "")] = item
    has_prev = isinstance(previous, dict)
    out: List[Dict[str, Any]] = []
    for row in section_rows(raw, sections.SEC_INTERFACE, 100000):
        name = clean_text(_field(row, "name", default=""), 60)
        rx = _num(_field(row, "rx-byte", "rx_byte", "rx", default=0), 0)
        tx = _num(_field(row, "tx-byte", "tx_byte", "tx", default=0), 0)
        rx_delta = tx_delta = 0.0
        rx_rate = tx_rate = 0.0
        if has_prev:
            prev = prev_map.get(name)
            if isinstance(prev, dict):
                rx_delta = max(0.0, rx - _num(prev.get("rx"), 0))
                tx_delta = max(0.0, tx - _num(prev.get("tx"), 0))
                rx_rate = rx_delta / sample_seconds if sample_seconds > 0 else 0.0
                tx_rate = tx_delta / sample_seconds if sample_seconds > 0 else 0.0
        out.append({
            "name": name,
            "type": clean_text(_field(row, "type", "interface-type", default=""), 40),
            "rx": rx,
            "tx": tx,
            "rx_text": human_bytes(rx),
            "tx_text": human_bytes(tx),
            "rx_rate": rx_rate,
            "tx_rate": tx_rate,
            "rx_rate_text": human_rate(rx_rate),
            "tx_rate_text": human_rate(tx_rate),
            "rx_delta": rx_delta,
            "tx_delta": tx_delta,
            "running": _truthy(_field(row, "running", default=""), False),
        })
    return out


def _traffic_card(interfaces: List[Dict[str, Any]], has_prev: bool,
                  sample_seconds: float) -> Dict[str, Any]:
    rx_rate = sum(float(i["rx_rate"]) for i in interfaces)
    tx_rate = sum(float(i["tx_rate"]) for i in interfaces)
    rx_total = sum(float(i["rx"]) for i in interfaces)
    tx_total = sum(float(i["tx"]) for i in interfaces)
    return {
        "mode": "rate" if has_prev else "cumulative",
        "sample_seconds": sample_seconds,
        "rx_rate": rx_rate,
        "tx_rate": tx_rate,
        "rx_rate_text": human_rate(rx_rate),
        "tx_rate_text": human_rate(tx_rate),
        "rx_total": rx_total,
        "tx_total": tx_total,
        "rx_total_text": human_bytes(rx_total),
        "tx_total_text": human_bytes(tx_total),
        "note": ("按最近两次刷新的差值估算" if has_prev else
                 "累计流量，再刷新一次就能看到速率"),
    }


def _topology(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """接口 + IP 地址 + 默认路由，拼成纯数据列表。"""
    out: List[Dict[str, Any]] = []
    for row in section_rows(raw, sections.SEC_INTERFACE, 200):
        out.append({
            "kind": "接口",
            "text": clean_text(_field(row, "name", default=""), 60),
            "detail": clean_text(_field(row, "type", default=""), 40),
        })
    for row in section_rows(raw, sections.SEC_ADDRESS, 200):
        out.append({
            "kind": "地址",
            "text": clean_text(_field(row, "address", default=""), 60),
            "detail": clean_text(_field(row, "interface", default=""), 40),
        })
    for row in section_rows(raw, sections.SEC_ROUTE, 200):
        dst = clean_text(_field(row, "dst-address", "dst_address", default=""), 60)
        if dst.startswith("0.0.0.0") or dst.startswith("::/0"):
            out.append({
                "kind": "默认路由",
                "text": clean_text(_field(row, "gateway", default=""), 60),
                "detail": f"距离 {clean_text(_field(row, 'distance', default=''), 10)}",
            })
    return out


# ---------------------------------------------------------------------------
# 各页签的表格
# ---------------------------------------------------------------------------

def _make_tab(label: str, cards: List[dict], tables: List[dict], stats: Dict[str, Any],
              notes: Optional[List[str]] = None, highlights: Optional[List[str]] = None,
              extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    tab: Dict[str, Any] = {
        "label": label,
        "cards": cards,
        "tables": tables,
        "stats": stats,
        "notes": list(notes or []),
        "highlights": list(highlights or []),
    }
    if extra:
        tab.update(extra)
    return tab


def _rule_rows(raw: Dict[str, Any], section: str, limit: int) -> List[List[str]]:
    rows: List[List[str]] = []
    for row in section_rows(raw, section, limit):
        rows.append([
            clean_text(_field(row, "chain", default=""), 30),
            clean_text(_field(row, "action", default=""), 30),
            clean_text(_field(row, "comment", default=""), 80),
            "禁用" if _truthy(_field(row, "disabled", default=""), False) else "启用",
        ])
    return rows


def _highlights(raw: Dict[str, Any], secs: Sequence[str]) -> List[str]:
    out: List[str] = []
    for sec in secs:
        for row in section_rows(raw, sec, 200):
            blob = " ".join(clean_text(row.get(k, ""), 120)
                            for k in ("comment", "name", "list", "address"))
            for tag in _TAGS:
                if tag in blob:
                    out.append(f"{sections.label_of(sec)}｜{clean_text(blob, 100)}｜{tag}")
                    break
    return out


def _overview_tab(raw: Dict[str, Any], counts: Dict[str, int], router: Dict[str, Any],
                  interfaces: List[Dict[str, Any]], topology: List[Dict[str, Any]],
                  traffic: Dict[str, Any], limit: int) -> Dict[str, Any]:
    rules = (counts[sections.SEC_FILTER] + counts[sections.SEC_NAT]
             + counts[sections.SEC_MANGLE])
    running = sum(1 for i in interfaces if i["running"])
    cards = [
        {"title": "RouterOS 版本", "value": router["version"] or "未知",
         "sub": router["name"] or "-", "color": "primary"},
        {"title": "运行时长", "value": router["uptime_text"],
         "sub": router["board_name"] or "-", "color": "info"},
        {"title": "CPU / 内存", "value": router["cpu_text"],
         "sub": router["memory_text"], "color": "success"},
        {"title": "接口", "value": f"{counts[sections.SEC_INTERFACE]} 个",
         "sub": f"运行中 {running} 个", "color": "info"},
        {"title": "规则", "value": f"{rules} 条", "sub": "filter / nat / mangle",
         "color": "warning"},
        {"title": "队列", "value": f"{counts[sections.SEC_QUEUE]} 条",
         "sub": "简单队列", "color": "info"},
        {"title": "静态 DNS", "value": f"{counts[sections.SEC_DNS]} 条",
         "sub": "本机解析", "color": "secondary"},
        {"title": "实时流量", "value": f"↓ {traffic['rx_rate_text']} / ↑ {traffic['tx_rate_text']}",
         "sub": traffic["note"], "color": "primary"},
    ]
    iface_rows = [[
        i["name"], i["type"], i["rx_text"], i["tx_text"],
        i["rx_rate_text"], i["tx_rate_text"], "运行中" if i["running"] else "未运行",
    ] for i in interfaces[:limit]]
    topo_rows = [[t["kind"], t["text"], t["detail"]] for t in topology[:limit]]
    tables = [
        {"title": "接口流量", "columns": ["接口", "类型", "累计下行", "累计上行",
                                      "下行速率", "上行速率", "状态"],
         "rows": iface_rows, "note": traffic["note"]},
        {"title": "拓扑（接口 / 地址 / 默认路由）", "columns": ["类别", "内容", "说明"],
         "rows": topo_rows, "note": "接口、地址和默认路由放一起看，不画图"},
    ]
    stats = {
        "interfaces": counts[sections.SEC_INTERFACE], "running": running,
        "rules": rules, "queues": counts[sections.SEC_QUEUE],
        "dns": counts[sections.SEC_DNS],
    }
    return _make_tab("概览", cards, tables, stats, notes=[traffic["note"]],
                     extra={"traffic": traffic, "topology": topology})


def _firewall_tab(raw: Dict[str, Any], counts: Dict[str, int], limit: int) -> Dict[str, Any]:
    secs = [sections.SEC_FILTER, sections.SEC_NAT, sections.SEC_MANGLE]
    tables: List[dict] = []
    all_rows: List[Dict[str, Any]] = []
    for sec in secs:
        tables.append({
            "title": sections.label_of(sec),
            "columns": ["链", "动作", "备注", "状态"],
            "rows": _rule_rows(raw, sec, limit),
            "note": "",
        })
        all_rows.extend(section_rows(raw, sec, 100000))
    accept = drop = disabled = 0
    for row in all_rows:
        action = str(_field(row, "action", default="")).strip().lower()
        if action == "accept":
            accept += 1
        elif action == "drop":
            drop += 1
        if _truthy(_field(row, "disabled", default=""), False):
            disabled += 1
    total = sum(counts[s] for s in secs)
    cards = [
        {"title": "规则总数", "value": f"{total} 条", "sub": "filter / nat / mangle",
         "color": "primary"},
        {"title": "放行", "value": f"{accept} 条", "sub": "action=accept", "color": "success"},
        {"title": "丢弃", "value": f"{drop} 条", "sub": "action=drop", "color": "error"},
        {"title": "禁用", "value": f"{disabled} 条", "sub": "disabled", "color": "warning"},
    ]
    stats = {"total": total, "accept": accept, "drop": drop, "disabled": disabled,
             "address_list": counts[sections.SEC_ADDRESS_LIST]}
    return _make_tab("防火墙", cards, tables, stats,
                     notes=[f"地址列表另有 {counts[sections.SEC_ADDRESS_LIST]} 条，在分流页签里"])


def _queue_tab(raw: Dict[str, Any], counts: Dict[str, int], limit: int) -> Dict[str, Any]:
    all_rows = section_rows(raw, sections.SEC_QUEUE, 100000)
    enabled = sum(1 for r in all_rows
                  if not _truthy(_field(r, "disabled", default=""), False))
    rows: List[List[str]] = []
    for row in all_rows[:limit]:
        disabled = _truthy(_field(row, "disabled", default=""), False)
        rows.append([
            clean_text(_field(row, "name", default=""), 60),
            clean_text(_field(row, "target", "target-addresses", default=""), 60),
            clean_text(_field(row, "max-limit", "max_limit", default=""), 40),
            clean_text(_field(row, "rate", default=""), 40),
            "禁用" if disabled else "启用",
        ])
    tables = [{"title": "简单队列", "columns": ["名称", "目标", "限速上限", "当前速率", "状态"],
               "rows": rows, "note": ""}]
    cards = [
        {"title": "队列数", "value": f"{counts[sections.SEC_QUEUE]} 条",
         "sub": "simple queue", "color": "primary"},
        {"title": "启用", "value": f"{enabled} 条", "sub": "没被禁用", "color": "success"},
    ]
    stats = {"total": counts[sections.SEC_QUEUE], "enabled": enabled,
             "disabled": counts[sections.SEC_QUEUE] - enabled}
    return _make_tab("Queue", cards, tables, stats)


def _backup_tab(raw: Dict[str, Any], counts: Dict[str, int], limit: int,
                plugins: List[dict]) -> Dict[str, Any]:
    rows: List[List[str]] = []
    backups = rsc = 0
    for row in section_rows(raw, sections.SEC_FILE, 100000):
        name = clean_text(_field(row, "name", default=""), 80)
        low = name.lower()
        if not (low.endswith(".backup") or low.endswith(".rsc")):
            continue
        if low.endswith(".backup"):
            backups += 1
        else:
            rsc += 1
        rows.append([name, human_bytes(_field(row, "size", default=0)),
                     clean_text(_field(row, "date", default=""), 40)])
    local = [p for p in plugins
             if "ConfigGuard" in str(p.get("id") or "")
             or "备份" in str(p.get("name") or "")]
    local_on = any(bool(p.get("enabled")) for p in local)
    cards = [
        {"title": "备份文件", "value": f"{len(rows)} 个",
         "sub": f".backup {backups} · .rsc {rsc}", "color": "primary"},
        {"title": "本地备份插件", "value": "已启用" if local_on else "没启用 / 没装",
         "sub": "、".join(str(p.get("id")) for p in local[:2]) or "RouterOSConfigGuard",
         "color": "success" if local_on else "warning"},
    ]
    tables = [{"title": "RouterOS 上的备份文件", "columns": ["文件名", "大小", "日期"],
               "rows": rows[:limit], "note": "只列 .backup 和 .rsc"}]
    stats = {"files": len(rows), "backup": backups, "rsc": rsc,
             "local_total": counts[sections.SEC_FILE], "local_enabled": local_on}
    names = "、".join(f"{p.get('id')}（{'启用' if p.get('enabled') else '未启用'}）"
                     for p in local[:4]) or "没找到本地配置备份插件"
    return _make_tab("备份", cards, tables, stats,
                     notes=[f"本地配置备份插件：{names}"],
                     extra={"backup_plugins": local})


def _routing_tab(raw: Dict[str, Any], counts: Dict[str, int], limit: int) -> Dict[str, Any]:
    dns_rows = [[
        clean_text(_field(r, "name", default=""), 60),
        clean_text(_field(r, "address", default=""), 60),
        clean_text(_field(r, "comment", default=""), 80),
    ] for r in section_rows(raw, sections.SEC_DNS, limit)]
    al_rows = [[
        clean_text(_field(r, "address", default=""), 60),
        clean_text(_field(r, "list", default=""), 60),
        clean_text(_field(r, "comment", default=""), 80),
    ] for r in section_rows(raw, sections.SEC_ADDRESS_LIST, limit)]
    route_rows = [[
        clean_text(_field(r, "dst-address", "dst_address", default=""), 60),
        clean_text(_field(r, "gateway", default=""), 60),
        clean_text(_field(r, "distance", default=""), 20),
        clean_text(_field(r, "comment", default=""), 80),
    ] for r in section_rows(raw, sections.SEC_ROUTE, limit)]
    tables = [
        {"title": "静态 DNS", "columns": ["名称", "地址", "备注"], "rows": dns_rows, "note": ""},
        {"title": "地址列表", "columns": ["地址", "列表", "备注"], "rows": al_rows, "note": ""},
        {"title": "路由", "columns": ["目标", "网关", "距离", "备注"], "rows": route_rows, "note": ""},
    ]
    highlights = _highlights(raw, [sections.SEC_DNS, sections.SEC_ADDRESS_LIST,
                                   sections.SEC_ROUTE])
    default_routes = sum(1 for r in section_rows(raw, sections.SEC_ROUTE, 100000)
                         if str(_field(r, "dst-address", "dst_address", default=""))
                         .startswith("0.0.0.0"))
    cards = [
        {"title": "静态 DNS", "value": f"{counts[sections.SEC_DNS]} 条",
         "sub": "本机解析", "color": "primary"},
        {"title": "地址列表", "value": f"{counts[sections.SEC_ADDRESS_LIST]} 条",
         "sub": "分流常用", "color": "info"},
        {"title": "路由", "value": f"{counts[sections.SEC_ROUTE]} 条",
         "sub": f"默认路由 {default_routes} 条", "color": "secondary"},
        {"title": "带标签", "value": f"{len(highlights)} 条",
         "sub": "MP-B3 / MP-A2", "color": "warning"},
    ]
    stats = {"dns": counts[sections.SEC_DNS], "address_list": counts[sections.SEC_ADDRESS_LIST],
             "routes": counts[sections.SEC_ROUTE], "default_routes": default_routes,
             "highlights": len(highlights)}
    notes = ["带 MP-B3 / MP-A2 的条目列在下面，认起来方便"] if highlights else []
    return _make_tab("分流", cards, tables, stats, notes=notes, highlights=highlights)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def build_snapshot(raw: Dict[str, List[dict]], plugin_states: List[dict],
                   previous: Optional[dict] = None, display_limit: int = 40) -> dict:
    """把各分区原始行整理成一份可渲染的聚合快照。"""
    raw = dict(raw or {})
    try:
        limit = max(1, min(500, int(display_limit or 40)))
    except (TypeError, ValueError):
        limit = 40
    now_ts = time.time()
    has_prev = isinstance(previous, dict)
    sample_seconds = _sample_seconds(previous, now_ts)

    counts = {sec: count_rows(raw, sec) for sec in sections.SECTIONS}
    router = _router_summary(raw)
    interfaces = _interfaces(raw, previous, sample_seconds)
    traffic = _traffic_card(interfaces, has_prev, sample_seconds)
    topology = _topology(raw)
    plugins = [dict(p) for p in (plugin_states or []) if isinstance(p, dict)]

    tabs = {
        "overview": _overview_tab(raw, counts, router, interfaces, topology, traffic, limit),
        "firewall": _firewall_tab(raw, counts, limit),
        "queue": _queue_tab(raw, counts, limit),
        "backup": _backup_tab(raw, counts, limit, plugins),
        "routing": _routing_tab(raw, counts, limit),
    }

    warnings: List[str] = []
    for sec in sections.SECTIONS:
        if sec not in raw:
            warnings.append(f"{sections.label_of(sec)}（{sec}）没读到数据")
    if not has_prev:
        warnings.append("还没有上一次快照，实时流量先显示累计值，再刷新一次就有速率")
    if not raw:
        warnings.append("没读到任何 RouterOS 分区，先检查地址和 API 账号读权限")

    return {
        "created": now_text(),
        "ts": now_ts,
        "router": router,
        "interfaces": interfaces,
        "topology": topology,
        "tabs": tabs,
        "plugins": plugins,
        "counts": counts,
        "warnings": warnings,
        "traffic": traffic,
        "sample_seconds": sample_seconds,
        "display_limit": limit,
        "has_previous": has_prev,
    }
