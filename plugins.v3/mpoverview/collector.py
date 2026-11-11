"""MP 总览看板：零配置只读采集层。

设计原则：
1. 只使用 V3 稳定 SDK（app.sdk.*）与 Python 标准库，不碰宿主内部实现；
2. 每一个宿主接口都延迟导入、独立 try/except，任何一项取不到都不影响其它卡片；
3. 全程只读，不写配置、不发通知、不触发任何业务动作；
4. 带 15 秒短时缓存，避免首页仪表盘自动刷新时反复查库。

离线（无宿主模块，例如 stub 测试）时返回结构完整的空快照，
``offline`` 为 True，页面仍可正常渲染。
"""

from __future__ import annotations

import datetime as _dt
import os
import shutil
import time
from typing import Any, Dict, List, Optional

CACHE_TTL = 15.0

_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
_LOCK = None  # 延迟初始化，避免导入期依赖 threading 之外的东西

_SUB_STATE_LABELS = {
    "N": "未开始",
    "R": "订阅中",
    "S": "暂停",
    "P": "待定",
}


def _lock():
    global _LOCK
    if _LOCK is None:
        import threading

        _LOCK = threading.Lock()
    return _LOCK


def _try(fn, default):
    """执行一个采集动作，任何异常都降级为默认值。"""
    try:
        return fn()
    except Exception:  # noqa: BLE001 - 看板不能因为单个数据源失败而整页挂掉
        return default


def _human_bytes(num: Optional[float]) -> str:
    if num is None or num < 0:
        return "—"
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    value = float(num)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return "—"


def _parse_time(value: Any) -> Optional[_dt.datetime]:
    if not value:
        return None
    if isinstance(value, _dt.datetime):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S"):
        try:
            return _dt.datetime.strptime(text[: len(fmt) + 2], fmt)
        except Exception:  # noqa: BLE001
            continue
    return None


