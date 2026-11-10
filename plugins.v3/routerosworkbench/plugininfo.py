"""RouterOS 统一工作台 —— 本地插件状态适配（延迟、容错）。

只在 load_plugin_states() 被调用时才去 import 宿主模块，模块导入期不联网、
不碰 app.*、不抛异常。统一走 V3 稳定入口 app.sdk.plugin.PluginManager，
拿不到就返回空列表和一句说明。

关注列表里没装的插件也会留一条占位，这样工具箱能一眼看出谁在、谁没装。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

WATCHED_IDS = [
    "RouterOSConnectionCenter",
    "RosFirewallManager",
    "BandwidthQueueScheduler",
    "RouterOSConfigGuard",
    "CFRouterOS",
    "RouterOSScenarioTemplates",
    "RouterOSRemoteAccessWizard",
    "RouterOSSiteRouting",
]

_TRUE = {"true", "1", "yes", "on", "enabled", "running", "installed", "active"}
_FALSE = {"false", "0", "no", "off", "disabled", "stopped", "inactive", "uninstalled"}


def _read(record: Any, *keys: str, default: Any = None) -> Any:
    """兼容 dict / 对象取字段，遇到第一个非空值就返回。"""
    for key in keys:
        try:
            if isinstance(record, dict):
                if key in record and record[key] is not None:
                    return record[key]
            else:
                value = getattr(record, key, None)
                if value is not None:
                    return value
        except Exception:  # noqa: BLE001
            continue
    return default


def _parse_enabled(value: Any) -> Optional[bool]:
    """把 enabled / state / installed 这类字段折成一个布尔，判不出来返回 None。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        for key in ("enabled", "state", "installed", "plugin_state"):
            if key in value:
                parsed = _parse_enabled(value[key])
                if parsed is not None:
                    return parsed
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    text = str(value if value is not None else "").strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return None


def _parse_installed(installed_raw: Any, state_raw: Any, default: bool = True) -> bool:
    parsed = _parse_enabled(installed_raw)
    if parsed is not None:
        return parsed
    text = str(state_raw if state_raw is not None else "").strip().lower()
    if text in ("uninstalled", "not_installed", "not-installed", "none", "未安装"):
        return False
    if text:
        return True
    return default


def _probe_enabled(manager: Any, pid: str, fallback: Optional[bool]) -> Optional[bool]:
    """用 get_plugin_config / get_plugin_state 补启用状态，宿主没有就跳过。"""
    for fname in ("get_plugin_config", "get_plugin_state"):
        fn = getattr(manager, fname, None)
        if not callable(fn):
            continue
        try:
            parsed = _parse_enabled(fn(pid))
        except Exception:  # noqa: BLE001
            continue
        if parsed is not None:
            return parsed
    return fallback


def normalize_record(record: Any, pid_hint: str = "", manager: Any = None) -> Dict[str, Any]:
    """把一条插件记录归一化成工具箱要的字段，dict 与对象两种都吃。"""
    pid = str(_read(record, "id", "plugin_id", default=pid_hint) or pid_hint or "").strip()
    name = str(_read(record, "plugin_name", "name", default="") or "").strip() or pid
    author = str(_read(record, "plugin_author", "author", default="") or "").strip()
    version = str(_read(record, "plugin_version", "version", default="") or "").strip()
    has_update = bool(_read(record, "has_update", default=False))
    repo_url = str(_read(record, "repo_url", default="") or "").strip()
    installed_raw = _read(record, "installed", default=None)
    state_raw = _read(record, "state", default=None)
    installed = _parse_installed(installed_raw, state_raw)

    enabled = _parse_enabled(state_raw)
    if enabled is None:
        enabled = _parse_enabled(installed_raw)
    if enabled is None:
        enabled = bool(installed)
    if manager is not None:
        probed = _probe_enabled(manager, pid, None)
        if probed is not None:
            enabled = probed

    if not installed:
        note = "本机没装，装了才会出现在这里"
    elif has_update:
        note = "有新版本"
    elif not enabled:
        note = "装了没启用"
    else:
        note = ""
    return {
        "id": pid,
        "name": name,
        "author": author,
        "version": version,
        "enabled": bool(enabled),
        "installed": bool(installed),
        "has_update": has_update,
        "repo_url": repo_url,
        "note": note,
    }


def _records_from(result: Any) -> List[Any]:
    """把 PluginManager 的返回值摊平成记录列表（兼容 list / dict / 单条）。"""
    if result is None:
        return []
    if isinstance(result, (list, tuple, set)):
        return [r for r in result if r is not None]
    if isinstance(result, dict):
        if isinstance(result.get("plugins"), (list, tuple)):
            return [r for r in result["plugins"] if r is not None]
        out: List[Any] = []
        for key, value in result.items():
            if isinstance(value, dict):
                row = dict(value)
                row.setdefault("id", key)
                out.append(row)
            elif not isinstance(value, (str, int, float, bool, type(None))):
                out.append(value)
        return out
    return [result]


def _instance(manager_cls: Any) -> Any:
    """PluginManager 在 V3 里是实例方法；少数宿主用类方法，实例化失败就退回类本身。"""
    try:
        return manager_cls()
    except Exception:  # noqa: BLE001
        return manager_cls


def _load_manager() -> Tuple[Any, str]:
    try:
        from app.sdk.plugin import PluginManager  # type: ignore
        return _instance(PluginManager), "app.sdk.plugin.PluginManager"
    except Exception:  # noqa: BLE001
        return None, ""


def load_plugin_states() -> Tuple[List[dict], str]:
    """返回 (插件状态列表, 一句来源说明)。拿不到宿主就返回空列表，不抛异常。"""
    manager, source = _load_manager()
    if manager is None:
        return [], "拿不到插件管理器，插件状态先留空"

    records: List[Any] = []
    for fname in ("get_installed_plugins", "get_local_plugins"):
        fn = getattr(manager, fname, None)
        if not callable(fn):
            continue
        try:
            rows = _records_from(fn())
        except Exception:  # noqa: BLE001
            continue
        if rows:
            records = rows
            source = f"{source}.{fname}()"
            break

    by_id: Dict[str, Dict[str, Any]] = {}
    for rec in records:
        try:
            item = normalize_record(rec, manager=manager)
        except Exception:  # noqa: BLE001
            continue
        if item.get("id"):
            by_id[item["id"]] = item

    states: List[dict] = []
    for pid in WATCHED_IDS:
        item = by_id.get(pid)
        if item is None:
            item = {
                "id": pid, "name": pid, "author": "", "version": "",
                "enabled": False, "installed": False, "has_update": False,
                "repo_url": "", "note": "本机没装，装了才会出现在这里",
            }
        states.append(item)
    return states, (f"插件状态来自 {source}" if source else "插件状态没找到来源")