def _system() -> Dict[str, Any]:
    """CPU 负载、内存占用与运行时长，全部走标准库。"""
    cores = _try(lambda: os.cpu_count() or 1, 1)
    load1 = _try(lambda: os.getloadavg()[0], None)
    cpu_percent = None
    if isinstance(load1, (int, float)) and cores:
        cpu_percent = round(min(100.0, load1 / cores * 100.0), 1)

    mem_percent = None
    mem_text = "—"

    def _read_mem():
        total = available = None
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    total = float(line.split()[1]) * 1024
                elif line.startswith("MemAvailable:"):
                    available = float(line.split()[1]) * 1024
                if total is not None and available is not None:
                    break
        if not total:
            return None, "—"
        used = total - (available or 0)
        return round(used / total * 100.0, 1), f"{_human_bytes(used)} / {_human_bytes(total)}"

    mem_percent, mem_text = _try(_read_mem, (None, "—"))

    uptime = "—"

    def _read_uptime():
        with open("/proc/uptime", "r", encoding="utf-8") as handle:
            seconds = float(handle.read().split()[0])
        days = int(seconds // 86400)
        hours = int(seconds % 86400 // 3600)
        return f"{days} 天 {hours} 小时" if days else f"{hours} 小时 {int(seconds % 3600 // 60)} 分"

    uptime = _try(_read_uptime, "—")
    return {
        "cores": cores,
        "load1": round(load1, 2) if isinstance(load1, (int, float)) else None,
        "cpu_percent": cpu_percent,
        "memory_percent": mem_percent,
        "memory_text": mem_text,
        "uptime": uptime,
    }


def _candidate_paths() -> List[str]:
    """从宿主配置里找出值得统计的目录，找不到就退回常见挂载点。"""
    paths: List[str] = []

    def _collect(obj: Any) -> None:
        if isinstance(obj, str):
            if obj.startswith("/") and len(obj) < 300:
                paths.append(obj)
        elif isinstance(obj, (list, tuple, set)):
            for item in obj:
                _collect(item)
        elif isinstance(obj, dict):
            for item in obj.values():
                _collect(item)

    def _from_settings():
        from app.sdk.config import settings

        for name in (
            "DOWNLOAD_PATH",
            "DOWNLOAD_SAVE_PATH",
            "LIBRARY_PATHS",
            "MEDIA_PATH",
            "TRANSFER_PATH",
            "CONFIG_DIR",
            "PLUGIN_DATA_PATH",
        ):
            _collect(getattr(settings, name, None))

    _try(_from_settings, None)
    for fallback in ("/config", "/media", "/downloads", "/"):
        paths.append(fallback)

    seen = set()
    result = []
    for path in paths:
        real = _try(lambda p=path: os.path.realpath(p), path)
        if real in seen or not _try(lambda p=real: os.path.isdir(p), False):
            continue
        seen.add(real)
        result.append(real)
    return result


def _storage() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    seen_dev = set()
    for path in _candidate_paths():
        try:
            usage = shutil.disk_usage(path)
            stat = os.stat(path)
            if stat.st_dev in seen_dev or usage.total <= 0:
                continue
            seen_dev.add(stat.st_dev)
            percent = round(usage.used / usage.total * 100.0, 1)
            rows.append({
                "path": path,
                "name": os.path.basename(path.rstrip("/")) or "/",
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
                "percent": percent,
                "free_text": _human_bytes(usage.free),
                "total_text": _human_bytes(usage.total),
            })
        except Exception:  # noqa: BLE001
            continue
    rows.sort(key=lambda r: r["percent"], reverse=True)
    return rows[:4]


def _services() -> Dict[str, Any]:
    def _count(module: str, cls: str) -> int:
        import importlib

        helper = getattr(importlib.import_module(module), cls)()
        configs = helper.get_configs() or {}
        if isinstance(configs, dict):
            return len([v for v in configs.values() if v])
        if isinstance(configs, (list, tuple)):
            return len(configs)
        return 0

    media = _try(lambda: _count("app.sdk.services", "MediaServerHelper"), None)
    downloaders = _try(lambda: _count("app.sdk.services", "DownloaderHelper"), None)
    return {
        "ok": media is not None or downloaders is not None,
        "media_servers": media or 0,
        "downloaders": downloaders or 0,
    }


def _subscriptions() -> Dict[str, Any]:
    def _read():
        from app.sdk import queries

        page = queries.list_subscriptions(page={"page": 1, "count": 200})
        items = list(getattr(page, "items", None) or [])
        total = int(getattr(page, "total", len(items)) or 0)
        active = paused = pending = lack = 0
        for item in items:
            state = str(getattr(item, "state", "") or "").strip().upper()
            if state == "R":
                active += 1
            elif state == "S":
                paused += 1
            else:
                pending += 1
            value = getattr(item, "lack_episode", None)
            if isinstance(value, (int, float)) and value > 0:
                lack += int(value)
        return {
            "ok": True,
            "total": total,
            "active": active,
            "paused": paused,
            "pending": pending,
            "lack": lack,
            "scanned": len(items),
        }

    return _try(_read, {
        "ok": False,
        "total": 0, "active": 0, "paused": 0, "pending": 0, "lack": 0, "scanned": 0,
    })


def _downloads() -> Dict[str, Any]:
    def _read():
        from app.sdk import queries

        page = queries.list_download_history(page={"page": 1, "count": 1})
        return int(getattr(page, "total", 0) or 0)

    value = _try(_read, None)
    return {"ok": value is not None, "total": value or 0}


def _imports() -> Dict[str, Any]:
    def _read():
        from app.sdk import queries

        page = queries.list_transfer_history(page={"page": 1, "count": 200})
        items = list(getattr(page, "items", None) or [])
        total = int(getattr(page, "total", len(items)) or 0)
        now = _dt.datetime.now()
        today = week = 0
        recent: List[Dict[str, str]] = []
        for item in items:
            when = _parse_time(getattr(item, "date", None))
            if when:
                if when.date() == now.date():
                    today += 1
                if (now - when).days < 7 and now >= when:
                    week += 1
            success = bool(getattr(item, "status", False))
            if success and len(recent) < 6:
                title = str(getattr(item, "title", "") or "").strip() or "未命名媒体"
                season = str(getattr(item, "seasons", "") or "").strip()
                episode = str(getattr(item, "episodes", "") or "").strip()
                detail = " ".join(x for x in (season, episode) if x)
                # 这里只做截断，HTML 转义统一由 renderer 负责，避免双重转义或漏转义。
                recent.append({
                    "title": title[:60],
                    "detail": detail[:24],
                    "category": str(getattr(item, "category", "") or "媒体")[:12],
                    "date": str(getattr(item, "date", "") or "")[:16],
                })
        return {"ok": True, "total": total, "today": today, "week": week, "recent": recent}

    return _try(_read, {"ok": False, "total": 0, "today": 0, "week": 0, "recent": []})


def _plugins() -> Dict[str, Any]:
    def _read():
        from app.sdk.plugin.manager import PluginManager

        manager = PluginManager()
        all_ids = manager.get_plugin_ids() or []
        running = manager.get_running_plugin_ids() or []
        return {"ok": True, "total": len(all_ids), "enabled": len(running)}

    return _try(_read, {"ok": False, "total": 0, "enabled": 0})


def _alerts(data: Dict[str, Any]) -> List[Dict[str, str]]:
    alerts: List[Dict[str, str]] = []
    for disk in data["storage"]:
        if disk["percent"] >= 90:
            alerts.append({"level": "danger", "text": f"{disk['name']} 空间剩余 {100 - disk['percent']:.0f}%，建议及时清理"})
        elif disk["percent"] >= 80:
            alerts.append({"level": "warn", "text": f"{disk['name']} 已使用 {disk['percent']:.0f}%，留意容量"})
    mem = data["system"]["memory_percent"]
    if isinstance(mem, (int, float)) and mem >= 90:
        alerts.append({"level": "warn", "text": f"内存占用 {mem:.0f}%，系统可能变慢"})
    services = data["services"]
    if not services["media_servers"]:
        alerts.append({"level": "info", "text": "尚未连接媒体服务器，可在 MoviePilot 设置中添加"})
    if not services["downloaders"]:
        alerts.append({"level": "info", "text": "尚未连接下载器，可在 MoviePilot 设置中添加"})
    if data["subscriptions"]["lack"] > 0:
        alerts.append({"level": "info", "text": f"订阅仍有 {data['subscriptions']['lack']} 集待补齐"})
    if not alerts:
        alerts.append({"level": "ok", "text": "各项状态正常，没有需要处理的提示"})
    return alerts[:5]


def _probe() -> Dict[str, bool]:
    """标记宿主接口是否可用，用于页面显示数据来源状态。"""
    def _has(module: str) -> bool:
        import importlib

        importlib.import_module(module)
        return True

    return {
        "queries": _try(lambda: _has("app.sdk.queries"), False),
        "services": _try(lambda: _has("app.sdk.services"), False),
        "plugins": _try(lambda: _has("app.sdk.plugin.manager"), False),
    }


def collect(force: bool = False) -> Dict[str, Any]:
    """采集一份完整快照。force=True 时跳过缓存重新读取。"""
    with _lock():
        if not force and _CACHE["data"] is not None and time.time() - _CACHE["ts"] < CACHE_TTL:
            return _CACHE["data"]

        sources = _probe()
        data: Dict[str, Any] = {
            "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "system": _system(),
            "storage": _storage(),
            "services": _services(),
            "subscriptions": _subscriptions(),
            "downloads": _downloads(),
            "imports": _imports(),
            "plugins": _plugins(),
            "sources": sources,
        }
        # 以「是否真的调通了宿主只读接口」为准，模块可 import 不代表数据可用。
        # 不可用时应显示占位符，而不是把缺失数据伪装成 0。
        data["offline"] = not any(
            data[key]["ok"]
            for key in ("services", "subscriptions", "downloads", "imports", "plugins")
        )
        data["alerts"] = _alerts(data)
        _CACHE["ts"] = time.time()
        _CACHE["data"] = data
        return data


def invalidate() -> None:
    """清空缓存，供「重新采集」按钮调用。"""
    with _lock():
        _CACHE["ts"] = 0.0
        _CACHE["data"] = None
